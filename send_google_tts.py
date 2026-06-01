#!/usr/bin/env python3
"""Send a Google Translate TTS voice message to the ESP32 AUD1 audio receiver.

This uses Google's unofficial/free translate_tts endpoint, then uses ffmpeg to
convert the returned MP3 into signed 16-bit little-endian mono PCM.
"""

import argparse
import subprocess
import sys
import urllib.parse
import urllib.request

from send_tone import DEFAULT_PORT, DEFAULT_SAMPLE_RATE, send_pcm


GOOGLE_TTS_URL = "https://translate.google.com/translate_tts"
DEFAULT_TEXT = "This is a test voice message from the audio alert device."


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


def mp3_to_pcm16(mp3_bytes, sample_rate, volume):
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


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Send a Google TTS voice message to the ESP32 audio playback device.",
    )
    parser.add_argument("host", help="ESP32 host or IP address, for example audio-alert.local")
    parser.add_argument(
        "text",
        nargs="*",
        help="Message text. If omitted, a default test message is sent.",
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
        "-l",
        "--language",
        default="en",
        help="Google TTS language code, for example en, en-US, es, fr",
    )
    parser.add_argument(
        "-v",
        "--volume",
        type=float,
        default=0.85,
        help="ffmpeg volume multiplier, for example 0.5, 1.0, or 1.5",
    )
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if not 8000 <= args.sample_rate <= 48000:
        parser.error("--sample-rate must be between 8000 and 48000")
    if args.volume <= 0:
        parser.error("--volume must be greater than 0")

    text = " ".join(args.text).strip() or DEFAULT_TEXT

    print(f"Fetching Google TTS audio: {text}")
    mp3_bytes = fetch_google_tts_mp3(text, args.language)
    print(f"Converting {len(mp3_bytes)} MP3 bytes to {args.sample_rate} Hz mono PCM16")
    pcm_bytes = mp3_to_pcm16(mp3_bytes, args.sample_rate, args.volume)
    print(f"Sending {len(pcm_bytes)} PCM bytes to {args.host}:{args.port}")
    send_pcm(args.host, args.port, args.sample_rate, pcm_bytes)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
