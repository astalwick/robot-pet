#!/usr/bin/env python3
"""Config-driven robot voice assistant service."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal

from config.voice import DEFAULT_CONFIG_PATH, VoiceConfig, VoiceConfigError, load_voice_config
from lib.log import setup_logging
from telemetry.messages import voice_update
from telemetry.paths import DEFAULT_PUBLISH_SOCKET
from telemetry.socket_client import publish_message
from voice.session import VoiceSession


DEFAULT_POLL_SECONDS = 1.0

log = setup_logging("robot-voice")


class RobotVoiceService:
    def __init__(
        self,
        config_path: str,
        telemetry_socket: str,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.config_path = config_path
        self.telemetry_socket = telemetry_socket
        self.poll_seconds = poll_seconds
        self.session: VoiceSession | None = None
        self.active_config: VoiceConfig | None = None
        self.status: dict[str, object] = {
            "status": "disabled",
            "assistant_speaking": False,
            "partial_transcript": None,
            "last_committed_transcript": None,
            "last_assistant_text": None,
            "last_error": None,
            "barge_in_enabled": None,
            "barge_in_threshold_rms": None,
            "barge_in_mic_rms": None,
            "barge_in_playback_rms": None,
            "barge_in_gate_open": None,
            "barge_in_last_reason": None,
        }
        self.last_logged_error: str | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                config = load_voice_config(self.config_path)
            except VoiceConfigError as exc:
                await self.stop_session()
                self.publish(VoiceConfig(), status="error", last_error=str(exc))
                await asyncio.sleep(self.poll_seconds)
                continue

            if not config.enabled:
                await self.stop_session()
                self.publish(config, status="disabled", assistant_speaking=False, last_error=None)
                await asyncio.sleep(self.poll_seconds)
                continue

            if not self.has_credentials():
                await self.stop_session()
                self.publish(config, status="error", last_error="Missing ELEVENLABS_API_KEY or OPENAI_API_KEY")
                await asyncio.sleep(self.poll_seconds)
                continue

            if self.session is None or self.active_config != config:
                await self.stop_session()
                await self.start_session(config)

            if self.session is not None:
                wait_task = asyncio.create_task(self.session.wait())
                sleep_task = asyncio.create_task(asyncio.sleep(self.poll_seconds))
                done, pending = await asyncio.wait({wait_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if wait_task in done:
                    try:
                        wait_task.result()
                    except Exception as exc:
                        log.warning("voice session failed: %s", exc)
                        await self.stop_session()
                        self.publish(config, status="reconnecting", last_error=str(exc))
                        await asyncio.sleep(min(5.0, self.poll_seconds * 2))
                continue

            await asyncio.sleep(self.poll_seconds)

        await self.stop_session()

    async def start_session(self, config: VoiceConfig) -> None:
        log.info(
            "starting voice: input=%s output=%s rate=%s channels=%s selected_channel=%s",
            config.input_device,
            config.output_device,
            config.sample_rate,
            config.capture_channels,
            config.capture_channel_index,
        )
        try:
            from openai import AsyncOpenAI
        except ModuleNotFoundError as exc:
            self.publish(config, status="error", last_error=f"Missing Python dependency: {exc.name}")
            return

        self.publish(config, status="starting", last_error=None)
        self.active_config = config
        self.session = VoiceSession(
            config,
            os.environ["ELEVENLABS_API_KEY"],
            AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"]),
            lambda update: self.publish(config, **update),
        )
        try:
            await self.session.start()
        except Exception as exc:
            log.warning("voice session start failed: %s", exc)
            await self.stop_session()
            self.publish(config, status="error", last_error=str(exc))

    async def stop_session(self) -> None:
        if self.session is not None:
            await self.session.stop()
            self.session = None
        self.active_config = None

    def publish(self, config: VoiceConfig, **updates: object) -> None:
        self.status.update(updates)
        last_error = optional_text(self.status["last_error"])
        if last_error and last_error != self.last_logged_error:
            log.error("voice error: %s", last_error)
            self.last_logged_error = last_error
        elif last_error is None:
            self.last_logged_error = None
        publish_message(
            self.telemetry_socket,
            voice_update(
                enabled=config.enabled,
                status=str(self.status["status"]),
                input_device=config.input_device,
                output_device=config.output_device,
                sample_rate=config.sample_rate,
                capture_channels=config.capture_channels,
                capture_channel_index=config.capture_channel_index,
                input_gain=config.input_gain,
                output_gain=config.output_gain,
                assistant_speaking=bool(self.status["assistant_speaking"]),
                partial_transcript=optional_text(self.status["partial_transcript"]),
                last_committed_transcript=optional_text(self.status["last_committed_transcript"]),
                last_assistant_text=optional_text(self.status["last_assistant_text"]),
                last_error=last_error,
                barge_in_enabled=(
                    optional_bool(self.status["barge_in_enabled"])
                    if self.status["barge_in_enabled"] is not None
                    else config.barge_in_enabled
                ),
                barge_in_min_rms=config.barge_in_min_rms,
                barge_in_sustain_ms=config.barge_in_sustain_ms,
                barge_in_playback_leakage_ratio=config.barge_in_playback_leakage_ratio,
                barge_in_threshold_rms=optional_int(self.status["barge_in_threshold_rms"]),
                barge_in_mic_rms=optional_int(self.status["barge_in_mic_rms"]),
                barge_in_playback_rms=optional_int(self.status["barge_in_playback_rms"]),
                barge_in_gate_open=optional_bool(self.status["barge_in_gate_open"]),
                barge_in_last_reason=optional_text(self.status["barge_in_last_reason"]),
            ),
        )

    @staticmethod
    def has_credentials() -> bool:
        return bool(os.environ.get("ELEVENLABS_API_KEY") and os.environ.get("OPENAI_API_KEY"))


def optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot voice assistant service.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return parser


async def run_service(args: argparse.Namespace) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    await RobotVoiceService(args.config, args.telemetry_socket, args.poll_seconds).run(stop_event)


def main() -> None:
    asyncio.run(run_service(build_parser().parse_args()))


if __name__ == "__main__":
    main()
