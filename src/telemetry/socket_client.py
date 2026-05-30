"""Tiny Unix socket helpers for newline-delimited telemetry JSON."""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from typing import Any

from telemetry.messages import decode_json_line, encode_json_line


DEFAULT_VOICE_COMMAND_TIMEOUT_SECS = 2.0


def publish_message(socket_path: str, message: dict[str, Any], timeout: float = 0.01) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(socket_path)
            client.sendall(encode_json_line(message))
            return True
    except OSError:
        return False


def send_voice_command(
    socket_path: str,
    message: dict[str, Any],
    timeout: float = DEFAULT_VOICE_COMMAND_TIMEOUT_SECS,
) -> dict[str, Any] | None:
    """Send one voice command and read the service ack line."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(socket_path)
            client.sendall(encode_json_line(message))
            with client.makefile("rb") as file_obj:
                line = file_obj.readline()
            if not line:
                return None
            ack = decode_json_line(line)
            if not isinstance(ack, dict):
                return None
            return ack
    except OSError:
        return None


def subscribe(socket_path: str, reconnect_interval: float = 1.0) -> Iterator[dict[str, Any]]:
    while True:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(socket_path)
                file_obj = client.makefile("rb")
                for line in file_obj:
                    if line:
                        yield decode_json_line(line)
        except OSError:
            time.sleep(reconnect_interval)
