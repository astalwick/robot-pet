import asyncio
import json
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import suppress
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

import robot_voice
from robot_voice import RobotVoiceService, TimelineBuffer, wake_handoff_audio
from config.voice import load_voice_config
from drivers.respeaker import DoAReading
from voice.assistant import AudioLevels
from voice.turn_policy import TurnPolicy


class RobotVoiceServiceTest(unittest.IsolatedAsyncioTestCase):
    def test_wake_handoff_audio_keeps_only_recent_tail(self):
        frames = [bytes([idx]) * 2560 for idx in range(10)]

        self.assertEqual(wake_handoff_audio(robot_voice.deque(frames), 16000), frames[-4:])
        self.assertEqual(wake_handoff_audio(robot_voice.deque(frames[:3]), 16000), frames[:3])

    async def test_disabled_config_does_not_start_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump({"enabled": False}, file_obj)

            service = RobotVoiceService(
                config_path,
                "/tmp/missing.sock",
                command_socket=os.path.join(tmpdir, "cmd.sock"),
                poll_seconds=0.01,
            )
            stop_event = asyncio.Event()

            async def stop_soon():
                await asyncio.sleep(0.03)
                stop_event.set()

            with mock.patch.object(service, "start_orchestrator", new=mock.AsyncMock()) as start_orchestrator:
                await asyncio.gather(service.run(stop_event), stop_soon())

        start_orchestrator.assert_not_called()

    async def test_wake_orchestrator_starts_without_api_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump({"enabled": True, "wake_word_enabled": True}, file_obj)

            service = RobotVoiceService(
                config_path,
                "/tmp/missing.sock",
                command_socket=os.path.join(tmpdir, "cmd.sock"),
                poll_seconds=0.01,
            )
            stop_event = asyncio.Event()

            async def stop_soon():
                await asyncio.sleep(0.05)
                stop_event.set()

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(service, "start_orchestrator", new=mock.AsyncMock()) as start_orchestrator:
                    await asyncio.gather(service.run(stop_event), stop_soon())

            start_orchestrator.assert_called()

    async def test_orchestrator_failure_restarts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump({"enabled": True, "wake_word_enabled": True}, file_obj)

            service = RobotVoiceService(
                config_path,
                "/tmp/missing.sock",
                command_socket=os.path.join(tmpdir, "cmd.sock"),
                poll_seconds=0.01,
            )
            stop_event = asyncio.Event()
            published = []
            start_count = 0

            async def fake_start_orchestrator(config):
                nonlocal start_count
                start_count += 1
                if start_count == 1:

                    async def fail():
                        raise RuntimeError("orchestrator failed")

                    service._orchestrator_task = asyncio.create_task(fail())
                    service.active_config = config
                    await asyncio.sleep(0)

            async def stop_soon():
                await asyncio.sleep(0.08)
                stop_event.set()

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
                    with mock.patch.object(service, "start_orchestrator", side_effect=fake_start_orchestrator):
                        await asyncio.gather(service.run(stop_event), stop_soon())

            self.assertGreaterEqual(start_count, 2)
            self.assertTrue(any(message.get("status") == "reconnecting" for message in published))

    async def test_start_orchestrator_rejects_missing_chime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump(
                    {
                        "enabled": True,
                        "wake_word_enabled": True,
                        "wake_chime_path": os.path.join(tmpdir, "missing.wav"),
                    },
                    file_obj,
                )

            service = RobotVoiceService(
                config_path,
                "/tmp/missing.sock",
                command_socket=os.path.join(tmpdir, "cmd.sock"),
                poll_seconds=0.01,
            )
            published = []

            with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
                await service.start_orchestrator(load_voice_config(config_path))

            self.assertIsNone(service._orchestrator_task)
            self.assertTrue(any("Chime WAV not found" in str(message.get("last_error")) for message in published))
            config = load_voice_config(config_path)
            self.assertEqual(service._orchestrator_startup_latched, config)

            published.clear()
            with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
                await service.start_orchestrator(config)
            self.assertEqual(len(published), 0)

    async def test_orchestrator_startup_latch_skips_retry_until_config_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump(
                    {
                        "enabled": True,
                        "wake_word_enabled": True,
                        "wake_chime_path": os.path.join(tmpdir, "missing.wav"),
                    },
                    file_obj,
                )

            service = RobotVoiceService(
                config_path,
                "/tmp/missing.sock",
                command_socket=os.path.join(tmpdir, "cmd.sock"),
                poll_seconds=0.01,
            )
            start_calls = 0
            config = load_voice_config(config_path)

            async def counting_start(config):
                nonlocal start_calls
                start_calls += 1
                return await RobotVoiceService.start_orchestrator(service, config)

            with mock.patch.object(service, "start_orchestrator", side_effect=counting_start):
                with mock.patch("robot_voice.publish_message", return_value=True):
                    await service._run_wake_orchestrator(config)
                    await service._run_wake_orchestrator(config)

            self.assertEqual(start_calls, 1)

    async def test_config_load_error_latches_until_file_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                file_obj.write("{")

            service = RobotVoiceService(
                config_path,
                "/tmp/missing.sock",
                command_socket=os.path.join(tmpdir, "cmd.sock"),
                poll_seconds=0.01,
            )
            stop_event = asyncio.Event()
            published = []

            async def change_config_then_stop():
                await asyncio.sleep(0.035)
                with open(config_path, "w") as file_obj:
                    json.dump({"sample_rate": 8000}, file_obj)
                await asyncio.sleep(0.035)
                stop_event.set()

            with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
                await asyncio.gather(service.run(stop_event), change_config_then_stop())

            errors = [message for message in published if message.get("status") == "error"]
            self.assertEqual(len(errors), 2)
            self.assertIn("Invalid voice config", str(errors[0].get("last_error")))
            self.assertEqual(errors[1].get("last_error"), "sample_rate must be 16000")

    async def test_personality_only_config_change_does_not_restart_orchestrator(self):
        service = RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock", poll_seconds=0.01)
        service.active_config = service_config(enabled=True, wake_word_enabled=True, personality="default")
        service._orchestrator_task = asyncio.create_task(asyncio.Event().wait())

        try:
            with (
                mock.patch.object(service, "stop_all", new=mock.AsyncMock()) as stop_all,
                mock.patch.object(service, "start_orchestrator", new=mock.AsyncMock()) as start_orchestrator,
            ):
                await service._run_wake_orchestrator(
                    service_config(enabled=True, wake_word_enabled=True, personality="nina")
                )

            stop_all.assert_not_called()
            start_orchestrator.assert_not_called()
            self.assertEqual(service.active_config.personality, "nina")
        finally:
            service._orchestrator_task.cancel()
            with suppress(asyncio.CancelledError):
                await service._orchestrator_task

    async def test_wake_off_stays_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "voice.json")
            with open(config_path, "w") as file_obj:
                json.dump({"enabled": True}, file_obj)

            service = RobotVoiceService(
                config_path,
                "/tmp/missing.sock",
                command_socket=os.path.join(tmpdir, "cmd.sock"),
                poll_seconds=0.01,
            )
            stop_event = asyncio.Event()

            async def stop_soon():
                await asyncio.sleep(0.03)
                stop_event.set()

            published = []
            with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "x", "OPENAI_API_KEY": "y"}):
                with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
                    with mock.patch.object(service, "start_orchestrator", new=mock.AsyncMock()) as start_orchestrator:
                        await asyncio.gather(service.run(stop_event), stop_soon())

            start_orchestrator.assert_not_called()
            self.assertTrue(any(message.get("status") == "disabled" for message in published))

    async def test_voice_errors_are_logged_once(self):
        service = RobotVoiceService("/tmp/missing.json", "/tmp/missing.sock", poll_seconds=0.01)

        with mock.patch("robot_voice.log") as log:
            service.publish(service_config(), status="error", last_error="bad output")
            service.publish(service_config(), status="error", last_error="bad output")
            service.publish(service_config(), status="listening", last_error=None)
            service.publish(service_config(), status="error", last_error="bad output")

        self.assertEqual(log.error.call_count, 2)

    async def test_publish_reports_configured_personality_without_active_session(self):
        service = RobotVoiceService("/tmp/missing.json", "/tmp/missing.sock", poll_seconds=0.01)
        published = []

        with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
            service.publish(service_config(personality="nina"), status="disabled")

        self.assertEqual(published[-1]["personality"], "nina")

    async def test_publish_turns_stt_led_off_when_voice_is_disabled(self):
        service = RobotVoiceService("/tmp/missing.json", "/tmp/missing.sock", poll_seconds=0.01)
        service.leds = mock.Mock()
        service.status["scribe_state"] = "waiting_for_commit"

        with mock.patch("robot_voice.publish_message", return_value=True):
            service.publish(service_config(enabled=False, wake_word_enabled=False), status="disabled")

        service.leds.update.assert_called_with(voice_on=False, stt_active=False, llm_active=False)

    async def test_stop_all_publishes_clean_terminal_state(self):
        service = RobotVoiceService("/tmp/missing.json", "/tmp/missing.sock", poll_seconds=0.01)
        service.leds = mock.Mock()
        service.status.update(
            {
                "assistant_speaking": True,
                "assistant_working": True,
                "partial_transcript": "hello",
                "last_committed_transcript": "hello there",
                "last_assistant_text": "Once upon a time",
                "barge_in_mic_rms": 900,
                "barge_in_gate_open": True,
                "scribe_state": "waiting_for_commit",
                "scribe_last_error": "stale error",
                "false_starts": 2,
            }
        )
        service.timeline.add_event({"type": "phase", "t": 1.0, "name": "hearing", "on": True})
        published = []

        with mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True):
            await service.stop_all(
                final_config=service_config(enabled=False, wake_word_enabled=False),
                final_status="disabled",
            )

        message = published[-1]
        self.assertEqual(message["status"], "disabled")
        self.assertFalse(message["enabled"])
        self.assertFalse(message["assistant_speaking"])
        self.assertIsNone(message["partial_transcript"])
        self.assertIsNone(message["last_committed_transcript"])
        self.assertIsNone(message["last_assistant_text"])
        self.assertEqual(message["scribe_state"], "closed")
        self.assertNotIn("scribe_last_error", message)
        self.assertEqual(message["false_starts"], 0)
        self.assertIn("timeline", message)
        closed_phases = [
            event
            for event in message["timeline"]["events"]
            if event.get("type") == "phase" and event.get("on") is False
        ]
        self.assertEqual(
            {event.get("name") for event in closed_phases},
            {"hearing", "thinking", "speaking", "user_speech"},
        )
        service.leds.update.assert_called_with(voice_on=False, stt_active=False, llm_active=False)

    async def test_publish_profile_logs_when_enabled(self):
        service = RobotVoiceService("/tmp/missing.json", "/tmp/missing.sock", poll_seconds=0.01, profile_every=1)

        with mock.patch("robot_voice.publish_message", return_value=True):
            with self.assertLogs("robot-voice", level="INFO") as logs:
                service.publish(service_config(), status="listening")

        output = "\n".join(logs.output)
        self.assertIn("voice publish profile:", output)
        self.assertIn("timeline=", output)
        self.assertIn("socket=", output)

    async def test_publish_throttles_timeline_payload(self):
        service = RobotVoiceService("/tmp/missing.json", "/tmp/missing.sock", poll_seconds=0.01)
        published = []

        with (
            mock.patch("robot_voice.time.monotonic", side_effect=[100.0, 100.1, 100.3]),
            mock.patch("robot_voice.publish_message", side_effect=lambda _socket, message: published.append(message) or True),
        ):
            service.publish(service_config(), status="listening")
            service.publish(service_config(), status="hearing")
            service.publish(service_config(), status="thinking")

        self.assertIn("timeline", published[0])
        self.assertNotIn("timeline", published[1])
        self.assertIn("timeline", published[2])

    async def test_activate_session_reloads_personality_cards(self):
        service = RobotVoiceService("/tmp/missing.json", "/tmp/missing.sock", poll_seconds=0.01)
        service.active_config = service_config(personality="cal")
        service.audio = mock.Mock()
        service.audio.stop_io = mock.AsyncMock()

        captured = {}

        class FakeSession:
            def __init__(self, *args, **kwargs):
                captured["personalities"] = kwargs["personalities"]
                self.history = mock.Mock()

            async def start(self):
                pass

            async def stop(self):
                pass

        openai = types.SimpleNamespace(AsyncOpenAI=lambda api_key: mock.Mock())
        cards = {"cal": ("voice-id", "Cal card")}
        with (
            mock.patch.dict(sys.modules, {"openai": openai}),
            mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "eleven", "OPENAI_API_KEY": "openai"}),
            mock.patch("robot_voice.load_personalities", return_value=cards),
            mock.patch("robot_voice.VoiceSession", FakeSession),
            mock.patch("robot_voice.publish_message", return_value=True),
        ):
            self.assertTrue(await service._activate_session())
            await service.stop_all()

        self.assertIs(captured["personalities"], cards)

    async def test_activate_session_consumes_wake_audio(self):
        service = RobotVoiceService("/tmp/missing.json", "/tmp/missing.sock", poll_seconds=0.01)
        service.active_config = service_config()
        service.audio = mock.Mock()
        service.audio.stop_io = mock.AsyncMock()
        wake_frames = [b"\x01" * 2560, b"\x02" * 2560]
        service._wake_audio = wake_frames

        captured = {}

        class FakeSession:
            def __init__(self, *args, **kwargs):
                captured["wake_audio"] = kwargs["wake_audio"]
                self.history = mock.Mock()

            async def start(self):
                pass

            async def stop(self):
                pass

        openai = types.SimpleNamespace(AsyncOpenAI=lambda api_key: mock.Mock())
        with (
            mock.patch.dict(sys.modules, {"openai": openai}),
            mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "eleven", "OPENAI_API_KEY": "openai"}),
            mock.patch("robot_voice.VoiceSession", FakeSession),
            mock.patch("robot_voice.publish_message", return_value=True),
        ):
            self.assertTrue(await service._activate_session())
            await service.stop_all()

        self.assertEqual(captured["wake_audio"], wake_frames)
        # A later talk_now-triggered session must not replay this audio.
        self.assertEqual(service._wake_audio, [])

    async def test_sample_timeline_zeros_stale_playback_rms(self):
        service = RobotVoiceService("/tmp/missing.json", "/tmp/missing.sock", poll_seconds=0.01)
        levels = AudioLevels(
            mic_peak=321,
            playback_rms=900,
            playback_at=0.0,
            threshold_rms=123,
            gate_open=True,
        )
        service.session = types.SimpleNamespace(audio_levels=levels, policy=TurnPolicy())

        with (
            mock.patch("robot_voice.time.monotonic", return_value=1.0),
            mock.patch("robot_voice.asyncio.sleep", new=mock.AsyncMock(side_effect=[None, asyncio.CancelledError])),
        ):
            with suppress(asyncio.CancelledError):
                await service._sample_timeline()

        self.assertEqual(service.timeline.levels[-1][1], 321)
        self.assertEqual(service.timeline.levels[-1][2], 0)
        self.assertEqual(service.timeline.levels[-1][3], 123)
        self.assertEqual(service.timeline.levels[-1][4], 1)
        self.assertEqual(levels.mic_peak, 0)
        self.assertTrue(levels.gate_open)


class TimelineBufferTest(unittest.TestCase):
    def test_partial_flood_does_not_evict_signal_events(self):
        buffer = TimelineBuffer()
        buffer.add_event({"type": "barge_in_fired", "t": 1.0, "reason": "user_spoke"})
        for index in range(2000):
            buffer.add_event({"type": "partial", "t": 1.5 + index * 0.001, "text": "..."})

        snapshot = buffer.snapshot(now=2.0)
        kinds = [event["type"] for event in snapshot["events"]]
        self.assertIn("barge_in_fired", kinds)

    def test_trim_drops_events_older_than_horizon(self):
        buffer = TimelineBuffer()
        buffer.add_event({"type": "barge_in_fired", "t": 0.0})
        buffer.add_event({"type": "state", "t": 0.0, "state": "listening"})
        buffer.add_event({"type": "partial", "t": 0.0, "text": "old"})
        buffer.add_event({"type": "barge_in_fired", "t": 100.0})

        buffer.trim(now=120.0)
        kinds = [event["type"] for event in buffer.snapshot(now=120.0)["events"]]
        self.assertEqual(kinds, ["barge_in_fired"])

    def test_snapshot_events_are_time_ordered(self):
        buffer = TimelineBuffer()
        buffer.add_event({"type": "state", "t": 1.0, "state": "listening"})
        buffer.add_event({"type": "partial", "t": 0.5, "text": "a"})
        buffer.add_event({"type": "barge_in_fired", "t": 2.0})
        buffer.add_event({"type": "partial", "t": 1.5, "text": "b"})

        times = [event["t"] for event in buffer.snapshot(now=3.0)["events"]]
        self.assertEqual(times, sorted(times))

    def test_snapshot_includes_turn_latency_stats(self):
        buffer = TimelineBuffer()
        buffer.add_event({"type": "partial", "t": 1.0, "text": "What do you see?"})
        buffer.add_event({"type": "turn_start", "t": 1.4, "turn_id": 1, "speculative": True, "prompt": "What do you see?"})
        buffer.add_event({"type": "turn_first_token", "t": 1.7, "turn_id": 1})
        buffer.add_event({"type": "assistant_start", "t": 2.2, "turn_id": 1})

        latency = buffer.snapshot(now=3.0)["latency"]
        last = latency["last"]

        self.assertEqual(last["turn_id"], 1)
        self.assertEqual(last["input_type"], "partial")
        self.assertEqual(last["input_to_turn_ms"], 400)
        self.assertEqual(last["turn_to_first_token_ms"], 300)
        self.assertEqual(last["turn_to_audio_ms"], 800)
        self.assertEqual(last["input_to_audio_ms"], 1200)
        self.assertEqual(latency["median_input_to_audio_ms"], 1200)

    def test_latency_stats_matches_stitched_prompt_to_commit_suffix(self):
        buffer = TimelineBuffer()
        buffer.add_event({"type": "commit", "t": 1.0, "text": "and motors please"})
        buffer.add_event(
            {
                "type": "turn_start",
                "t": 1.3,
                "turn_id": 1,
                "speculative": False,
                "prompt": "Tell me about batteries and motors please",
            }
        )
        buffer.add_event({"type": "assistant_start", "t": 1.9, "turn_id": 1})

        latency = buffer.snapshot(now=2.0)["latency"]
        last = latency["last"]

        self.assertEqual(last["input_type"], "commit")
        self.assertEqual(last["input_to_audio_ms"], 900)
        self.assertEqual(latency["median_input_to_audio_ms"], 900)


def service_config(**kwargs):
    from config.voice import VoiceConfig

    return VoiceConfig(**kwargs)


class FakeDoAReader:
    def __init__(self, readings=None, fail_reads=False):
        self.readings = readings or [DoAReading(270, True)]
        self.fail_reads = fail_reads
        self.read_count = 0
        self.closed = False

    def read(self):
        self.read_count += 1
        if self.fail_reads:
            raise OSError("USB read failed")
        return self.readings[(self.read_count - 1) % len(self.readings)]

    def close(self):
        self.closed = True


async def wait_for(predicate, timeout=1.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


class RobotVoiceDoATest(unittest.IsolatedAsyncioTestCase):
    def _service(self):
        return RobotVoiceService("/tmp/voice.json", "/tmp/missing.sock")

    async def _cancel(self, task):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def test_doa_loop_records_readings_in_tracker(self):
        service = self._service()
        reader = FakeDoAReader([DoAReading(270, True)])
        service._open_doa_reader = lambda: reader

        with mock.patch.object(robot_voice, "DOA_POLL_INTERVAL_SECONDS", 0.001):
            with mock.patch.object(service.doa_tracker, "update", wraps=service.doa_tracker.update) as update:
                task = asyncio.create_task(service._run_doa_loop())
                self.assertTrue(await wait_for(lambda: update.called))
                await self._cancel(task)

        self.assertEqual(update.call_args.args[0], DoAReading(270, True))
        self.assertIs(service.doa_reader, reader)

    async def test_doa_read_failure_does_not_terminate_loop(self):
        service = self._service()
        reader = FakeDoAReader(fail_reads=True)
        service._open_doa_reader = lambda: reader

        with mock.patch.object(robot_voice, "DOA_POLL_INTERVAL_SECONDS", 0.001):
            with mock.patch.object(robot_voice, "DOA_REOPEN_DELAY_SECONDS", 0.001):
                task = asyncio.create_task(service._run_doa_loop())
                self.assertTrue(await wait_for(lambda: reader.read_count >= 2))
                self.assertFalse(task.done())
                self.assertTrue(reader.closed)
                await self._cancel(task)

    async def test_unavailable_usb_does_not_terminate_loop(self):
        service = self._service()

        def boom():
            raise OSError("device not found")

        service._open_doa_reader = boom

        with mock.patch.object(robot_voice, "DOA_REOPEN_DELAY_SECONDS", 0.001):
            task = asyncio.create_task(service._run_doa_loop())
            self.assertTrue(await wait_for(lambda: service._doa_error_logged))
            self.assertFalse(task.done())
            self.assertIsNone(service.doa_reader)
            await self._cancel(task)

    async def test_stop_closes_reader_and_cancels_task(self):
        service = self._service()
        reader = FakeDoAReader([DoAReading(270, True)])
        service._open_doa_reader = lambda: reader

        with mock.patch.object(robot_voice, "DOA_POLL_INTERVAL_SECONDS", 0.001):
            service._doa_task = asyncio.create_task(service._run_doa_loop())
            self.assertTrue(await wait_for(lambda: service.doa_reader is reader))
            await service.stop_all()

        self.assertIsNone(service._doa_task)
        self.assertIsNone(service.doa_reader)
        self.assertTrue(reader.closed)

    def test_face_me_unavailable_without_cache(self):
        service = self._service()
        self.assertEqual(
            service.face_me_caller(),
            {"ok": False, "error": "speaker_direction_unavailable"},
        )

    def test_face_me_stale_cache(self):
        service = self._service()
        service.doa_tracker.stable_angle = 84
        service.doa_tracker.stable_at = time.monotonic() - 15.0
        self.assertEqual(
            service.face_me_caller(),
            {"ok": False, "error": "speaker_direction_stale"},
        )

    def test_face_me_fresh_cache_calls_motion(self):
        service = self._service()
        service.doa_tracker.stable_angle = 0
        service.doa_tracker.stable_at = time.monotonic()

        with mock.patch.object(
            robot_voice, "request_motion_intent", return_value={"ok": True, "result": "completed"}
        ) as request:
            result = service.face_me_caller()

        request.assert_called_once_with(
            service.motion_intent_socket,
            "face_me",
            timeout=robot_voice.FACE_ME_MOTION_TIMEOUT_SECONDS,
            relative_degrees=90,
        )
        self.assertEqual(result, {"ok": True, "result": "completed"})

    def test_doa_snapshot_without_cache(self):
        service = self._service()
        self.assertEqual(
            service.doa_snapshot(),
            {"connected": False, "relative_degrees": None, "age_seconds": None, "fresh": False},
        )

    def test_doa_snapshot_fresh_cache(self):
        service = self._service()
        service.doa_reader = FakeDoAReader()
        service.doa_tracker.stable_angle = 0
        service.doa_tracker.stable_at = time.monotonic()
        snapshot = service.doa_snapshot()
        self.assertTrue(snapshot["connected"])
        self.assertEqual(snapshot["relative_degrees"], 90)
        self.assertTrue(snapshot["fresh"])

    def test_doa_snapshot_stale_cache(self):
        service = self._service()
        service.doa_tracker.stable_angle = 0
        service.doa_tracker.stable_at = time.monotonic() - 15.0
        snapshot = service.doa_snapshot()
        self.assertEqual(snapshot["relative_degrees"], 90)
        self.assertFalse(snapshot["fresh"])
        self.assertGreaterEqual(snapshot["age_seconds"], 10.0)

    def test_face_me_already_facing_skips_motion(self):
        service = self._service()
        service.doa_tracker.stable_angle = 270
        service.doa_tracker.stable_at = time.monotonic()

        with mock.patch.object(robot_voice, "request_motion_intent") as request:
            result = service.face_me_caller()

        request.assert_not_called()
        self.assertEqual(
            result,
            {"ok": True, "result": "already_facing", "relative_degrees": 0},
        )


if __name__ == "__main__":
    unittest.main()
