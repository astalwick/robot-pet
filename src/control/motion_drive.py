"""Unix-socket drive commands from gamepad-teleop to robot-motion."""

from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import dataclass
from typing import Any, Callable

from telemetry.messages import decode_json_line, encode_json_line


DRIVE_COMMAND_TYPE = "drive_command"


@dataclass(frozen=True)
class DriveCommand:
    left_qpps: int
    right_qpps: int
    controller: dict[str, Any]
    wheels: dict[str, Any]
    drive_tuning: dict[str, Any]
    drive_status: dict[str, Any]
    link_loop: dict[str, Any]

    def to_message(self) -> dict[str, Any]:
        return {
            "type": DRIVE_COMMAND_TYPE,
            "left_qpps": self.left_qpps,
            "right_qpps": self.right_qpps,
            "controller": self.controller,
            "wheels": self.wheels,
            "drive_tuning": self.drive_tuning,
            "drive_status": self.drive_status,
            "link_loop": self.link_loop,
        }

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> "DriveCommand":
        if message.get("type") != DRIVE_COMMAND_TYPE:
            raise ValueError("not a drive command")
        return cls(
            left_qpps=int(message["left_qpps"]),
            right_qpps=int(message["right_qpps"]),
            controller=dict(message["controller"]),
            wheels=dict(message["wheels"]),
            drive_tuning=dict(message["drive_tuning"]),
            drive_status=dict(message["drive_status"]),
            link_loop=dict(message["link_loop"]),
        )


class MotionDrivePublisher:
    """Persistent line publisher used by gamepad-teleop."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._socket: socket.socket | None = None

    def connect(self) -> bool:
        self.close()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(self.socket_path)
        except OSError:
            client.close()
            return False
        self._socket = client
        return True

    def send(self, command: DriveCommand) -> bool:
        if self._socket is None:
            return False
        try:
            self._socket.sendall(encode_json_line(command.to_message()))
            return True
        except OSError:
            self.close()
            return False

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None


class DriveCommandListener:
    """Accepts one gamepad publisher and stores the latest drive command."""

    def __init__(
        self,
        socket_path: str,
        on_command: Callable[[DriveCommand], None],
    ):
        self.socket_path = socket_path
        self.on_command = on_command
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: socket.socket | None = None

    def start(self) -> None:
        parent = os.path.dirname(self.socket_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o660)
        server.listen(1)
        server.settimeout(0.5)
        self._server = server
        self._thread = threading.Thread(target=self._serve, name="motion-drive", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            try:
                self._read_commands(conn)
            finally:
                conn.close()

    def _read_commands(self, conn: socket.socket) -> None:
        buffer = b""
        conn.settimeout(0.5)
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                if not line:
                    continue
                try:
                    message = decode_json_line(line)
                    command = DriveCommand.from_message(message)
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    continue
                self.on_command(command)
