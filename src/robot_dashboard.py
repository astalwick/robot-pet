#!/usr/bin/env python3
"""Read-only SSH dashboard for robot telemetry."""

from __future__ import annotations

import argparse
import subprocess
import threading
from typing import Any

from rich.table import Table
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


def fmt(value: Any, suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def status_style(status: str) -> str:
    if status in {"ok", "live"}:
        return "green"
    if status in {"low", "stale", "warning"}:
        return "yellow"
    return "red"


class RobotDashboard(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #status {
        height: 1;
        padding: 0 1;
    }

    #main {
        height: 2fr;
    }

    #left, #right {
        width: 1fr;
    }

    Static, DataTable, RichLog {
        border: solid $primary;
        margin: 0 1 1 1;
    }

    #logs {
        height: 1fr;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, socket_path: str):
        super().__init__()
        self.socket_path = socket_path
        self.last_snapshot: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Connecting to telemetry...", id="status")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("Pi health unavailable", id="pi")
                yield Static("Motor battery unavailable", id="battery")
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
        status = self.query_one("#status", Static)
        status.update(
            f"Telemetry {self.socket_path} | gamepad: [{status_style(gamepad_status)}]{gamepad_status}[/] | "
            f"system: [{status_style(system_status)}]{system_status}[/]"
        )

        self._render_pi(snapshot.get("pi") or {})
        self._render_battery(snapshot.get("motor_battery") or {})
        self._render_controller(snapshot.get("controller") or {})
        self._render_wheels(snapshot.get("wheels") or {})

    def _source_label(self, source: dict[str, Any]) -> str:
        return "stale" if source.get("stale", True) else "live"

    def _render_pi(self, pi: dict[str, Any]):
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Uptime", fmt(pi.get("uptime_seconds"), "s", digits=0))
        table.add_row("Load", fmt(pi.get("load_1m")))
        table.add_row("Memory", f"{fmt(pi.get('memory_used_mb'), ' MB', 0)} / {fmt(pi.get('memory_total_mb'), ' MB', 0)}")
        table.add_row("Disk /", fmt(pi.get("disk_used_percent"), "%"))
        table.add_row("SoC temp", fmt(pi.get("soc_temp_c"), " C"))
        table.add_row("Throttled", fmt(pi.get("throttled_flags")))
        table.add_row("Power bank", "charge unavailable")
        self.query_one("#pi", Static).update(table)

    def _render_battery(self, battery: dict[str, Any]):
        status = battery.get("status", "unknown")
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Pack", fmt(battery.get("pack_voltage"), " V", 2))
        table.add_row("Cell est.", fmt(battery.get("cell_voltage"), " V", 2))
        table.add_row("Status", f"[{status_style(status)}]{status}[/]")
        self.query_one("#battery", Static).update(table)

    def _render_controller(self, controller: dict[str, Any]):
        table = self.query_one("#controller", DataTable)
        table.clear()
        table.add_row("Connected", "yes" if controller.get("connected") else "no")
        for name in ("left_stick_x", "left_stick_y", "right_stick_x", "right_stick_y", "left_trigger", "right_trigger"):
            table.add_row(name, self._meter(controller.get(name)))
        table.add_row("D-pad", f"{controller.get('dpad_x', 0)}, {controller.get('dpad_y', 0)}")
        pressed = [name.upper() for name, value in (controller.get("buttons") or {}).items() if value]
        table.add_row("Buttons", " ".join(pressed) if pressed else "-")

    def _render_wheels(self, wheels: dict[str, Any]):
        table = self.query_one("#wheels", DataTable)
        table.clear()
        read_status = "ok" if wheels.get("read_ok") else "read failed"
        for side in ("left", "right"):
            table.add_row(
                side,
                fmt(wheels.get(f"{side}_command"), digits=2),
                fmt(wheels.get(f"{side}_target_qpps"), digits=0),
                fmt(wheels.get(f"{side}_actual_qpps"), digits=0),
                fmt(wheels.get(f"{side}_error_qpps"), digits=0),
                fmt(wheels.get(f"{side}_current_amps"), " A", 2),
                read_status,
            )

    def _meter(self, value: float | None) -> str:
        if value is None:
            return "unavailable"
        width = 12
        filled = int(abs(value) * width)
        bar = "#" * filled + "-" * (width - filled)
        return f"{value:+.2f} [{bar}]"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only robot telemetry dashboard.")
    parser.add_argument("--socket", default=DEFAULT_SUBSCRIBE_SOCKET, help="Telemetry hub subscriber socket")
    return parser


def main():
    args = build_parser().parse_args()
    RobotDashboard(args.socket).run()


if __name__ == "__main__":
    main()
