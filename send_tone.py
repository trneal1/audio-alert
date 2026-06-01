#!/usr/bin/env python3
"""Send generated sine-wave tones to the ESP32 AUD1 TCP audio receiver."""

import argparse
import math
import socket
import struct
import sys
from array import array


DEFAULT_PORT = 7777
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_VOLUME = 0.35


def parse_tone(value):
    """Parse FREQ:DURATION or FREQ,DURATION into a (frequency, duration) tuple."""
    separator = ":" if ":" in value else ","
    parts = value.split(separator)

    if len(parts) != 2:
        raise argparse.ArgumentTypeError("tone must be FREQ:DURATION, for example 440:1.5")

    try:
        frequency = float(parts[0])
        duration = float(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("frequency and duration must be numbers") from exc

    if frequency <= 0:
        raise argparse.ArgumentTypeError("frequency must be greater than 0")
    if duration <= 0:
        raise argparse.ArgumentTypeError("duration must be greater than 0")

    return frequency, duration


def generate_tone_pcm(frequency, duration, sample_rate, volume):
    sample_count = max(1, int(round(duration * sample_rate)))
    amplitude = int(32767 * volume)
    samples = array("h")

    for index in range(sample_count):
        value = math.sin((2.0 * math.pi * frequency * index) / sample_rate)
        samples.append(int(amplitude * value))

    if sys.byteorder != "little":
        samples.byteswap()

    return samples.tobytes()


def make_header(sample_rate, channels, pcm_bytes):
    return b"AUD1" + struct.pack(
        "<IBBBBI",
        sample_rate,
        channels,
        16,
        1,
        0,
        len(pcm_bytes),
    )


def read_reply(sock):
    return sock.recv(128).decode("utf-8", errors="replace").strip()


def send_pcm(host, port, sample_rate, pcm_bytes):
    header = make_header(sample_rate, 1, pcm_bytes)

    with socket.create_connection((host, port), timeout=10) as sock:
        sock.settimeout(10)
        sock.sendall(header)

        reply = read_reply(sock)
        print(reply)
        if not reply.startswith("OK"):
            raise RuntimeError(f"device rejected stream: {reply}")

        sock.sendall(pcm_bytes)

        try:
            final_reply = read_reply(sock)
        except socket.timeout:
            final_reply = ""

        if final_reply:
            print(final_reply)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Generate and send sine-wave tones to the ESP32 audio playback device.",
    )
    parser.add_argument("host", help="ESP32 host or IP address, for example audio-alert.local")
    parser.add_argument(
        "tones",
        nargs="+",
        type=parse_tone,
        metavar="FREQ:DURATION",
        help="Tone frequency in Hz and duration in seconds, for example 440:1.0",
    )
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help="TCP port")
    parser.add_argument(
        "-r",
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help="Sample rate in Hz, 8000 to 48000",
    )
    parser.add_argument(
        "-v",
        "--volume",
        type=float,
        default=DEFAULT_VOLUME,
        help="Volume from 0.0 to 1.0",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=0.0,
        help="Silence between tones in seconds",
    )
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if not 8000 <= args.sample_rate <= 48000:
        parser.error("--sample-rate must be between 8000 and 48000")
    if not 0.0 <= args.volume <= 1.0:
        parser.error("--volume must be between 0.0 and 1.0")
    if args.gap < 0:
        parser.error("--gap must be 0 or greater")

    chunks = []
    silence = b"\x00\x00" * int(round(args.gap * args.sample_rate))

    for index, (frequency, duration) in enumerate(args.tones):
        if index > 0 and silence:
            chunks.append(silence)
        chunks.append(generate_tone_pcm(frequency, duration, args.sample_rate, args.volume))

    pcm_bytes = b"".join(chunks)
    total_seconds = len(pcm_bytes) / 2 / args.sample_rate

    print(
        f"Sending {len(args.tones)} tone(s), {total_seconds:.3f}s, "
        f"{args.sample_rate} Hz mono PCM16"
    )
    send_pcm(args.host, args.port, args.sample_rate, pcm_bytes)


if __name__ == "__main__":
    main()
