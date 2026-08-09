#!/usr/bin/env python3
"""Combined TCP text and tone queue service for the ESP32 AUD1 receiver.

Clients connect to this service and send one UTF-8 command per line.

Examples:
  TEXT The laundry is done
  TEXT volume=0.65 The laundry is done
  TONE 440:0.2 440-880:0.6 880:0.2 volume=0.35 wobbles=5
  SEQUENCE TONE 440:0.2; TEXT beep; TONE 880:0.2
  REPEAT 3 TONE 440:0.2; TEXT beep; TONE 880:0.2

Bare lines without a command prefix are treated as text.
"""

import argparse
import asyncio
import hashlib
import io
import itertools
import math
import os
import re
import shlex
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
DEFAULT_DEVICE_HOST = "audio-alert.local"
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 7790
DEFAULT_LANGUAGE = "en"
DEFAULT_TEXT_VOLUME = 0.85
DEFAULT_TONE_VOLUME = 0.35
DEFAULT_WOBBLES = 1
DEFAULT_GAP = 0.0
DEFAULT_PLAY_DELAY = 0.5
DEFAULT_SEQUENCE_GAP = 0.5
DEFAULT_MAX_LINE_BYTES = 4096
DEFAULT_DECODER = "ffmpeg"
DEFAULT_TTS_CACHE_DIR = ".tts_cache"
MAX_TTS_CACHE_LABEL_CHARS = 80
MAX_REPEAT_COUNT = 100
PLAY_WINDOW_PATTERN = re.compile(r"^(\d{2})(\d{2})-(\d{2})(\d{2})$")


@dataclass(frozen=True)
class PlayWindow:
    start_minute: int
    end_minute: int
    label: str


@dataclass(frozen=True)
class ToneRequest:
    tones: list[tuple[float, ...]]
    volume: float
    wobbles: int
    gap: float
    sample_rate: int
    raw_line: str


@dataclass(frozen=True)
class AudioRequest:
    kind: str
    text: str = ""
    text_volume: object = None
    tone_request: object = None
    items: tuple = ()
    repeat_count: int = 1
    sequence_length: int = 1


@dataclass(order=True)
class QueuedAudioRequest:
    sequence: int
    received_at: float = field(compare=False)
    request: AudioRequest = field(compare=False)
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


def parse_frequency_range(value):
    parts = value.split("-", 1)
    try:
        if len(parts) == 1:
            frequency = float(parts[0])
            start_frequency = frequency
            end_frequency = frequency
        else:
            start_frequency = float(parts[0])
            end_frequency = float(parts[1])
    except ValueError as exc:
        raise ValueError("frequency must be a number") from exc

    if start_frequency <= 0 or end_frequency <= 0:
        raise ValueError("frequency must be greater than 0")

    return start_frequency, end_frequency


def parse_tone(value):
    separator = ":" if ":" in value else ","
    parts = value.split(separator)

    if len(parts) != 2:
        raise ValueError(
            "tone must be FREQ:DURATION or START-END:DURATION, for example 440:1.5 or 440-880:1.5"
        )

    try:
        start_frequency, end_frequency = parse_frequency_range(parts[0])
        duration = float(parts[1])
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    if duration <= 0:
        raise ValueError("duration must be greater than 0")

    return start_frequency, end_frequency, duration


def generate_tone_pcm(start_frequency, end_frequency, duration, sample_rate, volume):
    sample_count = max(1, int(round(duration * sample_rate)))
    amplitude = int(32767 * volume)
    samples = array("h")
    phase = 0.0

    for index in range(sample_count):
        value = math.sin(phase)
        if sample_count == 1:
            frequency = start_frequency
        else:
            fraction = index / (sample_count - 1)
            frequency = start_frequency + ((end_frequency - start_frequency) * fraction)

        phase += (2.0 * math.pi * frequency) / sample_rate
        samples.append(int(amplitude * value))

    if sys.byteorder != "little":
        samples.byteswap()

    return samples.tobytes()


def build_tone_sequence_pcm(tones, sample_rate, volume, gap):
    chunks = []
    silence = b"\x00\x00" * int(round(gap * sample_rate))

    for index, tone in enumerate(tones):
        if index > 0 and silence:
            chunks.append(silence)

        if len(tone) == 2:
            frequency, duration = tone
            start_frequency = frequency
            end_frequency = frequency
        else:
            start_frequency, end_frequency, duration = tone

        chunks.append(generate_tone_pcm(start_frequency, end_frequency, duration, sample_rate, volume))

    return b"".join(chunks)


def expand_wobbles(tones, wobble_count):
    return list(tones) * wobble_count


def parse_int_setting(name, value):
    try:
        return int(value, 10)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def parse_float_setting(name, value):
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def parse_tone_request(line, default_sample_rate):
    tokens = shlex.split(line)
    if not tokens:
        raise ValueError("empty request")

    tones = []
    positional_settings = []
    volume = DEFAULT_TONE_VOLUME
    wobbles = DEFAULT_WOBBLES
    gap = DEFAULT_GAP
    sample_rate = default_sample_rate

    for token in tokens:
        if "=" not in token:
            if ":" not in token and "," not in token:
                positional_settings.append(token)
                continue

            try:
                tones.append(parse_tone(token))
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            continue

        name, value = token.split("=", 1)
        name = name.strip().lower().replace("-", "_")
        value = value.strip()
        if not value:
            raise ValueError(f"{name} requires a value")

        if name in ("volume", "vol", "v"):
            volume = parse_float_setting("volume", value)
        elif name in ("wobbles", "wobble", "repeat", "repeats", "w"):
            wobbles = parse_int_setting("wobbles", value)
        elif name == "gap":
            gap = parse_float_setting("gap", value)
        elif name in ("sample_rate", "rate", "r"):
            sample_rate = parse_int_setting("sample_rate", value)
        else:
            raise ValueError(f"unknown setting: {name}")

    if positional_settings:
        if len(positional_settings) > 2:
            raise ValueError("use at most two positional settings: volume wobbles")
        volume = parse_float_setting("volume", positional_settings[0])
        if len(positional_settings) == 2:
            wobbles = parse_int_setting("wobbles", positional_settings[1])

    if not tones:
        raise ValueError("at least one FREQ:DURATION tone is required")
    if not 0.0 <= volume <= 1.0:
        raise ValueError("volume must be between 0.0 and 1.0")
    if wobbles < 1:
        raise ValueError("wobbles must be 1 or greater")
    if gap < 0:
        raise ValueError("gap must be 0 or greater")
    if not 8000 <= sample_rate <= 48000:
        raise ValueError("sample_rate must be between 8000 and 48000")

    return ToneRequest(
        tones=tones,
        volume=volume,
        wobbles=wobbles,
        gap=gap,
        sample_rate=sample_rate,
        raw_line=line,
    )


def parse_text_request(payload):
    text = payload.strip()
    text_volume = None

    while text:
        setting, separator, remainder = text.partition(" ")
        if "=" not in setting:
            break

        name, value = setting.split("=", 1)
        name = name.strip().lower().replace("-", "_")
        if name not in ("text_volume", "volume", "vol", "v"):
            break

        if not value:
            raise ValueError(f"{name} requires a value")

        text_volume = parse_float_setting("text volume", value)
        if text_volume <= 0:
            raise ValueError("text volume must be greater than 0")

        text = remainder.strip() if separator else ""

    if not text:
        raise ValueError("TEXT requires message text")

    return AudioRequest(kind="text", text=text, text_volume=text_volume)


def parse_audio_request(line, default_sample_rate):
    command, _, payload = line.partition(" ")
    command_upper = command.upper()

    if command_upper == "TEXT":
        return parse_text_request(payload)

    if command_upper == "TONE":
        tone_line = payload.strip()
        if not tone_line:
            raise ValueError("TONE requires at least one FREQ:DURATION tone")
        return AudioRequest(
            kind="tone",
            tone_request=parse_tone_request(tone_line, default_sample_rate),
        )

    return AudioRequest(kind="text", text=line)


def parse_repeat_count(value):
    try:
        repeat_count = int(value, 10)
    except ValueError as exc:
        raise ValueError("REPEAT count must be an integer") from exc

    if repeat_count < 1:
        raise ValueError("REPEAT count must be 1 or greater")
    if repeat_count > MAX_REPEAT_COUNT:
        raise ValueError(f"REPEAT count must be {MAX_REPEAT_COUNT} or less")

    return repeat_count


def parse_audio_sequence_items(sequence_text, default_sample_rate, command_name):
    sequence_text = sequence_text.strip()
    if sequence_text.startswith("[") and sequence_text.endswith("]"):
        sequence_text = sequence_text[1:-1].strip()

    items = [item.strip() for item in sequence_text.split(";") if item.strip()]
    if not items:
        raise ValueError(f"{command_name} requires at least one nested TEXT or TONE command")

    sequence = []
    for item in items:
        request = parse_audio_request(item, default_sample_rate)
        if request.kind not in ("text", "tone"):
            raise ValueError(f"{command_name} items must be TEXT or TONE commands")
        sequence.append(request)

    return sequence


def parse_sequence_audio_request(line, default_sample_rate):
    _, _, sequence_text = line.partition(" ")
    if not sequence_text.strip():
        raise ValueError("SEQUENCE requires: SEQUENCE COMMAND ... ; COMMAND ...")

    sequence = parse_audio_sequence_items(sequence_text, default_sample_rate, "SEQUENCE")
    return AudioRequest(
        kind="sequence",
        items=tuple(sequence),
        repeat_count=1,
        sequence_length=len(sequence),
    )


def parse_repeated_audio_requests(line, default_sample_rate):
    _, _, payload = line.partition(" ")
    count_text, _, sequence_text = payload.strip().partition(" ")
    if not count_text or not sequence_text.strip():
        raise ValueError("REPEAT requires: REPEAT count COMMAND ... ; COMMAND ...")

    repeat_count = parse_repeat_count(count_text)
    sequence = parse_audio_sequence_items(sequence_text, default_sample_rate, "REPEAT")
    return AudioRequest(
        kind="sequence",
        items=tuple(sequence * repeat_count),
        repeat_count=repeat_count,
        sequence_length=len(sequence),
    )


def parse_audio_requests(line, default_sample_rate):
    command = line.split(None, 1)[0].upper() if line.split(None, 1) else ""
    if command == "SEQUENCE":
        request = parse_sequence_audio_request(line, default_sample_rate)
        return [request], request.repeat_count, request.sequence_length
    if command == "REPEAT":
        request = parse_repeated_audio_requests(line, default_sample_rate)
        return [request], request.repeat_count, request.sequence_length

    return [parse_audio_request(line, default_sample_rate)], 1, 1


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


async def handle_client(reader, writer, queue, counter, args):
    peer = writer.get_extra_info("peername")
    client_name = f"{peer[0]}:{peer[1]}" if peer else "unknown"
    print(f"audio client connected: {client_name}")

    ready = (
        "READY send TEXT volume=0.65 message, TONE 440:0.2 440-880:0.6 volume=0.35 wobbles=5, "
        "SEQUENCE TONE 440:0.2; TEXT beep; TONE 880:0.2, "
        "REPEAT 3 TONE 440:0.2; TEXT beep; TONE 880:0.2, "
        "or bare text"
    )
    if not await try_write_line(writer, ready):
        await close_writer(writer)
        return

    try:
        while True:
            try:
                raw_line = await read_text_line(reader)
            except asyncio.LimitOverrunError:
                await try_write_line(writer, f"ERR line too long, max {args.max_line_bytes} bytes")
                await reader.read(args.max_line_bytes)
                continue

            if raw_line is None:
                break
            if len(raw_line) > args.max_line_bytes:
                await try_write_line(writer, f"ERR line too long, max {args.max_line_bytes} bytes")
                continue

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                requests, repeat_count, sequence_length = parse_audio_requests(line, args.sample_rate)
            except ValueError as exc:
                await try_write_line(writer, f"ERR {exc}")
                continue

            for request in requests:
                sequence = next(counter)
                await queue.put(
                    QueuedAudioRequest(
                        sequence=sequence,
                        received_at=time.time(),
                        request=request,
                        client_name=client_name,
                        client_writer=writer,
                    )
                )

            if len(requests) == 1:
                request = requests[0]
                if request.kind == "sequence":
                    item_count = len(request.items)
                    await try_write_line(
                        writer,
                        (
                            f"QUEUED {sequence} SEQUENCE "
                            f"{request.repeat_count}x {request.sequence_length} item(s), "
                            f"{item_count} total"
                        ),
                    )
                    print(
                        f"queued sequence #{sequence} from {client_name}: "
                        f"{request.repeat_count}x {request.sequence_length} item(s), {item_count} total"
                    )
                else:
                    await try_write_line(writer, f"QUEUED {sequence} {request.kind.upper()}")
                    print(f"queued {request.kind} #{sequence} from {client_name}: {line}")
            else:
                await try_write_line(
                    writer,
                    f"QUEUED {sequence} REPEAT {repeat_count}x {sequence_length} item(s)",
                )
                print(
                    f"queued repeat #{sequence} from {client_name}: "
                    f"{repeat_count}x {sequence_length} item(s)"
                )
    finally:
        await close_writer(writer)
        print(f"audio client disconnected: {client_name}")


async def build_text_pcm(message, args, request=None, cache_label=None):
    request = request or message.request
    mp3_bytes, cache_hit = await asyncio.to_thread(
        fetch_cached_tts_mp3,
        request.text,
        args.language,
        args.tts_cache_dir,
    )
    label = f"{message.sequence}{cache_label or ''}"
    await try_write_line(
        message.client_writer,
        f"TTS_CACHE {label} {'HIT' if cache_hit else 'MISS'}",
    )
    pcm_bytes = await asyncio.to_thread(
        mp3_to_pcm16,
        mp3_bytes,
        args.sample_rate,
        request.text_volume if request.text_volume is not None else args.text_volume,
        args.decoder,
    )
    return pcm_bytes, args.sample_rate


async def build_tone_pcm(message, request=None, sample_rate=None):
    request = request or message.request
    tone_request = request.tone_request
    output_sample_rate = sample_rate or tone_request.sample_rate
    tones = expand_wobbles(tone_request.tones, tone_request.wobbles)
    pcm_bytes = build_tone_sequence_pcm(
        tones,
        output_sample_rate,
        tone_request.volume,
        tone_request.gap,
    )
    return pcm_bytes, output_sample_rate


def build_silence_pcm(seconds, sample_rate):
    if seconds <= 0:
        return b""
    return b"\x00\x00" * int(round(seconds * sample_rate))


async def build_sequence_pcm(message, args):
    chunks = []
    sample_rate = args.sample_rate
    silence = build_silence_pcm(args.sequence_gap, sample_rate)

    for index, request in enumerate(message.request.items, start=1):
        if index > 1 and silence:
            chunks.append(silence)

        if request.kind == "text":
            pcm_bytes, _ = await build_text_pcm(message, args, request, f".{index}")
        elif request.kind == "tone":
            pcm_bytes, _ = await build_tone_pcm(message, request, sample_rate)
        else:
            raise RuntimeError(f"unsupported sequence item: {request.kind}")

        chunks.append(pcm_bytes)

    return b"".join(chunks), sample_rate


async def playback_worker(queue, args):
    while True:
        message = await queue.get()

        try:
            if not is_within_play_window(args.play_window):
                label = args.play_window.label
                print(f"skipping #{message.sequence} outside play window {label}")
                await try_write_line(message.client_writer, f"SKIPPED {message.sequence} OUTSIDE_PLAY_WINDOW {label}")
                continue

            print(f"playing {message.request.kind} #{message.sequence} from {message.client_name}")
            if message.request.kind == "text":
                pcm_bytes, sample_rate = await build_text_pcm(message, args)
            elif message.request.kind == "sequence":
                pcm_bytes, sample_rate = await build_sequence_pcm(message, args)
            else:
                pcm_bytes, sample_rate = await build_tone_pcm(message)

            total_seconds = len(pcm_bytes) / 2 / sample_rate
            await try_write_line(
                message.client_writer,
                f"PLAYING {message.sequence} {message.request.kind.upper()} {len(pcm_bytes)} bytes {total_seconds:.3f}s",
            )

            def report_send_start(part, byte_count):
                print(f"sending {part} {message.request.kind} #{message.sequence}: {byte_count} bytes")

            await asyncio.to_thread(
                send_pcm,
                args.device_host,
                args.device_port,
                sample_rate,
                pcm_bytes,
                report_send_start,
            )
            await try_write_line(message.client_writer, f"DONE {message.sequence}")
            print(f"done {message.request.kind} #{message.sequence}")
        except Exception as exc:
            await try_write_line(message.client_writer, f"ERR {message.sequence} {exc}")
            print(f"error playing #{message.sequence}: {exc}", file=sys.stderr)
        finally:
            queue.task_done()

        if args.play_delay > 0:
            await asyncio.sleep(args.play_delay)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Accept queued TEXT and TONE commands and play them on the ESP32 audio alert device.",
    )
    parser.add_argument(
        "device_host",
        nargs="?",
        default=DEFAULT_DEVICE_HOST,
        help=f"ESP32 host or IP address, default: {DEFAULT_DEVICE_HOST}",
    )
    parser.add_argument("--device-port", type=int, default=DEFAULT_PORT, help="ESP32 AUD1 TCP port")
    parser.add_argument("--listen-host", default=DEFAULT_LISTEN_HOST, help="Host/interface to listen on")
    parser.add_argument("--listen-port", type=int, default=DEFAULT_LISTEN_PORT, help="TCP command port")
    parser.add_argument("-r", "--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE, help="Default sample rate in Hz")
    parser.add_argument("-l", "--language", default=DEFAULT_LANGUAGE, help="Google TTS language code")
    parser.add_argument(
        "--text-volume",
        type=float,
        default=DEFAULT_TEXT_VOLUME,
        help="Text-to-speech volume multiplier",
    )
    parser.add_argument("--decoder", choices=("ffmpeg", "av"), default=DEFAULT_DECODER, help="MP3 decoder backend")
    parser.add_argument("--tts-cache-dir", default=DEFAULT_TTS_CACHE_DIR, help="Directory for cached TTS MP3 files")
    parser.add_argument("--play-delay", type=float, default=DEFAULT_PLAY_DELAY, help="Seconds between queued plays")
    parser.add_argument(
        "--sequence-gap",
        type=float,
        default=DEFAULT_SEQUENCE_GAP,
        help="Seconds of silence between TEXT/TONE items inside one REPEAT sequence",
    )
    parser.add_argument(
        "--play-window",
        type=parse_play_window,
        default=None,
        metavar="HHMM-HHMM",
        help="Local-time playback window, for example 0555-2205",
    )
    parser.add_argument(
        "--max-line-bytes",
        type=int,
        default=DEFAULT_MAX_LINE_BYTES,
        help="Maximum UTF-8 bytes accepted for one command line",
    )
    return parser


async def run_server(args):
    queue = asyncio.PriorityQueue()
    counter = itertools.count(1)

    worker_task = asyncio.create_task(playback_worker(queue, args))
    server = await asyncio.start_server(
        lambda reader, writer: handle_client(reader, writer, queue, counter, args),
        args.listen_host,
        args.listen_port,
        limit=args.max_line_bytes + 1,
    )

    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"audio service listening on {sockets}")
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

    print("stopping audio service")
    worker_task.cancel()
    await asyncio.gather(worker_task, return_exceptions=True)


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if not 8000 <= args.sample_rate <= 48000:
        parser.error("--sample-rate must be between 8000 and 48000")
    if args.text_volume <= 0:
        parser.error("--text-volume must be greater than 0")
    if args.play_delay < 0:
        parser.error("--play-delay must be 0 or greater")
    if args.sequence_gap < 0:
        parser.error("--sequence-gap must be 0 or greater")
    if args.max_line_bytes < 1:
        parser.error("--max-line-bytes must be greater than 0")
    if args.device_port < 1 or args.device_port > 65535:
        parser.error("--device-port must be from 1 to 65535")
    if args.listen_port < 1 or args.listen_port > 65535:
        parser.error("--listen-port must be from 1 to 65535")

    try:
        asyncio.run(run_server(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
