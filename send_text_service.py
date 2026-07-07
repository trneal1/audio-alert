#!/usr/bin/env python3
"""TCP text-to-speech queue service for the ESP32 AUD1 audio receiver.

Clients connect to this service and send one UTF-8 text message per line. The
service accepts multiple simultaneous clients, queues messages in order of
receipt, fetches spoken MP3 audio from Google's free Translate TTS endpoint,
converts that MP3 to mono signed 16-bit little-endian PCM with either ffmpeg or
the Python av package, and plays the result on the audio alert device
one message at a time.
"""

import argparse
import asyncio
import hashlib
import io
import itertools
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from array import array
from dataclasses import dataclass, field
from pathlib import Path

from aud1_protocol import DEFAULT_PORT, DEFAULT_SAMPLE_RATE, send_pcm


GOOGLE_TTS_URL = "https://translate.google.com/translate_tts"
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 7788
DEFAULT_LANGUAGE = "en"
DEFAULT_VOLUME = 0.85
DEFAULT_PLAY_DELAY = 0.5
DEFAULT_MAX_LINE_BYTES = 4096
DEFAULT_DECODER = "ffmpeg"
DEFAULT_TTS_CACHE_DIR = ".tts_cache"
MAX_TTS_CACHE_LABEL_CHARS = 80
PLAY_WINDOW_PATTERN = re.compile(r"^(\d{2})(\d{2})-(\d{2})(\d{2})$")


@dataclass(frozen=True)
class PlayWindow:
    start_minute: int
    end_minute: int
    label: str


@dataclass(order=True)
class QueuedMessage:
    sequence: int
    received_at: float = field(compare=False)
    text: str = field(compare=False)
    client_name: str = field(compare=False)
    client_writer: asyncio.StreamWriter = field(compare=False)


def parse_play_window(value):
    match = PLAY_WINDOW_PATTERN.fullmatch(value)
    if not match:
        raise argparse.ArgumentTypeError("play window must use HHMM-HHMM, for example 0555-2205")

    start_hour, start_minute, end_hour, end_minute = (int(part) for part in match.groups())
    if start_hour > 23 or end_hour > 23:
        raise argparse.ArgumentTypeError("play window hours must be from 00 to 23")
    if start_minute > 59 or end_minute > 59:
        raise argparse.ArgumentTypeError("play window minutes must be from 00 to 59")

    return PlayWindow(
        start_minute=start_hour * 60 + start_minute,
        end_minute=end_hour * 60 + end_minute,
        label=value,
    )


def is_within_play_window(play_window, when=None):
    if play_window is None:
        return True

    local_time = time.localtime(when)
    current_minute = local_time.tm_hour * 60 + local_time.tm_min
    if play_window.start_minute == play_window.end_minute:
        return True
    if play_window.start_minute < play_window.end_minute:
        return play_window.start_minute <= current_minute <= play_window.end_minute
    return current_minute >= play_window.start_minute or current_minute <= play_window.end_minute


def fetch_google_tts_mp3(text, language):
    params = urllib.parse.urlencode(
        {
            "ie": "UTF-8",
            "client": "tw-ob",
            "tl": language,
            "q": text,
        }
    )
    request = urllib.request.Request(
        f"{GOOGLE_TTS_URL}?{params}",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            )
        },
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        content_type = response.headers.get("Content-Type", "")
        mp3_bytes = response.read()

    if not mp3_bytes:
        raise RuntimeError("Google TTS returned no audio")
    if "audio" not in content_type and not mp3_bytes.startswith(b"ID3"):
        raise RuntimeError(f"Google TTS did not return audio, content type was {content_type!r}")

    return mp3_bytes


def ffmpeg_mp3_to_pcm16(mp3_bytes, sample_rate, volume):
    volume_filter = f"volume={volume}" if volume != 1.0 else "anull"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-af",
        volume_filter,
        "-f",
        "s16le",
        "pipe:1",
    ]

    try:
        result = subprocess.run(
            command,
            input=mp3_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required and was not found on PATH") from exc

    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed: {error}")
    if not result.stdout:
        raise RuntimeError("ffmpeg returned no PCM audio")

    return result.stdout


def apply_pcm16_volume(pcm_bytes, volume):
    if volume == 1.0:
        return pcm_bytes

    samples = array("h")
    samples.frombytes(pcm_bytes)

    if sys.byteorder != "little":
        samples.byteswap()

    for index, sample in enumerate(samples):
        samples[index] = max(-32768, min(32767, int(sample * volume)))

    if sys.byteorder != "little":
        samples.byteswap()

    return samples.tobytes()


def av_mp3_to_pcm16(mp3_bytes, sample_rate, volume):
    try:
        import av
    except ImportError as exc:
        raise RuntimeError(
            "av decoder selected, but the av package is not installed; "
            "install it with: python -m pip install av"
        ) from exc

    pcm_chunks = []
    try:
        with av.open(io.BytesIO(mp3_bytes), mode="r") as container:
            resampler = av.audio.resampler.AudioResampler(
                format="s16",
                layout="mono",
                rate=sample_rate,
            )
            for frame in container.decode(audio=0):
                for resampled in resampler.resample(frame):
                    pcm_chunks.append(bytes(resampled.planes[0]))
    except av.error.FFmpegError as exc:
        raise RuntimeError(f"av failed to decode MP3: {exc}") from exc

    pcm_bytes = b"".join(pcm_chunks)
    if not pcm_bytes:
        raise RuntimeError("av returned no PCM audio")

    return apply_pcm16_volume(pcm_bytes, volume)


def mp3_to_pcm16(mp3_bytes, sample_rate, volume, decoder):
    if decoder == "ffmpeg":
        return ffmpeg_mp3_to_pcm16(mp3_bytes, sample_rate, volume)
    if decoder == "av":
        return av_mp3_to_pcm16(mp3_bytes, sample_rate, volume)

    raise RuntimeError(f"unknown decoder: {decoder}")


def tts_cache_label(text):
    label = re.sub(r"\s+", " ", text).strip().lower()
    label = re.sub(r"[^a-z0-9._ -]+", "", label)
    label = re.sub(r"[ ._-]+", "-", label).strip("-")
    return (label or "empty")[:MAX_TTS_CACHE_LABEL_CHARS]


def tts_cache_key(language, text):
    return hashlib.sha256(f"{language}\0{text}".encode("utf-8")).hexdigest()


def tts_cache_path(cache_dir, language, text):
    cache_key = tts_cache_key(language, text)
    label = tts_cache_label(text)
    return Path(cache_dir) / f"{language}-{label}-{cache_key[:12]}.mp3"


def legacy_tts_cache_path(cache_dir, language, text):
    cache_key = hashlib.sha256(f"{language}\0{text}".encode("utf-8")).hexdigest()
    return Path(cache_dir) / f"{cache_key}.mp3"


def fetch_cached_tts_mp3(text, language, cache_dir):
    if not cache_dir:
        return fetch_google_tts_mp3(text, language), False

    cache_path = tts_cache_path(cache_dir, language, text)
    try:
        mp3_bytes = cache_path.read_bytes()
        if mp3_bytes:
            return mp3_bytes, True
    except FileNotFoundError:
        pass

    legacy_cache_path = legacy_tts_cache_path(cache_dir, language, text)
    try:
        mp3_bytes = legacy_cache_path.read_bytes()
        if mp3_bytes:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                legacy_cache_path.replace(cache_path)
            except OSError:
                pass
            return mp3_bytes, True
    except FileNotFoundError:
        pass

    mp3_bytes = fetch_google_tts_mp3(text, language)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(f".{os.getpid()}.tmp")
    try:
        temp_path.write_bytes(mp3_bytes)
        temp_path.replace(cache_path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass

    return mp3_bytes, False


async def write_line(writer, line):
    writer.write(f"{line}\n".encode("utf-8"))
    await writer.drain()


async def try_write_line(writer, line):
    if writer.is_closing():
        return False

    try:
        await write_line(writer, line)
        return True
    except (ConnectionError, OSError, RuntimeError):
        return False


async def close_writer(writer):
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError, RuntimeError):
        pass


async def read_text_line(reader):
    try:
        return await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            return exc.partial
        return None
    except (ConnectionError, OSError, RuntimeError):
        return None


async def handle_client(reader, writer, queue, counter, max_line_bytes):
    peer = writer.get_extra_info("peername")
    client_name = f"{peer[0]}:{peer[1]}" if peer else "unknown"
    print(f"client connected: {client_name}")

    if not await try_write_line(writer, "READY send one UTF-8 text message per line"):
        print(f"client disconnected before READY: {client_name}")
        await close_writer(writer)
        return

    try:
        while True:
            try:
                raw_line = await read_text_line(reader)
            except asyncio.LimitOverrunError:
                await try_write_line(writer, f"ERR line too long, max {max_line_bytes} bytes")
                await reader.read(max_line_bytes)
                continue

            if raw_line is None:
                break

            if len(raw_line) > max_line_bytes:
                await try_write_line(writer, f"ERR line too long, max {max_line_bytes} bytes")
                continue

            text = raw_line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            sequence = next(counter)
            message = QueuedMessage(
                sequence=sequence,
                received_at=time.time(),
                text=text,
                client_name=client_name,
                client_writer=writer,
            )
            await queue.put(message)
            await try_write_line(writer, f"QUEUED {sequence}")
            print(f"queued #{sequence} from {client_name}: {text}")
    finally:
        await close_writer(writer)
        print(f"client disconnected: {client_name}")


async def playback_worker(queue, args):
    while True:
        message = await queue.get()

        try:
            if not is_within_play_window(args.play_window):
                print(
                    f"skipping #{message.sequence} outside play window "
                    f"{args.play_window.label}: {message.text}"
                )
                await try_write_line(
                    message.client_writer,
                    f"SKIPPED {message.sequence} OUTSIDE_PLAY_WINDOW {args.play_window.label}",
                )
                continue

            print(f"playing #{message.sequence} from {message.client_name}: {message.text}")
            mp3_bytes, cache_hit = await asyncio.to_thread(
                fetch_cached_tts_mp3,
                message.text,
                args.language,
                args.tts_cache_dir,
            )
            print(f"tts cache {'hit' if cache_hit else 'miss'} #{message.sequence}")
            await try_write_line(
                message.client_writer,
                f"TTS_CACHE {message.sequence} {'HIT' if cache_hit else 'MISS'}",
            )
            pcm_bytes = await asyncio.to_thread(
                mp3_to_pcm16,
                mp3_bytes,
                args.sample_rate,
                args.volume,
                args.decoder,
            )

            def report_send_start(part, byte_count):
                print(f"sending {part} #{message.sequence}: {byte_count} bytes")

            await asyncio.to_thread(
                send_pcm,
                args.device_host,
                args.device_port,
                args.sample_rate,
                pcm_bytes,
                report_send_start,
            )
            print(f"done #{message.sequence}")
        except Exception as exc:
            print(f"error playing #{message.sequence}: {exc}", file=sys.stderr)
        finally:
            queue.task_done()

        if args.play_delay > 0:
            await asyncio.sleep(args.play_delay)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Accept TCP text messages, queue them, and play them on the ESP32 audio alert device.",
    )
    parser.add_argument(
        "device_host",
        help="ESP32 host or IP address, for example audio-alert.local",
    )
    parser.add_argument(
        "--device-port",
        type=int,
        default=DEFAULT_PORT,
        help="ESP32 AUD1 TCP port",
    )
    parser.add_argument(
        "--listen-host",
        default=DEFAULT_LISTEN_HOST,
        help="Host/interface for the text service to listen on",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=DEFAULT_LISTEN_PORT,
        help="TCP port for clients to submit text messages",
    )
    parser.add_argument(
        "-r",
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help="Sample rate in Hz, 8000 to 48000",
    )
    parser.add_argument(
        "-l",
        "--language",
        default=DEFAULT_LANGUAGE,
        help="Google TTS language code, for example en, en-US, es, fr",
    )
    parser.add_argument(
        "-v",
        "--volume",
        type=float,
        default=DEFAULT_VOLUME,
        help="Audio volume multiplier, for example 0.5, 1.0, or 1.5",
    )
    parser.add_argument(
        "--decoder",
        choices=("ffmpeg", "av"),
        default=DEFAULT_DECODER,
        help="MP3 decoder backend: ffmpeg subprocess or Python av package",
    )
    parser.add_argument(
        "--tts-cache-dir",
        default=DEFAULT_TTS_CACHE_DIR,
        help="Directory for cached TTS MP3 files; use an empty value to disable",
    )
    parser.add_argument(
        "--play-delay",
        type=float,
        default=DEFAULT_PLAY_DELAY,
        help="Seconds to wait between queued plays",
    )
    parser.add_argument(
        "--play-window",
        type=parse_play_window,
        default=None,
        metavar="HHMM-HHMM",
        help=(
            "Local-time window when queued audio may play, for example 0555-2205; "
            "omit to allow playback at any time"
        ),
    )
    parser.add_argument(
        "--max-line-bytes",
        type=int,
        default=DEFAULT_MAX_LINE_BYTES,
        help="Maximum UTF-8 bytes accepted for one submitted text line",
    )
    return parser


async def run_server(args):
    queue = asyncio.PriorityQueue()
    counter = itertools.count(1)

    worker_task = asyncio.create_task(playback_worker(queue, args))
    server = await asyncio.start_server(
        lambda reader, writer: handle_client(reader, writer, queue, counter, args.max_line_bytes),
        args.listen_host,
        args.listen_port,
        limit=args.max_line_bytes + 1,
    )

    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"text service listening on {sockets}")
    print(f"playing to {args.device_host}:{args.device_port}")
    print(f"decoder: {args.decoder}")
    if args.play_window:
        print(f"play window: {args.play_window.label}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    async with server:
        await stop_event.wait()

    print("stopping text service")
    worker_task.cancel()
    await asyncio.gather(worker_task, return_exceptions=True)


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if not 8000 <= args.sample_rate <= 48000:
        parser.error("--sample-rate must be between 8000 and 48000")
    if args.volume <= 0:
        parser.error("--volume must be greater than 0")
    if args.play_delay < 0:
        parser.error("--play-delay must be 0 or greater")
    if args.max_line_bytes < 1:
        parser.error("--max-line-bytes must be greater than 0")

    try:
        asyncio.run(run_server(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
