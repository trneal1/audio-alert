#!/usr/bin/env python3
"""TCP tone queue service for the ESP32 AUD1 audio receiver.

Clients connect to this service and send one UTF-8 line containing a sequence
of FREQ:DURATION tones plus optional volume and wobble count settings.

Example client line:
  440:0.2 440-880:0.6 880:0.2 volume=0.35 wobbles=5
"""

import argparse
import asyncio
import itertools
import shlex
import signal
import sys
import time
from dataclasses import dataclass, field

from aud1_protocol import DEFAULT_PORT, DEFAULT_SAMPLE_RATE, send_pcm
from send_tone import (
    DEFAULT_HOST as DEFAULT_DEVICE_HOST,
    DEFAULT_VOLUME,
    DEFAULT_WOBBLES,
    build_tone_sequence_pcm,
    expand_wobbles,
    parse_tone,
)


DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 7789
DEFAULT_GAP = 0.0
DEFAULT_PLAY_DELAY = 0.05
DEFAULT_MAX_LINE_BYTES = 4096


@dataclass(frozen=True)
class ToneRequest:
    tones: list[tuple[float, ...]]
    volume: float
    wobbles: int
    gap: float
    sample_rate: int
    raw_line: str


@dataclass(order=True)
class QueuedToneRequest:
    sequence: int
    received_at: float = field(compare=False)
    request: ToneRequest = field(compare=False)
    client_name: str = field(compare=False)
    client_writer: asyncio.StreamWriter = field(compare=False)


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
    volume = DEFAULT_VOLUME
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
            except argparse.ArgumentTypeError as exc:
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
    print(f"tone client connected: {client_name}")

    ready = (
        "READY send tones like: "
        "440:0.2 440-880:0.6 volume=0.35 wobbles=5 gap=0.05"
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
                request = parse_tone_request(line, args.sample_rate)
            except ValueError as exc:
                await try_write_line(writer, f"ERR {exc}")
                continue

            sequence = next(counter)
            message = QueuedToneRequest(
                sequence=sequence,
                received_at=time.time(),
                request=request,
                client_name=client_name,
                client_writer=writer,
            )
            await queue.put(message)
            await try_write_line(writer, f"QUEUED {sequence}")
            print(f"queued tone #{sequence} from {client_name}: {line}")
    finally:
        await close_writer(writer)
        print(f"tone client disconnected: {client_name}")


async def playback_worker(queue, args):
    while True:
        message = await queue.get()

        try:
            request = message.request
            tones = expand_wobbles(request.tones, request.wobbles)
            pcm_bytes = build_tone_sequence_pcm(
                tones,
                request.sample_rate,
                request.volume,
                request.gap,
            )
            total_seconds = len(pcm_bytes) / 2 / request.sample_rate

            print(
                f"playing tone #{message.sequence} from {message.client_name}: "
                f"{len(request.tones)} tone(s) x {request.wobbles}, "
                f"{total_seconds:.3f}s, volume={request.volume:g}"
            )
            await try_write_line(
                message.client_writer,
                f"PLAYING {message.sequence} {len(pcm_bytes)} bytes {total_seconds:.3f}s",
            )

            def report_send_start(part, byte_count):
                print(f"sending {part} tone #{message.sequence}: {byte_count} bytes")

            await asyncio.to_thread(
                send_pcm,
                args.device_host,
                args.device_port,
                request.sample_rate,
                pcm_bytes,
                report_send_start,
            )
            await try_write_line(message.client_writer, f"DONE {message.sequence}")
            print(f"done tone #{message.sequence}")
        except Exception as exc:
            await try_write_line(message.client_writer, f"ERR {message.sequence} {exc}")
            print(f"error playing tone #{message.sequence}: {exc}", file=sys.stderr)
        finally:
            queue.task_done()

        if args.play_delay > 0:
            await asyncio.sleep(args.play_delay)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Accept TCP tone requests, queue them, and play them on the ESP32 audio alert device.",
    )
    parser.add_argument(
        "device_host",
        nargs="?",
        default=DEFAULT_DEVICE_HOST,
        help=f"ESP32 host or IP address, default: {DEFAULT_DEVICE_HOST}",
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
        help="Host/interface for the tone service to listen on",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=DEFAULT_LISTEN_PORT,
        help="TCP port for clients to submit tone requests",
    )
    parser.add_argument(
        "-r",
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help="Default sample rate in Hz, 8000 to 48000",
    )
    parser.add_argument(
        "--play-delay",
        type=float,
        default=DEFAULT_PLAY_DELAY,
        help="Seconds to wait between queued tone plays",
    )
    parser.add_argument(
        "--max-line-bytes",
        type=int,
        default=DEFAULT_MAX_LINE_BYTES,
        help="Maximum UTF-8 bytes accepted for one submitted request line",
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
    print(f"tone service listening on {sockets}")
    print(f"playing to {args.device_host}:{args.device_port}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    async with server:
        await stop_event.wait()

    print("stopping tone service")
    worker_task.cancel()
    await asyncio.gather(worker_task, return_exceptions=True)


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if not 8000 <= args.sample_rate <= 48000:
        parser.error("--sample-rate must be between 8000 and 48000")
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
