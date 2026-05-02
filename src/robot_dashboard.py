#!/usr/bin/env python3
"""SSH dashboard for robot telemetry and safe redeploys."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
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
GLYPH_LINK = "◉"


def fmt(value: Any, suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def fix_wraparound(value: int | float | None, bits: int = 32) -> int | float | None:
    """Fix signed 32-bit values that arrived through unsigned math."""
    if value is None:
        return None
    if isinstance(value, float):
        return value
    max_signed = (1 << (bits - 1)) - 1
    min_signed = -(1 << (bits - 1))
    span = 1 << bits
    if value > max_signed:
        return value - span
    if value < min_signed:
        return value + span
    return value


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


def fmt_relative_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 10:
        return f"{seconds:.1f}s ago"
    return f"{int(seconds)}s ago"


def bar(value: float | None, limit: float = 1.0, width: int = 10, absolute: bool = True) -> str:
    """Render a block-style gauge bar."""
    if value is None:
        return "░" * width
    scaled = abs(value) if absolute else max(0.0, value)
    ratio = min(1.0, scaled / limit)
    full_blocks = int(ratio * width)
    remainder = (ratio * width) - full_blocks
    partial_idx = int(remainder * (len(BAR_BLOCKS) - 1))
    
    result = "█" * full_blocks
    if full_blocks < width:
        result += BAR_BLOCKS[partial_idx]
        result += "░" * (width - full_blocks - 1)
    return result


def cell_bar(value: float | None, limit: float = 1.0, width: int = 10, absolute: bool = True) -> str:
    """Render a fixed-cell gauge with a stable visual right edge."""
    if value is None:
        return "░" * width
    scaled = abs(value) if absolute else max(0.0, value)
    ratio = min(1.0, scaled / limit)
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


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


def sparkline(
    values: deque[float | None],
    width: int = 12,
    limit: float | None = None,
    absolute: bool = False,
) -> str:
    """Render a sparkline from historical values."""
    clean = [abs(v) if absolute else v for v in values if v is not None]
    if not clean:
        return "─" * width
    
    # Take last `width` values
    recent = list(clean)[-width:]
    if len(recent) < 2:
        return "─" * width
    
    if limit is None:
        low, high = min(recent), max(recent)
    else:
        low, high = 0.0, limit
    if high == low:
        return SPARK_BLOCKS[4] * len(recent) + "─" * (width - len(recent))
    
    result = ""
    for v in recent:
        ratio = max(0.0, min(1.0, (v - low) / (high - low)))
        idx = int(ratio * (len(SPARK_BLOCKS) - 1))
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
        height: 2fr;
        border: heavy #0a4f6a;
    }

    #link-panel {
        height: auto;
        border: heavy #0a5f5a;
    }

    #logs {
        height: 1fr;
        border: heavy #1a2a3a;
        margin: 0 1 1 1;
        background: #020a10;
    }

    """

    BINDINGS = [("q", "quit", "Quit"), ("r", "redeploy", "Redeploy")]

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
        self.max_abs_speed_qpps = 1.0
        self.redeploy_armed_until = 0.0
        self.redeploy_running = False

    def compose(self) -> ComposeResult:
        yield Static(self._hud_waiting(), id="hud-header")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("", id="pi-panel", classes="panel")
                yield Static("", id="power-panel", classes="panel")
                yield Static("", id="controller-panel", classes="panel")
            with Vertical(id="right"):
                yield Static("", id="wheels-panel", classes="panel")
                yield Static("", id="link-panel", classes="panel")
        yield RichLog(id="logs", wrap=True, highlight=True)
        yield Footer()

    def _hud_waiting(self) -> Text:
        return Text.from_markup(
            "[bold white]R O B O - P E T[/]   [dim]awaiting telemetry link...[/]"
        )

    def on_mount(self):
        self.title = "Robo-Pet Dashboard"
        self._render_pi_waiting()
        self._render_power_waiting()
        self._render_controller_waiting()
        self._render_wheels_waiting()
        self._render_link_waiting()

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

    def _render_link_waiting(self):
        self.query_one("#link-panel", Static).update(
            Text.from_markup(f"[bold cyan]{GLYPH_LINK} LINK / LOOP HEALTH[/]  [dim]awaiting link[/]")
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

    def action_redeploy(self):
        logs = self.query_one("#logs", RichLog)
        now = time.monotonic()
        if self.redeploy_running:
            logs.write("Redeploy already running.")
            return
        if now > self.redeploy_armed_until:
            self.redeploy_armed_until = now + 10.0
            logs.write("Redeploy armed. Press r again within 10s to git fast-forward and restart robot services.")
            return

        self.redeploy_running = True
        self.redeploy_armed_until = 0.0
        logs.write("Starting redeploy...")
        threading.Thread(target=self._redeploy_thread, daemon=True).start()

    def _redeploy_thread(self):
        logs = self.query_one("#logs", RichLog)
        script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "redeploy-robot.sh"))
        env = {**os.environ, "ROBOT_PET_REPO_DIR": os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))}

        try:
            process = subprocess.Popen(
                [script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
        except OSError as exc:
            self.redeploy_running = False
            self.call_from_thread(logs.write, f"Redeploy failed to start: {exc}")
            return

        if process.stdout is not None:
            for line in process.stdout:
                self.call_from_thread(logs.write, line.rstrip())

        exit_code = process.wait()
        self.redeploy_running = False
        if exit_code == 0:
            self.call_from_thread(logs.write, "Redeploy succeeded. Restarting dashboard...")
            self.call_from_thread(self._restart_dashboard)
        else:
            self.call_from_thread(logs.write, f"Redeploy failed with exit code {exit_code}. Dashboard left running.")

    def _restart_dashboard(self):
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def apply_snapshot(self, snapshot: dict[str, Any]):
        self.last_snapshot = snapshot
        sources = snapshot.get("sources", {})
        gamepad_status = self._source_label(sources.get("gamepad_teleop", {}))
        system_status = self._source_label(sources.get("system", {}))
        gamepad_live = gamepad_status == "live"
        self._record_history(snapshot, gamepad_live)

        controller = snapshot.get("controller") or {}
        wheels = snapshot.get("wheels") or {}
        battery = snapshot.get("motor_battery") or {}
        link_loop = snapshot.get("link_loop") or {}
        if not gamepad_live:
            controller = {}
            wheels = {}
            battery = {"status": "stale"}
            link_loop = {"status": "stale"}

        hud = self.query_one("#hud-header", Static)
        hud.update(self._hud_banner(snapshot, sources, gamepad_status, system_status, controller, wheels, battery))

        self._render_pi(snapshot.get("pi") or {})
        self._render_battery(battery)
        self._render_controller(controller)
        self._render_wheels(wheels)
        self._render_link_loop(link_loop)

    def _source_label(self, source: dict[str, Any]) -> str:
        return "stale" if source.get("stale", True) else "live"

    def _record_history(self, snapshot: dict[str, Any], gamepad_live: bool):
        if not gamepad_live:
            return
        wheels = snapshot.get("wheels") or {}
        battery = snapshot.get("motor_battery") or {}
        self.history["pack_voltage"].append(battery.get("pack_voltage"))
        self.history["left_current"].append(wheels.get("left_current_amps"))
        self.history["right_current"].append(wheels.get("right_current_amps"))
        for side in ("left", "right"):
            target, actual, error = self._wheel_qpps(wheels, side)
            self.history[f"{side}_actual"].append(actual)
            self.history[f"{side}_error"].append(error)
            for value in (target, actual):
                if value is not None:
                    self.max_abs_speed_qpps = max(self.max_abs_speed_qpps, abs(value))

        for value in (wheels.get("left_current_amps"), wheels.get("right_current_amps")):
            if value is not None:
                self.max_current_amps = max(self.max_current_amps, abs(value))

    def _hud_banner(
        self,
        snapshot: dict[str, Any],
        sources: dict[str, Any],
        gamepad_status: str,
        system_status: str,
        controller: dict[str, Any],
        wheels: dict[str, Any],
        battery: dict[str, Any],
    ) -> Text:
        now = time.time()
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
        voltage_str = f"{voltage:.1f}V" if voltage is not None else "--.-V"

        # Build session timer
        session = fmt_duration(time.monotonic() - self.session_started)

        # Gamepad/system status with age
        gp_age = fmt_age((sources.get("gamepad_teleop") or {}).get("last_seen"), now)
        sys_age = fmt_age((sources.get("system") or {}).get("last_seen"), now)
        gp_color = "green" if gamepad_status == "live" else "yellow"
        sys_color = "green" if system_status == "live" else "yellow"

        notes_str = " │ ".join(drive_notes) if drive_notes else "all systems nominal"

        lines = [
            f"[bold white]R O B O - P E T[/]   [{status_color}]{status_glyph} {drive_status.upper()}[/]    "
            f"[bold yellow]{GLYPH_POWER}[/] {voltage_str}    [magenta]⏱ {session}[/]",
            f"[dim]{notes_str}[/]",
            f"[{gp_color}]{GLYPH_GAMEPAD} gamepad {gp_age}[/]    [{sys_color}]{GLYPH_SIGNAL} system {sys_age}[/]",
        ]
        return Text.from_markup("\n".join(lines))

    def _wheel_qpps(self, wheels: dict[str, Any], side: str) -> tuple[int | float | None, int | float | None, int | float | None]:
        target = fix_wraparound(wheels.get(f"{side}_target_qpps"))
        actual = fix_wraparound(wheels.get(f"{side}_actual_qpps"))
        raw_error = fix_wraparound(wheels.get(f"{side}_error_qpps"))
        error = target - actual if target is not None and actual is not None else raw_error
        return target, actual, error

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

        if gamepad_status != "live":
            notes.append("drive telemetry stale")
            return "hold", notes
        if battery_status in {"critical", "unknown"}:
            notes.append(f"battery {battery_status}")
            return "hold", notes
        if not controller_connected:
            notes.append("controller offline")
            return "hold", notes

        if system_status != "live":
            notes.append("system telemetry stale")
        if battery_status == "low":
            notes.append("battery low")
        if throttled not in {None, "0x0", "0"}:
            notes.append(f"pi throttled {throttled}")
        if not wheels_read_ok:
            notes.append("wheel readback missing")

        if notes:
            return "caution", notes
        return "ready", ["manual drive only"]

    def _row(
        self,
        label: str,
        gauge: str = "",
        value: str = "",
        *,
        label_w: int = 11,
        gauge_w: int = 12,
        value_w: int = 11,
        value_style: str = "",
        gauge_style: str = "",
    ) -> str:
        """Render a label / gauge / value row with fixed-width columns."""
        label_part = f"[dim]{label:<{label_w}}[/]"
        gauge_padded = gauge.ljust(gauge_w)
        gauge_part = f"[{gauge_style}]{gauge_padded}[/]" if gauge_style else gauge_padded
        value_padded = value.rjust(value_w)
        value_part = f"[{value_style}]{value_padded}[/]" if value_style else value_padded
        return f"  {label_part} {gauge_part} {value_part}"

    def _render_pi(self, pi: dict[str, Any]):
        throttle_val = pi.get("throttled_flags")
        throttle_ok = throttle_val in {None, "0x0", "0"}
        throttle_color = "green" if throttle_ok else "yellow"
        throttle_glyph = GLYPH_OK if throttle_ok else GLYPH_WARN

        temp = pi.get("soc_temp_c")
        if temp is None:
            temp_color = ""
        elif temp < 70:
            temp_color = "green"
        elif temp < 80:
            temp_color = "yellow"
        else:
            temp_color = "red"

        load = pi.get("load_1m")
        load_bar = bar(load, limit=4.0, width=10) if load is not None else " " * 10

        mem_used = pi.get("memory_used_mb") or 0
        mem_total = pi.get("memory_total_mb") or 1
        mem_bar = bar(mem_used, limit=mem_total, width=10)
        mem_str = f"{int(mem_used)}/{int(mem_total)}MB"

        GW, VW = 10, 12
        lines = [
            f"[bold cyan]{GLYPH_CPU} CORE SYSTEMS[/]  [dim]pi rail / compute[/]",
            self._row("uptime", "", fmt(pi.get("uptime_seconds"), "s", digits=0), gauge_w=GW, value_w=VW),
            self._row("load", load_bar, fmt(load), gauge_w=GW, value_w=VW),
            self._row("memory", mem_bar, mem_str, gauge_w=GW, value_w=VW),
            self._row("disk", "", fmt(pi.get("disk_used_percent"), "%"), gauge_w=GW, value_w=VW),
            self._row("soc temp", "", fmt(temp, "°C"), gauge_w=GW, value_w=VW, value_style=temp_color),
            self._row(
                "throttle",
                "",
                f"{throttle_glyph} {throttle_val or '0x0'}",
                gauge_w=GW,
                value_w=VW,
                value_style=throttle_color,
            ),
        ]
        self.query_one("#pi-panel", Static).update(Text.from_markup("\n".join(lines)))

    def _render_battery(self, battery: dict[str, Any]):
        status = battery.get("status", "unknown")
        status_glyph = GLYPH_OK if status == "ok" else (GLYPH_WARN if status in {"low", "stale"} else GLYPH_ERR)

        pack_v = battery.get("pack_voltage")
        cell_v = battery.get("cell_voltage")

        GW, VW = 14, 8

        # Voltage bar (assuming 3S LiPo: 9.0V empty, 12.6V full)
        v_bar = bar(pack_v - 9.0, limit=3.6, width=GW, absolute=False) if pack_v is not None else " " * GW
        v_spark = sparkline(self.history["pack_voltage"], width=GW)

        lines = [
            f"[bold yellow]{GLYPH_POWER} POWER RAIL[/]  [{status_style(status)}]{status_glyph} {status.upper()}[/]",
            self._row("pack", v_bar, fmt(pack_v, "V", 2), gauge_w=GW, value_w=VW, value_style="bold"),
            self._row("cell est", "", fmt(cell_v, "V", 2), gauge_w=GW, value_w=VW),
            self._row("trend", v_spark, "", gauge_w=GW, value_w=VW, gauge_style="cyan"),
            self._row("peak amps", "", fmt(self.max_current_amps, "A", 2), gauge_w=GW, value_w=VW),
        ]
        self.query_one("#power-panel", Static).update(Text.from_markup("\n".join(lines)))

    def _render_controller(self, controller: dict[str, Any]):
        connected = controller.get("connected", False)
        conn_glyph = GLYPH_OK if connected else GLYPH_ERR
        conn_color = "green" if connected else "red"
        conn_text = "LINKED" if connected else "OFFLINE"

        lx = controller.get("left_stick_x")
        ly = controller.get("left_stick_y")
        rx = controller.get("right_stick_x")
        ry = controller.get("right_stick_y")

        lt = controller.get("left_trigger")
        rt = controller.get("right_trigger")

        dx, dy = controller.get("dpad_x", 0), controller.get("dpad_y", 0)
        dpad_arrows = {
            (0, 1): "▲", (0, -1): "▼", (-1, 0): "◀", (1, 0): "▶",
            (-1, 1): "◤", (1, 1): "◥", (-1, -1): "◣", (1, -1): "◢", (0, 0): "◇",
        }
        dpad = dpad_arrows.get((dx, dy), "◇")

        pressed = [name.upper() for name, value in (controller.get("buttons") or {}).items() if value]
        buttons_str = " ".join(pressed) if pressed else "[dim]none[/]"

        LW = 9
        sticks = [
            ("L stick", "X", lx),
            ("",        "Y", ly),
            ("R stick", "X", rx),
            ("",        "Y", ry),
        ]

        lines = [
            f"[bold magenta]{GLYPH_GAMEPAD} PILOT INPUTS[/]  [{conn_color}]{conn_glyph} {conn_text}[/]",
            "",
        ]
        for label, axis, value in sticks:
            lines.append(
                f"  [dim]{label:<{LW}}[/] {axis} {bipolar_bar(value, width=6)} {fmt(value, digits=2):>6}"
            )
        lines.extend([
            "",
            f"  [dim]{'triggers':<{LW}}[/] L {bar(lt, width=8)} {fmt(lt, digits=2):>5}    "
            f"R {bar(rt, width=8)} {fmt(rt, digits=2):>5}",
            f"  [dim]{'d-pad':<{LW}}[/] [bold]{dpad}[/]",
            f"  [dim]{'buttons':<{LW}}[/] {buttons_str}",
        ])
        self.query_one("#controller-panel", Static).update(Text.from_markup("\n".join(lines)))

    def _render_wheels(self, wheels: dict[str, Any]):
        read_ok = wheels.get("read_ok", False)
        read_glyph = GLYPH_OK if read_ok else GLYPH_ERR
        read_color = "green" if read_ok else "red"
        read_text = "ENCODER OK" if read_ok else "READ FAIL"

        LABEL_W, COL_W, GW = 13, 13, 13
        STACK_OFFSET = "  "

        def styled(value: str, style: str = "") -> str:
            return f"[{style}]{value}[/]" if style else value

        def wheel_row(
            label: str,
            left_value: str,
            right_value: str,
            *,
            align: str = ">",
            left_style: str = "",
            right_style: str = "",
        ) -> str:
            if align == "^":
                left_cell = left_value.center(COL_W)
                right_cell = right_value.center(COL_W)
            elif align == "<":
                left_cell = left_value.ljust(COL_W)
                right_cell = right_value.ljust(COL_W)
            else:
                left_cell = left_value.rjust(COL_W)
                right_cell = right_value.rjust(COL_W)
            return (
                f"  [dim]{label:<{LABEL_W}}[/] "
                f"{STACK_OFFSET}{styled(left_cell, left_style)}   {styled(right_cell, right_style)}"
            )

        lines = [
            f"[bold cyan]{GLYPH_WHEEL} DRIVETRAIN[/]  [{read_color}]{read_glyph} {read_text}[/]",
            "",
            f"  {'':<{LABEL_W}} "
            f"{STACK_OFFSET}"
            f"[bold cyan]{'L WHEEL':^{COL_W}}[/]   "
            f"[bold cyan]{'R WHEEL':^{COL_W}}[/]",
        ]

        wheel_data: dict[str, dict[str, Any]] = {}
        for side in ("left", "right"):
            cmd = wheels.get(f"{side}_command")
            target, actual, error = self._wheel_qpps(wheels, side)
            current = wheels.get(f"{side}_current_amps")

            track_style = "green"
            if error is not None:
                ratio = abs(error) / max(abs(target or 0), 1)
                if ratio > 0.25:
                    track_style = "red"
                elif ratio > 0.10:
                    track_style = "yellow"

            wheel_data[side] = {
                "cmd": bipolar_bar(cmd, width=6),
                "target": fmt(target, digits=0),
                "actual": fmt(actual, digits=0),
                "error": fmt(error, digits=0),
                "current": fmt(current, "A", 2),
                "load": bar(current, limit=5.0, width=GW) if current is not None else " " * GW,
                "track_style": track_style,
            }

        lines.extend([
            wheel_row("cmd", wheel_data["left"]["cmd"], wheel_data["right"]["cmd"], align="^"),
            wheel_row("target", wheel_data["left"]["target"], wheel_data["right"]["target"]),
            wheel_row("actual", wheel_data["left"]["actual"], wheel_data["right"]["actual"]),
            wheel_row(
                "error",
                wheel_data["left"]["error"],
                wheel_data["right"]["error"],
                left_style=wheel_data["left"]["track_style"],
                right_style=wheel_data["right"]["track_style"],
            ),
            wheel_row("amps", wheel_data["left"]["current"], wheel_data["right"]["current"]),
            wheel_row("load", wheel_data["left"]["load"], wheel_data["right"]["load"], align="^"),
            "",
            f"  [dim]{'speed   trend':<{LABEL_W}}[/] "
            f"L [cyan]{sparkline(self.history['left_actual'], width=GW, limit=self.max_abs_speed_qpps, absolute=True)}[/]  "
            f"R [cyan]{sparkline(self.history['right_actual'], width=GW, limit=self.max_abs_speed_qpps, absolute=True)}[/]",
            f"  [dim]{'current trend':<{LABEL_W}}[/] "
            f"L [cyan]{sparkline(self.history['left_current'], width=GW)}[/]  "
            f"R [cyan]{sparkline(self.history['right_current'], width=GW)}[/]",
        ])

        self.query_one("#wheels-panel", Static).update(Text.from_markup("\n".join(lines)))

    def _render_link_loop(self, link_loop: dict[str, Any]):
        status = self._link_loop_status(link_loop)
        if status == "live":
            status_glyph = GLYPH_OK
            status_color = "green"
            status_text = "LIVE"
        elif status == "degraded":
            status_glyph = GLYPH_WARN
            status_color = "yellow"
            status_text = "DEGRADED"
        else:
            status_glyph = GLYPH_ERR
            status_color = "red"
            status_text = "STALE"

        success_rate = link_loop.get("read_success_rate")
        success_percent = f"{success_rate * 100:.0f}% ok" if success_rate is not None else "--"
        success_bar = cell_bar(success_rate, limit=1.0, width=10, absolute=False) if success_rate is not None else " " * 10

        failures = link_loop.get("consecutive_read_failures")
        failure_text = "none" if failures == 0 else (f"{failures} streak" if failures is not None else "--")
        failure_bar = cell_bar(failures, limit=10, width=10, absolute=False) if failures else " " * 10

        latency = link_loop.get("telemetry_latency_ms")
        latency_text = f"{latency:.0f}ms ok" if latency is not None and latency <= 100 else (
            f"{latency:.0f}ms" if latency is not None else "--"
        )
        latency_bar = cell_bar(100.0 - latency, limit=100.0, width=10, absolute=False) if latency is not None else " " * 10

        loop_hz = link_loop.get("command_loop_hz")
        loop_text = f"{loop_hz:.1f} Hz" if loop_hz is not None else "--"
        loop_bar = cell_bar(loop_hz, limit=20.0, width=10, absolute=False) if loop_hz is not None else " " * 10

        GW, VW = 10, 11
        lines = [
            f"[bold cyan]{GLYPH_LINK} LINK / LOOP HEALTH[/]  [{status_color}]{status_glyph} {status_text}[/]",
            self._row(
                "roboclaw",
                success_bar,
                success_percent,
                gauge_w=GW,
                value_w=VW,
                value_style=status_color,
            ),
            self._row(
                "last good",
                "",
                fmt_relative_seconds(link_loop.get("last_good_read_age_seconds")),
                gauge_w=GW,
                value_w=VW,
            ),
            self._row(
                "failures",
                failure_bar,
                failure_text,
                gauge_w=GW,
                value_w=VW,
                value_style=status_color,
            ),
            self._row("latency", latency_bar, latency_text, gauge_w=GW, value_w=VW),
            self._row("drive loop", loop_bar, loop_text, gauge_w=GW, value_w=VW),
        ]
        self.query_one("#link-panel", Static).update(Text.from_markup("\n".join(lines)))

    def _link_loop_status(self, link_loop: dict[str, Any]) -> str:
        if link_loop.get("status") == "stale":
            return "stale"

        success_rate = link_loop.get("read_success_rate")
        failures = link_loop.get("consecutive_read_failures")
        last_good_age = link_loop.get("last_good_read_age_seconds")
        latency = link_loop.get("telemetry_latency_ms")

        if success_rate is None:
            return "stale"
        if success_rate < 0.5 or (failures is not None and failures >= 5) or (
            last_good_age is not None and last_good_age >= 5
        ):
            return "stale"
        if success_rate < 0.9 or (failures is not None and failures > 0) or (
            latency is not None and latency > 100
        ):
            return "degraded"
        return "live"



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot telemetry dashboard.")
    parser.add_argument("--socket", default=DEFAULT_SUBSCRIBE_SOCKET, help="Telemetry hub subscriber socket")
    return parser


def main():
    args = build_parser().parse_args()
    RobotDashboard(args.socket).run()


if __name__ == "__main__":
    main()
