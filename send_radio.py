#!/usr/bin/env python3
"""Stream an internet radio station to the ESP32 AUD1 audio receiver.

The ESP32 receiver accepts an AUD1 header with payload length 0 to mean
"stream until the TCP connection closes". This script uses ffmpeg to decode an
internet radio stream into signed 16-bit little-endian PCM and forwards that
PCM to the audio playback device as it is produced.
"""

import argparse
import html
import http.cookiejar
import json
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from aud1_protocol import DEFAULT_PORT, DEFAULT_SAMPLE_RATE, make_header, read_reply


DEFAULT_HOST = "audio-alert.local"
DEFAULT_VOLUME = 0.85
DEFAULT_RECONNECT_DELAY = 3.0
DEFAULT_DIRECTORY_BASE = "https://de1.api.radio-browser.info"
DEFAULT_IHEART_SEARCH_BASE = "https://us.api.iheart.com/api/v3/search/all"
DEFAULT_RADIO_LOCATOR_BASE = "https://radio-locator.com"
DEFAULT_COUNTRY_CODE = "US"
DEFAULT_SEARCH_LIMIT = 12
DEFAULT_PROBE_TIMEOUT = 5.0
READ_SIZE = 4096

STATIONS = {
    "wbal": {
        "name": "WBAL NewsRadio 1090 AM / 101.5 FM",
        "url": "https://playerservices.streamtheworld.com/api/livestream-redirect/WBALAMAAC.aac",
    },
}


def is_stream_url(value):
    return value.startswith(("http://", "https://"))


def fetch_json(url, timeout):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "audio-alert-radio/1.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def strip_html(value):
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def is_likely_call_sign(value):
    return re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{1,11}", value.strip()) is not None


def radio_locator_iheart_stream_url(url):
    match = re.search(r"/live/[^/?#]+-(\d+)(?:[/?#]|$)", url)
    if not match:
        return ""
    return iheart_stream_url(match.group(1))


def fetch_radio_locator_matches(args):
    if not is_likely_call_sign(args.station):
        return []

    base = args.radio_locator_base.rstrip("/")
    params = urllib.parse.urlencode(
        {
            "call": args.station.upper(),
            "sr": "Y",
            "s": "C",
        }
    )
    search_url = f"{base}/cgi-bin/finder?{params}"

    try:
        cookies = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "Mozilla/5.0", "Referer": f"{base}/cgi-bin/home"}
        opener.open(urllib.request.Request(f"{base}/cgi-bin/home", headers=headers), timeout=args.directory_timeout).read()
        page = opener.open(
            urllib.request.Request(search_url, headers=headers),
            timeout=args.directory_timeout,
        ).read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Radio-Locator lookup failed: {exc}") from exc

    title_match = re.search(r"<title>(.*?)</title>", page, re.I | re.S)
    if not title_match:
        return []

    title = strip_html(title_match.group(1))
    if title == "Radio-Locator.com":
        return []

    audio_match = re.search(
        r"<b>\s*Audio Feed:\s*</b>.*?<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        page,
        re.I | re.S,
    )
    if not audio_match:
        return []

    audio_url = html.unescape(audio_match.group(1))
    audio_label = strip_html(audio_match.group(2))
    if audio_url.startswith("/"):
        audio_url = urllib.parse.urljoin(base, audio_url)

    try:
        redirect_response = opener.open(
            urllib.request.Request(audio_url, headers={"User-Agent": "Mozilla/5.0", "Referer": search_url}),
            timeout=args.directory_timeout,
        )
        final_url = redirect_response.geturl()
        content_type = redirect_response.headers.get("Content-Type", "")
        redirect_response.read(512)
        redirect_response.close()
    except OSError as exc:
        raise RuntimeError(f"Radio-Locator audio link failed: {exc}") from exc

    stream_url = ""
    if "audio" in content_type.lower():
        stream_url = final_url
    elif "iheart.com/live/" in final_url:
        stream_url = radio_locator_iheart_stream_url(final_url)
    elif final_url.endswith((".m3u8", ".pls", ".mp3", ".aac")):
        stream_url = final_url

    if not stream_url:
        return []

    call_match = re.search(r"\b([A-Z]{2,5}(?:-[AF]M)?)\b", title)
    frequency_match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:kHz|MHz)\b", title)
    city_match = re.search(r"-\s*([^,-]+),\s*([A-Z]{2})\b", title)

    return [
        {
            "source": "Radio-Locator",
            "name": title,
            "url_resolved": stream_url,
            "countrycode": "US",
            "state": city_match.group(2) if city_match else "",
            "codec": "AAC" if "iHeart" in audio_label else "?",
            "bitrate": "?",
            "clickcount": 0,
            "votes": 0,
            "lastcheckok": True,
            "callletters": call_match.group(1) if call_match else args.station.upper(),
            "frequency": frequency_match.group(1) if frequency_match else "",
            "description": audio_label,
        }
    ]


def fetch_radio_browser_matches(args):
    params = {
        "name": args.station,
        "hidebroken": "true",
        "order": "clickcount",
        "reverse": "true",
        "limit": str(args.search_limit),
    }
    if args.country_code:
        params["countrycode"] = args.country_code.upper()

    url = f"{args.directory_base.rstrip('/')}/json/stations/search?{urllib.parse.urlencode(params)}"
    try:
        return fetch_json(url, args.directory_timeout)
    except OSError as exc:
        raise RuntimeError(f"Radio Browser lookup failed: {exc}") from exc


def iheart_stream_url(station_id):
    return f"https://stream.revma.ihrhls.com/zc{station_id}"


def fetch_iheart_matches(args):
    params = {
        "keywords": args.station,
        "limit": str(args.search_limit),
    }
    url = f"{args.iheart_search_base}?{urllib.parse.urlencode(params)}"

    try:
        data = fetch_json(url, args.directory_timeout)
    except OSError as exc:
        raise RuntimeError(f"iHeart lookup failed: {exc}") from exc

    stations = data.get("results", {}).get("stations", [])
    matches = []
    for station in stations:
        station_id = station.get("id")
        if station_id is None:
            continue

        matches.append(
            {
                "source": "iHeart",
                "name": station.get("name", ""),
                "url_resolved": iheart_stream_url(station_id),
                "countrycode": "US",
                "state": "",
                "codec": "AAC",
                "bitrate": "?",
                "clickcount": 0,
                "votes": 0,
                "lastcheckok": True,
                "callletters": station.get("callLetters", ""),
                "frequency": station.get("frequency", ""),
                "description": station.get("description", ""),
            }
        )

    return matches


def fetch_directory_matches(args):
    errors = []
    matches = []

    for fetcher in (fetch_radio_locator_matches, fetch_iheart_matches, fetch_radio_browser_matches):
        try:
            matches.extend(fetcher(args))
        except RuntimeError as exc:
            errors.append(str(exc))

    if matches:
        return matches
    if errors:
        raise RuntimeError("; ".join(errors))
    return []


def dedupe_matches(matches):
    deduped = {}
    for station in matches:
        url = stream_url_from_match(station)
        key = url.lower() if url else (station.get("source", ""), station.get("name", ""))
        current = deduped.get(key)
        if current is None or station_score("", station) > station_score("", current):
            deduped[key] = station
    return list(deduped.values())


def probe_stream_url(url, timeout):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Range": "bytes=0-1023",
            "Icy-MetaData": "0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            sample = response.read(256)
    except OSError as exc:
        return False, str(exc)

    if "text/html" in content_type:
        return False, f"HTML response ({content_type})"
    if not sample:
        return False, "empty response"
    if sample.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return False, "HTML response"

    return True, ""


def playable_matches(args, matches):
    if not args.probe_streams:
        return matches

    playable = []
    for station in matches:
        url = stream_url_from_match(station)
        ok, error = probe_stream_url(url, args.probe_timeout)
        if ok:
            station["probe_ok"] = True
            playable.append(station)
        else:
            station["probe_error"] = error

    return playable


def stream_url_from_match(station):
    return station.get("url_resolved") or station.get("url") or ""


def station_score(query, station):
    query_lower = query.lower()
    name = station.get("name", "")
    name_lower = name.lower()
    tags_lower = station.get("tags", "").lower()
    call_letters = station.get("callletters", "").lower()
    call_sign = call_letters.split("-", 1)[0]
    score = 0

    if call_sign == query_lower:
        score += 2000
    if call_letters.startswith(query_lower):
        score += 1500
    if name_lower == query_lower:
        score += 1000
    if query_lower in name_lower.split():
        score += 500
    if query_lower in name_lower:
        score += 250
    if query_lower in tags_lower:
        score += 50
    if station.get("lastcheckok"):
        score += 100
    if stream_url_from_match(station):
        score += 100

    try:
        score += min(int(station.get("clickcount", 0)), 500)
    except (TypeError, ValueError):
        pass

    try:
        score += min(int(station.get("votes", 0)), 250)
    except (TypeError, ValueError):
        pass

    if station.get("source") == "iHeart":
        try:
            score += int(float(station.get("frequency") or 0))
        except (TypeError, ValueError):
            pass
    if station.get("source") == "Radio-Locator":
        score += 1800

    return score


def usable_matches(matches):
    return [station for station in matches if stream_url_from_match(station)]


def format_station_match(index, station):
    name = station.get("name", "unknown")
    country = station.get("countrycode") or station.get("country") or ""
    state = station.get("state") or ""
    source = station.get("source", "Radio Browser")
    call_letters = station.get("callletters", "")
    codec = station.get("codec") or "?"
    bitrate = station.get("bitrate") or "?"
    url = stream_url_from_match(station)
    location = ", ".join(part for part in (state, country) if part)
    suffix = f" ({location})" if location else ""
    calls = f" {call_letters}" if call_letters else ""
    return f"{index:2}. {name}{calls}{suffix} [{source}; {codec} {bitrate} kbps] {url}"


def resolve_station(args):
    value = args.station.strip()
    key = value.lower()

    if is_stream_url(value):
        return value, value

    if not args.directory_only and key in STATIONS:
        station = STATIONS[key]
        return station["url"], station["name"]

    raw_matches = dedupe_matches(usable_matches(fetch_directory_matches(args)))
    matches = playable_matches(args, raw_matches)
    matches.sort(key=lambda station: station_score(value, station), reverse=True)

    if args.show_matches:
        if not matches:
            if raw_matches and args.probe_streams:
                print(f"No playable directory matches for {value!r}")
            else:
                print(f"No directory matches for {value!r}")
            return None, None
        for index, station in enumerate(matches, start=1):
            print(format_station_match(index, station))
        return None, None

    if not matches:
        if raw_matches and args.probe_streams:
            raise RuntimeError(f"directory matches found for {value!r}, but none passed stream probing")
        raise RuntimeError(f"no radio directory matches found for {value!r}")

    station = matches[0]
    name = station.get("name") or value
    url = stream_url_from_match(station)
    print(f"Directory selected: {format_station_match(1, station)}")
    return url, name


def build_ffmpeg_command(ffmpeg, url, sample_rate, volume):
    volume_filter = f"volume={volume}" if volume != 1.0 else "anull"
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "10",
        "-i",
        url,
        "-vn",
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


def start_ffmpeg(ffmpeg, url, sample_rate, volume):
    command = build_ffmpeg_command(ffmpeg, url, sample_rate, volume)
    try:
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required and was not found on PATH") from exc


def close_ffmpeg(process):
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def read_ffmpeg_error(process):
    if not process.stderr:
        return ""

    try:
        return process.stderr.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def stream_once(args, url, station_name):
    process = start_ffmpeg(args.ffmpeg, url, args.sample_rate, args.volume)

    try:
        with socket.create_connection((args.host, args.port), timeout=10) as sock:
            sock.settimeout(10)
            sock.sendall(make_header(args.sample_rate, 1, b""))

            reply = read_reply(sock)
            print(reply)
            if not reply.startswith("OK"):
                raise RuntimeError(f"device rejected stream: {reply}")

            sock.settimeout(None)
            print(f"Streaming {station_name} to {args.host}:{args.port}; press Ctrl+C to stop")

            while True:
                if process.stdout is None:
                    raise RuntimeError("ffmpeg stdout pipe was not opened")

                chunk = process.stdout.read(READ_SIZE)
                if chunk:
                    sock.sendall(chunk)
                    continue

                return_code = process.poll()
                if return_code is None:
                    continue

                error = read_ffmpeg_error(process)
                if return_code != 0:
                    raise RuntimeError(f"ffmpeg stopped with exit code {return_code}: {error}")
                break
    finally:
        close_ffmpeg(process)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Stream internet radio to the ESP32 audio playback device.",
    )
    parser.add_argument(
        "station",
        nargs="?",
        help="Station key/query such as wor or wbal, or a direct http(s) stream URL",
    )
    parser.add_argument(
        "host",
        nargs="?",
        default=DEFAULT_HOST,
        help=f"ESP32 host or IP address, default: {DEFAULT_HOST}",
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
        help="ffmpeg volume multiplier, for example 0.5, 1.0, or 1.5",
    )
    parser.add_argument(
        "--ffmpeg",
        default="ffmpeg",
        help="Path to ffmpeg executable if it is not on PATH",
    )
    parser.add_argument(
        "--list-stations",
        action="store_true",
        help="List built-in station keys and exit",
    )
    parser.add_argument(
        "--show-matches",
        action="store_true",
        help="Show directory matches for the station query and exit",
    )
    parser.add_argument(
        "--directory-only",
        action="store_true",
        help="Resolve built-in station keys through the online directory too",
    )
    parser.add_argument(
        "--directory-base",
        default=DEFAULT_DIRECTORY_BASE,
        help="Radio Browser API base URL",
    )
    parser.add_argument(
        "--iheart-search-base",
        default=DEFAULT_IHEART_SEARCH_BASE,
        help="iHeart public search API URL",
    )
    parser.add_argument(
        "--radio-locator-base",
        default=DEFAULT_RADIO_LOCATOR_BASE,
        help="Radio-Locator base URL for call-sign lookup",
    )
    parser.add_argument(
        "--country-code",
        default=DEFAULT_COUNTRY_CODE,
        help="Optional two-letter country filter for directory search; use an empty value for global search",
    )
    parser.add_argument(
        "--search-limit",
        type=int,
        default=DEFAULT_SEARCH_LIMIT,
        help="Maximum directory matches to fetch",
    )
    parser.add_argument(
        "--directory-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the radio directory",
    )
    parser.add_argument(
        "--no-stream-probe",
        dest="probe_streams",
        action="store_false",
        help="Do not test candidate stream URLs before selecting one",
    )
    parser.add_argument(
        "--probe-timeout",
        type=float,
        default=DEFAULT_PROBE_TIMEOUT,
        help="Seconds to wait when testing a candidate stream URL",
    )
    parser.set_defaults(probe_streams=True)
    parser.add_argument(
        "--reconnect",
        action="store_true",
        help="Reconnect after stream/device failures until Ctrl+C",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=DEFAULT_RECONNECT_DELAY,
        help="Seconds to wait before reconnecting when --reconnect is used",
    )
    return parser


def print_stations():
    for key, station in sorted(STATIONS.items()):
        print(f"{key:12} {station['name']}  {station['url']}")


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.list_stations:
        print_stations()
        return

    if args.station is None:
        parser.error("station is required unless --list-stations is used")

    if not 8000 <= args.sample_rate <= 48000:
        parser.error("--sample-rate must be between 8000 and 48000")
    if args.volume <= 0:
        parser.error("--volume must be greater than 0")
    if args.reconnect_delay < 0:
        parser.error("--reconnect-delay must be 0 or greater")
    if args.search_limit < 1:
        parser.error("--search-limit must be greater than 0")
    if args.directory_timeout <= 0:
        parser.error("--directory-timeout must be greater than 0")
    if args.probe_timeout <= 0:
        parser.error("--probe-timeout must be greater than 0")

    url, station_name = resolve_station(args)
    if url is None:
        return

    while True:
        try:
            stream_once(args, url, station_name)
            if not args.reconnect:
                return
            print("stream ended; reconnecting")
        except KeyboardInterrupt:
            print()
            return
        except Exception as exc:
            if not args.reconnect:
                raise
            print(f"stream error: {exc}", file=sys.stderr)

        time.sleep(args.reconnect_delay)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
