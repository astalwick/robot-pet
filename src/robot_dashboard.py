#!/usr/bin/env python3
"""Read-only SSH dashboard for robot telemetry."""

from __future__ import annotations

import argparse
import subprocess
import threading
import time
from collections import deque
from collections.abc import Iterable
from typing import Any

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from telemetry.paths import DEFAULT_SUBSCRIBE_SOCKET
from telemetry.socket_client import subscribe


LOG_COMMAND = [
    "journalctl",
    "-u",
    "robot-telemetry",
    "-u",
    "gamepad-teleop",
    "-u",
    "robot-brain",
    "-f",
    "-n",
    "100",
]

HISTORY_LENGTH = 48
SPARK_CHARS = " .:-=+*#%@"
BAR_FULL = "#"
BAR_EMPTY = "."


def fmt(value: Any, suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def status_style(status: str) -> str:
    if status in {"ok", "live", "ready"}:
        return "bold green"
    if status in {"low", "stale", "warning", "caution"}:
        return "bold yellow"
    return "bold red"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def fmt_age(last_seen: float | None, now: float) -> str:
    if last_seen is None:
        return "never"
    age = max(0.0, now - last_seen)
    if age < 10:
        return f"{age:.1f}s ago"
    return f"{int(age)}s ago"


def sparkline(values: Iterable[float | None], width: int = 24) -> str:
    clean = [value for value in values if value is not None]
    if not clean:
        return "-" * width
    clean = clean[-width:]
    low = min(clean)
    high = max(clean)
    if low == high:
        return SPARK_CHARS[len(SPARK_CHARS) // 2] * len(clean)
    scale = len(SPARK_CHARS) - 1
    return "".join(SPARK_CHARS[int(((value - low) / (high - low)) * scale)] for value in clean)


def bar(value: float | None, limit: float = 1.0, width: int = 14) -> str:
    if value is None:
        return "-" * width
    ratio = min(1.0, abs(value) / limit)
    filled = int(round(ratio * width))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def signed_bar(value: float | None, limit: float, width: int = 15) -> str:
    if value is None:
        return "-" * width
    half = width // 2
    magnitude = min(half, int(round((abs(value) / limit) * half))) if limit else 0
    if value > 0:
        return BAR_EMPTY * half + "|" + BAR_FULL * magnitude + BAR_EMPTY * (half - magnitude)
    if value < 0:
        return BAR_EMPTY * (half - magnitude) + BAR_FULL * magnitude + "|" + BAR_EMPTY * half
    return BAR_EMPTY * half + "|" + BAR_EMPTY * half


def rich(markup: str) -> Text:
    return Text.from_markup(markup)


class RobotDashboard(App):
    CSS = """
    Screen {
        layout: vertical;
        background: #05080d;
        color: #d7faff;
    }

    #status {
        height: 3;
        padding: 0 1;
        border: tall #00d7ff;
        background: #07131f;
        color: #d7faff;
    }

    #main {
        height: 2fr;
    }

    #left, #right {
        width: 1fr;
    }

    Static, DataTable, RichLog {
        border: round #0f6f88;
        margin: 0 1 1 1;
        background: #071018;
    }

    #logs {
        height: 1fr;
        border: round #28394a;
    }

    DataTable {
        height: auto;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, socket_path: str):
        super().__init__()
        self.socket_path = socket_path
        self.last_snapshot: dict[str, Any] | None = None
        self.session_started = time.monotonic()
        self.history: dict[str, deque[float | None]] = {
            "pack_voltage": deque(maxlen=HISTORY_LENGTH),
            "left_current": deque(maxlen=HISTORY_LENGTH),
            "right_current": deque(maxlen=HISTORY_LENGTH),
            "left_actual": deque(maxlen=HISTORY_LENGTH),
            "right_actual": deque(maxlen=HISTORY_LENGTH),
            "left_error": deque(maxlen=HISTORY_LENGTH),
            "right_error": deque(maxlen=HISTORY_LENGTH),
        }
        self.max_current_amps = 0.0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("ROBO-PET // waiting for telemetry", id="status")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("CORE SYSTEMS // no telemetry", id="pi")
                yield Static("POWER RAIL // no telemetry", id="battery")
                yield DataTable(id="controller")
            with Vertical(id="right"):
                yield DataTable(id="wheels")
        yield RichLog(id="logs", wrap=True, highlight=True)
        yield Footer()

    def on_mount(self):
        self.title = "Robo-Pet Dashboard"
        controller = self.query_one("#controller", DataTable)
        controller.add_columns("Input", "Value")
        wheels = self.query_one("#wheels", DataTable)
        wheels.add_columns("Wheel", "Command", "Target", "Actual", "Error", "Current", "Read")

        threading.Thread(target=self._telemetry_thread, daemon=True).start()
        threading.Thread(target=self._logs_thread, daemon=True).start()

    def _telemetry_thread(self):
        for message in subscribe(self.socket_path, reconnect_interval=1.0):
            self.call_from_thread(self.apply_snapshot, message)

    def _logs_thread(self):
        logs = self.query_one("#logs", RichLog)
        try:
            process = subprocess.Popen(LOG_COMMAND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError as exc:
            self.call_from_thread(logs.write, f"journalctl unavailable: {exc}")
            return

        if process.stdout is None:
            return
        for line in process.stdout:
            self.call_from_thread(logs.write, line.rstrip())

    def apply_snapshot(self, snapshot: dict[str, Any]):
        self.last_snapshot = snapshot
        sources = snapshot.get("sources", {})
        gamepad_status = self._source_label(sources.get("gamepad_teleop", {}))
        system_status = self._source_label(sources.get("system", {}))
        self._record_history(snapshot)

        status = self.query_one("#status", Static)
        status.update(self._status_banner(snapshot, gamepad_status, system_status))

        self._render_pi(snapshot.get("pi") or {})
        self._render_battery(snapshot.get("motor_battery") or {})
        self._render_controller(snapshot.get("controller") or {})
        self._render_wheels(snapshot.get("wheels") or {})

    def _source_label(self, source: dict[str, Any]) -> str:
        return "stale" if source.get("stale", True) else "live"

    def _record_history(self, snapshot: dict[str, Any]):
        wheels = snapshot.get("wheels") or {}
        battery = snapshot.get("motor_battery") or {}
        self.history["pack_voltage"].append(battery.get("pack_voltage"))
        self.history["left_current"].append(wheels.get("left_current_amps"))
        self.history["right_current"].append(wheels.get("right_current_amps"))
        self.history["left_actual"].append(wheels.get("left_actual_qpps"))
        self.history["right_actual"].append(wheels.get("right_actual_qpps"))
        self.history["left_error"].append(wheels.get("left_error_qpps"))
        self.history["right_error"].append(wheels.get("right_error_qpps"))

        for value in (wheels.get("left_current_amps"), wheels.get("right_current_amps")):
            if value is not None:
                self.max_current_amps = max(self.max_current_amps, abs(value))

    def _status_banner(self, snapshot: dict[str, Any], gamepad_status: str, system_status: str) -> Table:
        now = time.time()
        sources = snapshot.get("sources", {})
        controller = snapshot.get("controller") or {}
        wheels = snapshot.get("wheels") or {}
        battery = snapshot.get("motor_battery") or {}
        pi = snapshot.get("pi") or {}
        drive_status, drive_notes = self._drive_status(gamepad_status, system_status, controller, wheels, battery, pi)

        table = Table.grid(expand=True)
        table.add_column(ratio=2)
        table.add_column(ratio=3)
        table.add_column(justify="right", ratio=2)
        table.add_row(
            Text("ROBO-PET // OPERATOR MODE", style="bold cyan"),
            Text(f"DRIVE: {drive_status}", style=status_style(drive_status)),
            Text(f"session {fmt_duration(time.monotonic() - self.session_started)}", style="magenta"),
        )
        table.add_row(
            f"telemetry {self.socket_path}",
            " | ".join(drive_notes),
            (
                f"gamepad {gamepad_status} ({fmt_age((sources.get('gamepad_teleop') or {}).get('last_seen'), now)}) "
                f"/ system {system_status} ({fmt_age((sources.get('system') or {}).get('last_seen'), now)})"
            ),
        )
        return table

    def _drive_status(
        self,
        gamepad_status: str,
        system_status: str,
        controller: dict[str, Any],
        wheels: dict[str, Any],
        battery: dict[str, Any],
        pi: dict[str, Any],
    ) -> tuple[str, list[str]]:
        notes: list[str] = []
        battery_status = battery.get("status", "unknown")
        throttled = pi.get("throttled_flags")
        controller_connected = controller.get("connected", False)
        wheels_read_ok = wheels.get("read_ok", False)

        if gamepad_status != "live" or system_status != "live":
            notes.append("telemetry stale")
            return "caution", notes
        if battery_status in {"critical", "unknown"}:
            notes.append(f"battery {battery_status}")
            return "hold", notes
        if not controller_connected:
            notes.append("controller offline")
            return "hold", notes

        if battery_status == "low":
            notes.append("battery low")
        if throttled not in {None, "0x0", "0"}:
            notes.append(f"pi throttled {throttled}")
        if not wheels_read_ok:
            notes.append("wheel readback missing")

        if notes:
            return "caution", notes
        return "ready", ["manual drive only"]

    def _render_pi(self, pi: dict[str, Any]):
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("[cyan]CORE SYSTEMS[/]", "[dim]pi rail / compute[/]")
        table.add_row("Uptime", fmt(pi.get("uptime_seconds"), "s", digits=0))
        table.add_row("Load", fmt(pi.get("load_1m")))
        table.add_row("Memory", f"{fmt(pi.get('memory_used_mb'), ' MB', 0)} / {fmt(pi.get('memory_total_mb'), ' MB', 0)}")
        table.add_row("Disk /", fmt(pi.get("disk_used_percent"), "%"))
        table.add_row("SoC temp", fmt(pi.get("soc_temp_c"), " C"))
        table.add_row("Throttle", self._throttle_label(pi.get("throttled_flags")))
        table.add_row("Power bank", "charge unavailable")
        self.query_one("#pi", Static).update(table)

    def _render_battery(self, battery: dict[str, Any]):
        status = battery.get("status", "unknown")
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("[cyan]POWER RAIL[/]", f"[{status_style(status)}]{status.upper()}[/]")
        table.add_row("Pack", fmt(battery.get("pack_voltage"), " V", 2))
        table.add_row("Cell est.", fmt(battery.get("cell_voltage"), " V", 2))
        table.add_row("Trace", sparkline(self.history["pack_voltage"]))
        table.add_row("Max current", fmt(self.max_current_amps, " A", 2))
        self.query_one("#battery", Static).update(table)

    def _render_controller(self, controller: dict[str, Any]):
        table = self.query_one("#controller", DataTable)
        table.clear()
        table.add_row(rich("[bold cyan]CONTROL DECK[/]"), rich("[dim]live input[/]"))
        table.add_row("Connected", rich("[green]yes[/]" if controller.get("connected") else "[red]no[/]"))
        for name in ("left_stick_x", "left_stick_y", "right_stick_x", "right_stick_y", "left_trigger", "right_trigger"):
            table.add_row(name, self._meter(controller.get(name)))
        table.add_row("D-pad", f"{controller.get('dpad_x', 0)}, {controller.get('dpad_y', 0)}")
        pressed = [name.upper() for name, value in (controller.get("buttons") or {}).items() if value]
        table.add_row("Buttons", " ".join(pressed) if pressed else "-")

    def _render_wheels(self, wheels: dict[str, Any]):
        table = self.query_one("#wheels", DataTable)
        table.clear()
        read_status = rich("[green]ok[/]" if wheels.get("read_ok") else "[red]read failed[/]")
        table.add_row(rich("[bold cyan]DRIVE BUS[/]"), "cmd", "target", "actual", "error", "current", read_status)
        for side in ("left", "right"):
            actual = wheels.get(f"{side}_actual_qpps")
            target = wheels.get(f"{side}_target_qpps")
            error = wheels.get(f"{side}_error_qpps")
            table.add_row(
                side,
                self._meter(wheels.get(f"{side}_command")),
                fmt(wheels.get(f"{side}_target_qpps"), digits=0),
                fmt(wheels.get(f"{side}_actual_qpps"), digits=0),
                self._error_label(error, target),
                fmt(wheels.get(f"{side}_current_amps"), " A", 2),
                read_status,
            )
            table.add_row(
                f"{side} trace",
                "",
                sparkline(self.history[f"{side}_actual"]),
                signed_bar(actual, max(abs(target or 0), 1)),
                sparkline(self.history[f"{side}_error"]),
                sparkline(self.history[f"{side}_current"]),
                "",
            )

    def _meter(self, value: float | None) -> str:
        if value is None:
            return "--"
        return f"{value:+.2f} [{bar(value)}]"

    def _error_label(self, error: float | None, target: float | None) -> Text | str:
        if error is None:
            return "--"
        style = "green"
        if target:
            ratio = abs(error) / max(abs(target), 1)
            if ratio > 0.25:
                style = "red"
            elif ratio > 0.10:
                style = "yellow"
        return rich(f"[{style}]{fmt(error, digits=0)}[/]")

    def _throttle_label(self, throttled: Any) -> str:
        if throttled in {None, "0x0", "0"}:
            return "[green]0x0[/]"
        return f"[yellow]{throttled}[/]"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only robot telemetry dashboard.")
    parser.add_argument("--socket", default=DEFAULT_SUBSCRIBE_SOCKET, help="Telemetry hub subscriber socket")
    return parser


def main():
    args = build_parser().parse_args()
    RobotDashboard(args.socket).run()


if __name__ == "__main__":
    main()
