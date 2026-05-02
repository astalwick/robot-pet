#!/usr/bin/env python3
"""Local telemetry hub for the SSH robot dashboard."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lib.log import setup_logging
from telemetry.messages import decode_json_line, encode_json_line
from telemetry.paths import DEFAULT_PUBLISH_SOCKET, DEFAULT_SUBSCRIBE_SOCKET


DEFAULT_RATE_HZ = 5.0
DEFAULT_STALE_TIMEOUT = 1.0

log = setup_logging("robot-telemetry")


def read_text(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def run_command(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=0.2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def parse_meminfo(text: str) -> tuple[int | None, int | None]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            values[parts[0][:-1]] = int(parts[1])

    total_kb = values.get("MemTotal")
    available_kb = values.get("MemAvailable")
    if total_kb is None or available_kb is None:
        return None, None
    return (total_kb - available_kb) // 1024, total_kb // 1024


def parse_soc_temp(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value.startswith("temp="):
        value = value.removeprefix("temp=").removesuffix("'C")
        return float(value)
    return int(value) / 1000.0


def sample_pi_health(
    read_file: Callable[[str], str | None] = read_text,
    disk_usage: Callable[[str], Any] = shutil.disk_usage,
    command_runner: Callable[[list[str]], str | None] = run_command,
) -> dict[str, Any]:
    uptime = read_file("/proc/uptime")
    load = read_file("/proc/loadavg")
    meminfo = read_file("/proc/meminfo")
    disk = disk_usage("/")

    used_mb, total_mb = parse_meminfo(meminfo or "")
    temp_output = command_runner(["vcgencmd", "measure_temp"])
    if temp_output is None:
        temp_output = read_file("/sys/class/thermal/thermal_zone0/temp")

    throttled = command_runner(["vcgencmd", "get_throttled"])
    if throttled is not None and "=" in throttled:
        throttled = throttled.split("=", 1)[1]

    return {
        "uptime_seconds": int(float(uptime.split()[0])) if uptime else None,
        "load_1m": float(load.split()[0]) if load else None,
        "memory_used_mb": used_mb,
        "memory_total_mb": total_mb,
        "disk_used_percent": round((disk.used / disk.total) * 100, 1),
        "soc_temp_c": parse_soc_temp(temp_output),
        "throttled_flags": throttled,
        "power_bank_charge": None,
    }


class TelemetryHub:
    def __init__(
        self,
        publish_socket: str = DEFAULT_PUBLISH_SOCKET,
        subscribe_socket: str = DEFAULT_SUBSCRIBE_SOCKET,
        rate_hz: float = DEFAULT_RATE_HZ,
        stale_timeout: float = DEFAULT_STALE_TIMEOUT,
        sampler: Callable[[], dict[str, Any]] = sample_pi_health,
    ):
        self.publish_socket = publish_socket
        self.subscribe_socket = subscribe_socket
        self.interval = 1.0 / rate_hz
        self.stale_timeout = stale_timeout
        self.sampler = sampler
        self.latest: dict[str, dict[str, Any]] = {}
        self.system_last_seen: float | None = None
        self.system_health: dict[str, Any] = {}
        self.subscribers: set[asyncio.StreamWriter] = set()
        self._servers: list[asyncio.AbstractServer] = []
        self._broadcast_task: asyncio.Task | None = None

    async def run(self):
        await self.start()
        try:
            await asyncio.Future()
        finally:
            await self.stop()

    async def start(self):
        self._prepare_socket(self.publish_socket)
        self._prepare_socket(self.subscribe_socket)
        pub_server = await asyncio.start_unix_server(self._handle_publisher, path=self.publish_socket)
        sub_server = await asyncio.start_unix_server(self._handle_subscriber, path=self.subscribe_socket)
        self._servers = [pub_server, sub_server]
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        log.info("telemetry hub listening on %s and %s", self.publish_socket, self.subscribe_socket)

    async def stop(self):
        if self._broadcast_task is not None:
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass
        for server in self._servers:
            server.close()
            await server.wait_closed()
        for writer in list(self.subscribers):
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        for socket_path in (self.publish_socket, self.subscribe_socket):
            try:
                os.unlink(socket_path)
            except FileNotFoundError:
                pass

    async def _handle_publisher(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while line := await reader.readline():
                message = decode_json_line(line)
                source = message.get("source")
                if message.get("type") == "source_update" and source:
                    self.latest[source] = {"last_seen": time.time(), "data": message}
        except Exception as exc:
            log.warning("publisher update failed: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _handle_subscriber(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.subscribers.add(writer)
        try:
            writer.write(encode_json_line(self.build_snapshot()))
            await writer.drain()
            await reader.read()
        except OSError:
            pass
        finally:
            self.subscribers.discard(writer)
            writer.close()

    async def _broadcast_loop(self):
        while True:
            started = time.monotonic()
            self._sample_system_health()
            await self.broadcast(self.build_snapshot())
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, self.interval - elapsed))

    async def broadcast(self, snapshot: dict[str, Any]):
        dead: list[asyncio.StreamWriter] = []
        encoded = encode_json_line(snapshot)
        for writer in self.subscribers:
            try:
                writer.write(encoded)
                await writer.drain()
            except (ConnectionError, OSError):
                dead.append(writer)
        for writer in dead:
            self.subscribers.discard(writer)

    def build_snapshot(self) -> dict[str, Any]:
        now = time.time()
        gamepad = self.latest.get("gamepad_teleop")
        gamepad_data = gamepad["data"] if gamepad else {}

        return {
            "type": "snapshot",
            "time": now,
            "sources": {
                "gamepad_teleop": self._source_status(gamepad, now),
                "system": self._system_status(now),
            },
            "controller": gamepad_data.get("controller"),
            "wheels": gamepad_data.get("wheels"),
            "motor_battery": gamepad_data.get("motor_battery"),
            "link_loop": gamepad_data.get("link_loop"),
            "pi": self.system_health,
        }

    def _source_status(self, source: dict[str, Any] | None, now: float) -> dict[str, Any]:
        last_seen = source["last_seen"] if source else None
        return {
            "last_seen": last_seen,
            "stale": last_seen is None or (now - last_seen) > self.stale_timeout,
        }

    def _system_status(self, now: float) -> dict[str, Any]:
        return {
            "last_seen": self.system_last_seen,
            "stale": self.system_last_seen is None or (now - self.system_last_seen) > self.stale_timeout,
        }

    def _sample_system_health(self):
        try:
            self.system_health = self.sampler()
            self.system_last_seen = time.time()
        except Exception as exc:
            log.warning("system health sample failed: %s", exc)

    def _prepare_socket(self, socket_path: str):
        Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot telemetry hub.")
    parser.add_argument("--publish-socket", default=DEFAULT_PUBLISH_SOCKET)
    parser.add_argument("--subscribe-socket", default=DEFAULT_SUBSCRIBE_SOCKET)
    parser.add_argument("--rate-hz", type=float, default=DEFAULT_RATE_HZ)
    parser.add_argument("--stale-timeout", type=float, default=DEFAULT_STALE_TIMEOUT)
    return parser


def main():
    args = build_parser().parse_args()
    hub = TelemetryHub(
        publish_socket=args.publish_socket,
        subscribe_socket=args.subscribe_socket,
        rate_hz=args.rate_hz,
        stale_timeout=args.stale_timeout,
    )
    asyncio.run(hub.run())


if __name__ == "__main__":
    main()
