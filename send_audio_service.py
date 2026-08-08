#!/usr/bin/env python3
"""Combined TCP text and tone queue service for the ESP32 AUD1 receiver.

Clients connect to this service and send one UTF-8 command per line.

Examples:
  TEXT The laundry is done
  TONE 440:0.2 440-880:0.6 880:0.2 volume=0.35 wobbles=5

Bare lines without a command prefix are treated as text.
"""

import argparse
import asyncio
import itertools
import signal
import sys
import time
from dataclasses import dataclass, field

from aud1_protocol import DEFAULT_PORT, DEFAULT_SAMPLE_RATE, send_pcm
from send_text_service import (
    DEFAULT_DECODER,
    DEFAULT_LANGUAGE,
    DEFAULT_MAX_LINE_BYTES,
    DEFAULT_PLAY_DELAY,
    DEFAULT_TTS_CACHE_DIR,
    DEFAULT_VOLUME as DEFAULT_TEXT_VOLUME,
    fetch_cached_tts_mp3,
    is_within_play_window,
    mp3_to_pcm16,
    parse_play_window,
)
from send_tone_service import (
    DEFAULT_DEVICE_HOST,
    build_tone_sequence_pcm,
    expand_wobbles,
    parse_tone_request,
)


DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 7790


@dataclass(frozen=True)
class AudioRequest:
    kind: str
    text: str = ""
    tone_request: object = None


@dataclass(order=True)
class QueuedAudioRequest:
    sequence: int
    received_at: float = field(compare=False)
    request: AudioRequest = field(compare=False)
    client_name: str = field(compare=False)
    client_writer: asyncio.StreamWriter = field(compare=False)


def parse_audio_request(line, default_sample_rate):
    command, _, payload = line.partition(" ")
    command_upper = command.upper()

    if command_upper == "TEXT":
        text = payload.strip()
        if not text:
            raise ValueError("TEXT requires message text")
        return AudioRequest(kind="text", text=text)

    if command_upper == "TONE":
        tone_line = payload.strip()
        if not tone_line:
            raise ValueError("TONE requires at least one FREQ:DURATION tone")
        return AudioRequest(
            kind="tone",
            tone_request=parse_tone_request(tone_line, default_sample_rate),
        )

    return AudioRequest(kind="text", text=line)


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
        "READY send TEXT message, TONE 440:0.2 440-880:0.6 volume=0.35 wobbles=5, "
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
                request = parse_audio_request(line, args.sample_rate)
            except ValueError as exc:
                await try_write_line(writer, f"ERR {exc}")
                continue

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
            await try_write_line(writer, f"QUEUED {sequence} {request.kind.upper()}")
            print(f"queued {request.kind} #{sequence} from {client_name}: {line}")
    finally:
        await close_writer(writer)
        print(f"audio client disconnected: {client_name}")


async def build_text_pcm(message, args):
    mp3_bytes, cache_hit = await asyncio.to_thread(
        fetch_cached_tts_mp3,
        message.request.text,
        args.language,
        args.tts_cache_dir,
    )
    await try_write_line(
        message.client_writer,
        f"TTS_CACHE {message.sequence} {'HIT' if cache_hit else 'MISS'}",
    )
    pcm_bytes = await asyncio.to_thread(
        mp3_to_pcm16,
        mp3_bytes,
        args.sample_rate,
        args.text_volume,
        args.decoder,
    )
    return pcm_bytes, args.sample_rate


async def build_tone_pcm(message):
    tone_request = message.request.tone_request
    tones = expand_wobbles(tone_request.tones, tone_request.wobbles)
    pcm_bytes = build_tone_sequence_pcm(
        tones,
        tone_request.sample_rate,
        tone_request.volume,
        tone_request.gap,
    )
    return pcm_bytes, tone_request.sample_rate


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
