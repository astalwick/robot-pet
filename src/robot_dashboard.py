#!/usr/bin/env python3
"""Read-only SSH dashboard for robot telemetry."""

from __future__ import annotations

import argparse
import subprocess
import threading
import time
from collections import deque
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, RichLog, Static

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

# Block characters for gauges
BAR_BLOCKS = " ▏▎▍▌▋▊▉█"
SPARK_BLOCKS = "▁▂▃▄▅▆▇█"

# Status glyphs
GLYPH_OK = "●"
GLYPH_WARN = "◐"
GLYPH_ERR = "○"
GLYPH_POWER = "⚡"
GLYPH_GAMEPAD = "◈"
GLYPH_WHEEL = "◎"
GLYPH_CPU = "▣"
GLYPH_SIGNAL = "◉"


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


def bar(value: float | None, limit: float = 1.0, width: int = 10) -> str:
    """Render a block-style gauge bar."""
    if value is None:
        return "░" * width
    ratio = min(1.0, abs(value) / limit)
    full_blocks = int(ratio * width)
    remainder = (ratio * width) - full_blocks
    partial_idx = int(remainder * (len(BAR_BLOCKS) - 1))
    
    result = "█" * full_blocks
    if full_blocks < width:
        result += BAR_BLOCKS[partial_idx]
        result += "░" * (width - full_blocks - 1)
    return result


def bipolar_bar(value: float | None, limit: float = 1.0, width: int = 10) -> str:
    """Render a center-zero gauge for bipolar values like joystick axes."""
    if value is None:
        return "░" * (width * 2 + 1)
    
    half_width = width
    ratio = max(-1.0, min(1.0, value / limit))
    
    if ratio >= 0:
        left = "░" * half_width
        right_fill = int(ratio * half_width)
        right = "█" * right_fill + "░" * (half_width - right_fill)
        center = "┃"
    else:
        left_fill = int(abs(ratio) * half_width)
        left = "░" * (half_width - left_fill) + "█" * left_fill
        right = "░" * half_width
        center = "┃"
    
    return left + center + right


def sparkline(values: deque[float | None], width: int = 12) -> str:
    """Render a sparkline from historical values."""
    clean = [v for v in values if v is not None]
    if not clean:
        return "─" * width
    
    # Take last `width` values
    recent = list(clean)[-width:]
    if len(recent) < 2:
        return "─" * width
    
    low, high = min(recent), max(recent)
    if high == low:
        return SPARK_BLOCKS[4] * len(recent) + "─" * (width - len(recent))
    
    result = ""
    for v in recent:
        idx = int((v - low) / (high - low) * (len(SPARK_BLOCKS) - 1))
        result += SPARK_BLOCKS[idx]
    
    return result + "─" * (width - len(recent))


class RobotDashboard(App):
    CSS = """
    Screen {
        layout: vertical;
        background: #020408;
        color: #c0e8f0;
    }

    #hud-header {
        height: 5;
        padding: 0 1;
        background: #04101a;
        border-bottom: solid #0a4f6a;
    }

    #main {
        height: 2fr;
    }

    #left, #right {
        width: 1fr;
    }

    .panel {
        border: heavy #0a4f6a;
        margin: 0 1 1 1;
        background: #040c14;
        padding: 0 1;
    }

    .panel-title {
        text-style: bold;
        color: #00d4ff;
    }

    #pi-panel {
        height: auto;
        border: heavy #0a5f5a;
    }

    #power-panel {
        height: auto;
        border: heavy #6a4f0a;
    }

    #controller-panel {
        height: auto;
        border: heavy #4a0f6a;
    }

    #wheels-panel {
        height: 1fr;
        border: heavy #0a4f6a;
    }

    #logs {
        height: 1fr;
        border: heavy #1a2a3a;
        margin: 0 1 1 1;
        background: #020a10;
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
        yield Static(self._hud_waiting(), id="hud-header")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("", id="pi-panel", classes="panel")
                yield Static("", id="power-panel", classes="panel")
                yield Static("", id="controller-panel", classes="panel")
            with Vertical(id="right"):
                yield Static("", id="wheels-panel", classes="panel")
        yield RichLog(id="logs", wrap=True, highlight=True)
        yield Footer()

    def _hud_waiting(self) -> Text:
        return Text.from_markup(
            "[bold cyan]╔══════════════════════════════════════════════════════════════════════════════╗[/]\n"
            "[bold cyan]║[/]  [bold white]R O B O - P E T[/]   [dim]awaiting telemetry link...[/]                              [bold cyan]║[/]\n"
            "[bold cyan]║[/]  [dim]───────────────────────────────────────────────────────────────────────[/]  [bold cyan]║[/]\n"
            "[bold cyan]╚══════════════════════════════════════════════════════════════════════════════╝[/]"
        )

    def on_mount(self):
        self.title = "Robo-Pet Dashboard"
        self._render_pi_waiting()
        self._render_power_waiting()
        self._render_controller_waiting()
        self._render_wheels_waiting()

        threading.Thread(target=self._telemetry_thread, daemon=True).start()
        threading.Thread(target=self._logs_thread, daemon=True).start()

    def _render_pi_waiting(self):
        self.query_one("#pi-panel", Static).update(
            Text.from_markup(f"[bold cyan]{GLYPH_CPU} CORE SYSTEMS[/]  [dim]awaiting link[/]")
        )

    def _render_power_waiting(self):
        self.query_one("#power-panel", Static).update(
            Text.from_markup(f"[bold yellow]{GLYPH_POWER} POWER RAIL[/]  [dim]awaiting link[/]")
        )

    def _render_controller_waiting(self):
        self.query_one("#controller-panel", Static).update(
            Text.from_markup(f"[bold magenta]{GLYPH_GAMEPAD} PILOT INPUTS[/]  [dim]awaiting link[/]")
        )

    def _render_wheels_waiting(self):
        self.query_one("#wheels-panel", Static).update(
            Text.from_markup(f"[bold cyan]{GLYPH_WHEEL} DRIVETRAIN[/]  [dim]awaiting link[/]")
        )

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

        hud = self.query_one("#hud-header", Static)
        hud.update(self._hud_banner(snapshot, sources, gamepad_status, system_status))

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

    def _hud_banner(
        self,
        snapshot: dict[str, Any],
        sources: dict[str, Any],
        gamepad_status: str,
        system_status: str,
    ) -> Text:
        now = time.time()
        controller = snapshot.get("controller") or {}
        wheels = snapshot.get("wheels") or {}
        battery = snapshot.get("motor_battery") or {}
        pi = snapshot.get("pi") or {}
        drive_status, drive_notes = self._drive_status(gamepad_status, system_status, controller, wheels, battery, pi)

        # Status color and glyph
        if drive_status == "ready":
            status_color = "bold green"
            status_glyph = GLYPH_OK
        elif drive_status == "caution":
            status_color = "bold yellow"
            status_glyph = GLYPH_WARN
        else:
            status_color = "bold red"
            status_glyph = GLYPH_ERR

        # Voltage display
        voltage = battery.get("pack_voltage")
        voltage_str = f"{voltage:.1f}V" if voltage else "--.-V"

        # Build session timer
        session = fmt_duration(time.monotonic() - self.session_started)

        # Gamepad/system status with age
        gp_age = fmt_age((sources.get("gamepad_teleop") or {}).get("last_seen"), now)
        sys_age = fmt_age((sources.get("system") or {}).get("last_seen"), now)
        gp_color = "green" if gamepad_status == "live" else "yellow"
        sys_color = "green" if system_status == "live" else "yellow"

        notes_str = " │ ".join(drive_notes) if drive_notes else "all systems nominal"

        lines = [
            "[bold cyan]╔══════════════════════════════════════════════════════════════════════════════╗[/]",
            f"[bold cyan]║[/]  [bold white]R O B O - P E T[/]   "
            f"[{status_color}]{status_glyph} {drive_status.upper()}[/]     "
            f"[bold yellow]{GLYPH_POWER}[/] {voltage_str}     "
            f"[magenta]⏱ {session}[/]"
            + " " * 20 + "[bold cyan]║[/]",
            f"[bold cyan]║[/]  [dim]{notes_str}[/]" + " " * max(0, 72 - len(notes_str)) + "[bold cyan]║[/]",
            f"[bold cyan]║[/]  [{gp_color}]{GLYPH_GAMEPAD} gamepad {gp_age}[/]  │  "
            f"[{sys_color}]{GLYPH_SIGNAL} system {sys_age}[/]"
            + " " * 36 + "[bold cyan]║[/]",
            "[bold cyan]╚══════════════════════════════════════════════════════════════════════════════╝[/]",
        ]
        return Text.from_markup("\n".join(lines))

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
        throttle_val = pi.get("throttled_flags")
        throttle_ok = throttle_val in {None, "0x0", "0"}
        throttle_color = "green" if throttle_ok else "yellow"
        throttle_glyph = GLYPH_OK if throttle_ok else GLYPH_WARN

        temp = pi.get("soc_temp_c")
        temp_color = "green" if temp and temp < 70 else ("yellow" if temp and temp < 80 else "red")

        load = pi.get("load_1m")
        load_bar = bar(load, limit=4.0, width=8) if load else "░" * 8

        mem_used = pi.get("memory_used_mb") or 0
        mem_total = pi.get("memory_total_mb") or 1
        mem_bar = bar(mem_used, limit=mem_total, width=8)

        lines = [
            f"[bold cyan]{GLYPH_CPU} CORE SYSTEMS[/]  [dim]pi rail / compute[/]",
            f"  [dim]uptime[/]   {fmt(pi.get('uptime_seconds'), 's', digits=0):>10}",
            f"  [dim]load[/]     {load_bar} {fmt(load):>5}",
            f"  [dim]memory[/]   {mem_bar} {fmt(mem_used, 'MB', 0):>6} / {fmt(mem_total, 'MB', 0)}",
            f"  [dim]disk[/]     {fmt(pi.get('disk_used_percent'), '%'):>10}",
            f"  [dim]soc temp[/] [{temp_color}]{fmt(temp, '°C'):>10}[/]",
            f"  [dim]throttle[/] [{throttle_color}]{throttle_glyph} {throttle_val or '0x0'}[/]",
        ]
        self.query_one("#pi-panel", Static).update(Text.from_markup("\n".join(lines)))

    def _render_battery(self, battery: dict[str, Any]):
        status = battery.get("status", "unknown")
        status_glyph = GLYPH_OK if status == "ok" else (GLYPH_WARN if status == "low" else GLYPH_ERR)

        pack_v = battery.get("pack_voltage")
        cell_v = battery.get("cell_voltage")

        # Voltage bar (assuming 3S LiPo: 9.0V empty, 12.6V full)
        v_bar = bar(pack_v - 9.0, limit=3.6, width=12) if pack_v else "░" * 12

        # Sparkline for voltage history
        v_spark = sparkline(self.history["pack_voltage"], width=16)

        lines = [
            f"[bold yellow]{GLYPH_POWER} POWER RAIL[/]  [{status_style(status)}]{status_glyph} {status.upper()}[/]",
            f"  [dim]pack[/]      {v_bar} [bold]{fmt(pack_v, 'V', 2):>7}[/]",
            f"  [dim]cell est[/]  {fmt(cell_v, 'V', 2):>18}",
            f"  [dim]trend[/]     [cyan]{v_spark}[/]",
            f"  [dim]peak amps[/] {fmt(self.max_current_amps, 'A', 2):>18}",
        ]
        self.query_one("#power-panel", Static).update(Text.from_markup("\n".join(lines)))

    def _render_controller(self, controller: dict[str, Any]):
        connected = controller.get("connected", False)
        conn_glyph = GLYPH_OK if connected else GLYPH_ERR
        conn_color = "green" if connected else "red"
        conn_text = "LINKED" if connected else "OFFLINE"

        # Sticks with bipolar bars
        lx = controller.get("left_stick_x")
        ly = controller.get("left_stick_y")
        rx = controller.get("right_stick_x")
        ry = controller.get("right_stick_y")

        # Triggers with unipolar bars
        lt = controller.get("left_trigger")
        rt = controller.get("right_trigger")

        # D-pad as direction indicator
        dx, dy = controller.get("dpad_x", 0), controller.get("dpad_y", 0)
        dpad_arrows = {
            (0, 1): "▲", (0, -1): "▼", (-1, 0): "◀", (1, 0): "▶",
            (-1, 1): "◤", (1, 1): "◥", (-1, -1): "◣", (1, -1): "◢", (0, 0): "◇"
        }
        dpad = dpad_arrows.get((dx, dy), "◇")

        # Buttons
        pressed = [name.upper() for name, value in (controller.get("buttons") or {}).items() if value]
        buttons_str = " ".join(pressed) if pressed else "[dim]none[/]"

        lines = [
            f"[bold magenta]{GLYPH_GAMEPAD} PILOT INPUTS[/]  [{conn_color}]{conn_glyph} {conn_text}[/]",
            "",
            f"  [dim]L stick[/]  X {bipolar_bar(lx, width=6)} {fmt(lx, digits=2):>6}",
            f"           Y {bipolar_bar(ly, width=6)} {fmt(ly, digits=2):>6}",
            f"  [dim]R stick[/]  X {bipolar_bar(rx, width=6)} {fmt(rx, digits=2):>6}",
            f"           Y {bipolar_bar(ry, width=6)} {fmt(ry, digits=2):>6}",
            "",
            f"  [dim]triggers[/] L {bar(lt, width=8)} {fmt(lt, digits=2):>5}   R {bar(rt, width=8)} {fmt(rt, digits=2):>5}",
            f"  [dim]d-pad[/]    [bold]{dpad}[/]",
            f"  [dim]buttons[/]  {buttons_str}",
        ]
        self.query_one("#controller-panel", Static).update(Text.from_markup("\n".join(lines)))

    def _render_wheels(self, wheels: dict[str, Any]):
        read_ok = wheels.get("read_ok", False)
        read_glyph = GLYPH_OK if read_ok else GLYPH_ERR
        read_color = "green" if read_ok else "red"
        read_text = "ENCODER OK" if read_ok else "READ FAIL"

        lines = [
            f"[bold cyan]{GLYPH_WHEEL} DRIVETRAIN[/]  [{read_color}]{read_glyph} {read_text}[/]",
            "",
            "  [dim]┌─────────────────────────────────────────────────────────────┐[/]",
        ]

        for side in ("left", "right"):
            label = "L" if side == "left" else "R"
            cmd = wheels.get(f"{side}_command")
            target = wheels.get(f"{side}_target_qpps")
            actual = wheels.get(f"{side}_actual_qpps")
            error = wheels.get(f"{side}_error_qpps")
            current = wheels.get(f"{side}_current_amps")

            # Error color
            error_style = "green"
            if error and target:
                ratio = abs(error) / max(abs(target), 1)
                if ratio > 0.25:
                    error_style = "red"
                elif ratio > 0.10:
                    error_style = "yellow"

            # Command bar
            cmd_bar = bipolar_bar(cmd, width=5)

            # Speed sparkline
            speed_spark = sparkline(self.history[f"{side}_actual"], width=10)

            # Current bar
            current_bar = bar(current, limit=5.0, width=6) if current else "░" * 6

            lines.extend([
                f"  [dim]│[/] [bold]{label}[/] cmd {cmd_bar} {fmt(cmd, digits=2):>6}  "
                f"[dim]target[/] {fmt(target, digits=0):>6}  [dim]actual[/] {fmt(actual, digits=0):>6}  "
                f"[dim]err[/] [{error_style}]{fmt(error, digits=0):>5}[/] [dim]│[/]",
                f"  [dim]│[/]   [dim]speed[/] [cyan]{speed_spark}[/]  "
                f"[dim]current[/] {current_bar} {fmt(current, 'A', 2):>6}"
                + " " * 13 + "[dim]│[/]",
            ])

        lines.append("  [dim]└─────────────────────────────────────────────────────────────┘[/]")

        # Current history
        lines.extend([
            "",
            f"  [dim]current trend[/]  L [cyan]{sparkline(self.history['left_current'], width=12)}[/]  "
            f"R [cyan]{sparkline(self.history['right_current'], width=12)}[/]",
        ])

        self.query_one("#wheels-panel", Static).update(Text.from_markup("\n".join(lines)))



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only robot telemetry dashboard.")
    parser.add_argument("--socket", default=DEFAULT_SUBSCRIBE_SOCKET, help="Telemetry hub subscriber socket")
    return parser


def main():
    args = build_parser().parse_args()
    RobotDashboard(args.socket).run()


if __name__ == "__main__":
    main()
