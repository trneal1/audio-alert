#!/usr/bin/env python3
"""Send clock announcements to send_text_service.

The text service accepts one UTF-8 message per line. This script wakes on
configured minutes, formats the local time like "10:15 am", and submits that
line to the service.
"""

import argparse
import configparser
import re
import socket
import sys
import time
from datetime import datetime, timedelta

from send_text_service import DEFAULT_LISTEN_PORT


DEFAULT_HOST = "127.0.0.1"
DEFAULT_MINUTES = "00 15 30 45"
DEFAULT_HOURS = "6am - 10pm"
DEFAULT_TIMEOUT = 10.0
DEFAULT_CONFIG = "clock_announce.ini"
DEFAULT_NTP_SERVER = "pool.ntp.org"
DEFAULT_NTP_PORT = 123
DEFAULT_NTP_TIMEOUT = 5.0
DEFAULT_NTP_SYNC_INTERVAL = 3600.0
NTP_DELTA = 2208988800


def parse_minutes(value):
    minutes = []
    for part in re.split(r"[\s,]+", value.strip()):
        if not part:
            continue
        try:
            minute = int(part, 10)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid minute {part!r}") from exc
        if not 0 <= minute <= 59:
            raise argparse.ArgumentTypeError("minutes must be from 00 to 59")
        minutes.append(minute)

    if not minutes:
        raise argparse.ArgumentTypeError("at least one minute is required")

    return sorted(set(minutes))


def parse_hour_token(value):
    match = re.fullmatch(r"\s*(\d{1,2})(?::([0-5]\d))?\s*([ap]m)?\s*", value, re.I)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid hour {value!r}; use values like 6am, 22, or 10:30pm")

    hour = int(match.group(1), 10)
    minute = int(match.group(2) or "0", 10)
    suffix = (match.group(3) or "").lower()

    if suffix:
        if not 1 <= hour <= 12:
            raise argparse.ArgumentTypeError("12-hour times must use hours 1 through 12")
        if suffix == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    elif not 0 <= hour <= 23:
        raise argparse.ArgumentTypeError("24-hour times must use hours 0 through 23")

    return hour * 60 + minute


def parse_hours(value):
    parts = re.split(r"\s*-\s*", value.strip(), maxsplit=1)
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("hours must be a range like '6am - 10pm'")

    start = parse_hour_token(parts[0])
    end = parse_hour_token(parts[1])
    return start, end


def is_within_hours(now, hours):
    start, end = hours
    current = now.hour * 60 + now.minute

    if start <= end:
        return start <= current <= end

    return current >= start or current <= end


def format_time_text(now):
    hour = now.hour % 12 or 12
    suffix = "am" if now.hour < 12 else "pm"
    return f"{hour}:{now.minute:02d} {suffix}"


def fetch_ntp_time(server, port, timeout):
    packet = bytearray(48)
    packet[0] = 0x1B

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (server, port))
        data, _ = sock.recvfrom(48)

    if len(data) < 48:
        raise RuntimeError("short NTP response")

    seconds = int.from_bytes(data[40:44], "big")
    fraction = int.from_bytes(data[44:48], "big")
    return (seconds - NTP_DELTA) + (fraction / 2**32)


class NtpClock:
    def __init__(self, server, port, timeout, sync_interval):
        self.server = server
        self.port = port
        self.timeout = timeout
        self.sync_interval = sync_interval
        self.offset = 0.0
        self.last_sync = 0.0
        self.last_error = ""

    def sync(self, required=False):
        monotonic_now = time.monotonic()
        if not required and monotonic_now - self.last_sync < self.sync_interval:
            return

        try:
            ntp_time = fetch_ntp_time(self.server, self.port, self.timeout)
        except OSError as exc:
            self.last_error = str(exc)
            if required:
                raise RuntimeError(f"NTP sync failed: {exc}") from exc
            print(f"warning: NTP sync failed; using existing clock offset: {exc}", file=sys.stderr)
            return

        self.offset = ntp_time - time.time()
        self.last_sync = monotonic_now
        self.last_error = ""
        print(f"NTP synced with {self.server}; offset {self.offset:+.3f}s")

    def now(self):
        self.sync()
        return datetime.fromtimestamp(time.time() + self.offset)

    def corrected_timestamp(self):
        return time.time() + self.offset


def read_config(path):
    config = configparser.ConfigParser()
    read_files = config.read(path)
    if not read_files:
        return {}

    section = config["clock"] if config.has_section("clock") else config["DEFAULT"]
    return {
        key.replace("-", "_"): value
        for key, value in section.items()
        if value != ""
    }


def send_text(host, port, text, timeout):
    with socket.create_connection((host, port), timeout=timeout) as sock:
        reader = sock.makefile("r", encoding="utf-8", newline="\n")
        writer = sock.makefile("w", encoding="utf-8", newline="\n")

        ready = reader.readline().strip()
        if not ready.startswith("READY"):
            raise RuntimeError(f"text service did not send READY: {ready}")

        writer.write(f"{text}\n")
        writer.flush()

        reply = reader.readline().strip()
        if not reply.startswith("QUEUED"):
            raise RuntimeError(f"text service rejected message: {reply}")

        return reply


def next_minute_boundary(now):
    return (now.replace(second=0, microsecond=0) + timedelta(minutes=1)).timestamp()


def run(args):
    announced_keys = set()
    clock = NtpClock(args.ntp_server, args.ntp_port, args.ntp_timeout, args.ntp_sync_interval)

    if args.no_ntp:
        clock.last_sync = time.monotonic()
    else:
        try:
            clock.sync(required=args.require_ntp)
        except RuntimeError as exc:
            raise RuntimeError(f"{exc}; use --no-require-ntp to fall back to system time") from exc

    while True:
        now = datetime.now() if args.no_ntp else clock.now()
        key = (now.year, now.month, now.day, now.hour, now.minute)

        if now.minute in args.minutes and is_within_hours(now, args.hours) and key not in announced_keys:
            text = format_time_text(now)
            if args.dry_run:
                print(f"would send: {text}")
            else:
                reply = send_text(args.host, args.port, text, args.timeout)
                print(f"sent {text!r}: {reply}")
            announced_keys.add(key)

        if len(announced_keys) > 256:
            cutoff = now - timedelta(days=1)
            announced_keys = {
                item
                for item in announced_keys
                if datetime(item[0], item[1], item[2], item[3], item[4]) >= cutoff
            }

        sleep_until = next_minute_boundary(now)
        current_timestamp = time.time() if args.no_ntp else clock.corrected_timestamp()
        time.sleep(max(1.0, sleep_until - current_timestamp))


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Send local time announcements such as '10:15 am' to send_text_service.",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"Optional INI config file, default: {DEFAULT_CONFIG}",
    )
    parser.add_argument("--host", default=None, help=f"send_text_service host, default: {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=None, help=f"send_text_service TCP port, default: {DEFAULT_LISTEN_PORT}")
    parser.add_argument(
        "--minutes",
        default=None,
        help=f"Minutes past the hour to announce, default: {DEFAULT_MINUTES!r}",
    )
    parser.add_argument(
        "--hours",
        default=None,
        help=f"Allowed local-time range, default: {DEFAULT_HOURS!r}",
    )
    parser.add_argument("--timeout", type=float, default=None, help=f"TCP timeout seconds, default: {DEFAULT_TIMEOUT}")
    parser.add_argument("--ntp-server", default=None, help=f"NTP server, default: {DEFAULT_NTP_SERVER}")
    parser.add_argument("--ntp-port", type=int, default=None, help=f"NTP UDP port, default: {DEFAULT_NTP_PORT}")
    parser.add_argument("--ntp-timeout", type=float, default=None, help=f"NTP timeout seconds, default: {DEFAULT_NTP_TIMEOUT}")
    parser.add_argument(
        "--ntp-sync-interval",
        type=float,
        default=None,
        help=f"Seconds between NTP syncs, default: {DEFAULT_NTP_SYNC_INTERVAL:g}",
    )
    parser.add_argument("--no-ntp", action="store_true", help="Use the local system clock without NTP correction")
    parser.add_argument(
        "--no-require-ntp",
        dest="require_ntp",
        action="store_false",
        help="Start even if the first NTP sync fails",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print announcements without contacting the service")
    parser.set_defaults(require_ntp=True)
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    config = read_config(args.config)

    args.host = args.host or config.get("host", DEFAULT_HOST)
    args.port = args.port if args.port is not None else int(config.get("port", DEFAULT_LISTEN_PORT))
    args.minutes = parse_minutes(args.minutes or config.get("minutes", DEFAULT_MINUTES))
    args.hours = parse_hours(args.hours or config.get("hours", DEFAULT_HOURS))
    args.timeout = args.timeout if args.timeout is not None else float(config.get("timeout", DEFAULT_TIMEOUT))
    args.ntp_server = args.ntp_server or config.get("ntp_server", DEFAULT_NTP_SERVER)
    args.ntp_port = args.ntp_port if args.ntp_port is not None else int(config.get("ntp_port", DEFAULT_NTP_PORT))
    args.ntp_timeout = args.ntp_timeout if args.ntp_timeout is not None else float(config.get("ntp_timeout", DEFAULT_NTP_TIMEOUT))
    args.ntp_sync_interval = (
        args.ntp_sync_interval
        if args.ntp_sync_interval is not None
        else float(config.get("ntp_sync_interval", DEFAULT_NTP_SYNC_INTERVAL))
    )

    if args.port < 1 or args.port > 65535:
        parser.error("--port must be from 1 to 65535")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.ntp_port < 1 or args.ntp_port > 65535:
        parser.error("--ntp-port must be from 1 to 65535")
    if args.ntp_timeout <= 0:
        parser.error("--ntp-timeout must be greater than 0")
    if args.ntp_sync_interval <= 0:
        parser.error("--ntp-sync-interval must be greater than 0")

    run(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
