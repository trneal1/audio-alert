"""Client helpers for sending PCM audio to an AUD1 receiver."""

import socket
import struct


DEFAULT_PORT = 7777
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_SOCKET_TIMEOUT = 30


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


def read_reply(source):
    if hasattr(source, "readline"):
        data = source.readline()
    else:
        data = source.recv(128)
    return data.decode("utf-8", errors="replace").strip()


def send_pcm(host, port, sample_rate, pcm_bytes, progress=None):
    header = make_header(sample_rate, 1, pcm_bytes)

    with socket.create_connection((host, port), timeout=DEFAULT_SOCKET_TIMEOUT) as sock:
        sock.settimeout(DEFAULT_SOCKET_TIMEOUT)
        with sock.makefile("rb") as reader:
            if progress:
                progress("header", len(header))
            sock.sendall(header)

            reply = read_reply(reader)
            print(reply)
            if not reply.startswith("OK"):
                raise RuntimeError(f"device rejected stream: {reply}")

            if progress:
                progress("audio data", len(pcm_bytes))
            sock.sendall(pcm_bytes)

            received_reply = read_reply(reader)
            if received_reply:
                print(received_reply)
            if not received_reply.startswith("RECEIVED"):
                raise RuntimeError(f"device did not acknowledge audio receipt: {received_reply}")

            try:
                final_reply = read_reply(reader)
            except socket.timeout:
                final_reply = ""

            if final_reply:
                print(final_reply)
