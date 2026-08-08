#!/usr/bin/env python3
"""Send generated sine-wave tones to the ESP32 AUD1 TCP audio receiver."""

import argparse
import math
import sys
from array import array

from aud1_protocol import DEFAULT_PORT, DEFAULT_SAMPLE_RATE, send_pcm

DEFAULT_VOLUME = 0.35
DEFAULT_HOST = "audio-alert.local"
DEFAULT_WOBBLES = 1


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
        raise argparse.ArgumentTypeError("frequency must be a number") from exc

    if start_frequency <= 0 or end_frequency <= 0:
        raise argparse.ArgumentTypeError("frequency must be greater than 0")

    return start_frequency, end_frequency


def parse_tone(value):
    """Parse FREQ:DURATION, START-END:DURATION, or comma variants."""
    separator = ":" if ":" in value else ","
    parts = value.split(separator)

    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            "tone must be FREQ:DURATION or START-END:DURATION, for example 440:1.5 or 440-880:1.5"
        )

    try:
        start_frequency, end_frequency = parse_frequency_range(parts[0])
        duration = float(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("frequency and duration must be numbers") from exc

    if duration <= 0:
        raise argparse.ArgumentTypeError("duration must be greater than 0")

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


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Generate and send sine-wave tones to the ESP32 audio playback device.",
    )
    parser.add_argument(
        "host",
        nargs="?",
        default=DEFAULT_HOST,
        help=f"ESP32 host or IP address, default: {DEFAULT_HOST}",
    )
    parser.add_argument(
        "tones",
        nargs="+",
        type=parse_tone,
        metavar="FREQ:DURATION",
        help="Tone frequency in Hz and duration in seconds; use START-END:DURATION to ramp",
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
    parser.add_argument(
        "-w",
        "--wobbles",
        type=int,
        default=DEFAULT_WOBBLES,
        help="Number of times to play the tone sequence",
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
    if args.wobbles < 1:
        parser.error("--wobbles must be 1 or greater")

    tones = expand_wobbles(args.tones, args.wobbles)
    pcm_bytes = build_tone_sequence_pcm(tones, args.sample_rate, args.volume, args.gap)
    total_seconds = len(pcm_bytes) / 2 / args.sample_rate

    print(
        f"Sending {len(args.tones)} tone(s) x {args.wobbles} wobble(s), {total_seconds:.3f}s, "
        f"{args.sample_rate} Hz mono PCM16"
    )
    send_pcm(args.host, args.port, args.sample_rate, pcm_bytes)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
