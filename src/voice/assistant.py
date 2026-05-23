from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from voice.conversation import ConversationHistory
from voice.turn_policy import DEFAULT_TURN_POLICY, TurnPolicy


OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_VOICE_ID = "Ct9jL3ofSaf3bjiuX3cL"
ALTERNATE_VOICE_ID = "Pj4KiuLufWTFgLAn5sAM"
DEFAULT_SYSTEM_PROMPT = (
    "You are a voice assistant named Bloop. Keep responses brief. "
    "Answer naturally in one or two sentences. Avoid markdown unless the user explicitly asks for it. "
    "You can use the switch_voice tool to toggle between the default and alternate speaking voices. "
    "Only call switch_voice when the user explicitly asks you to switch, change, or toggle voices."
)
VOICE_SWITCH_TOOL_NAME = "switch_voice"
PLAYBACK_RMS_STALE_SECS = 0.25


def effective_playback_rms(audio_levels: dict[str, float | int], now: float) -> int:
    playback_at = float(audio_levels.get("playback_at", 0.0))
    if now - playback_at > PLAYBACK_RMS_STALE_SECS:
        return 0
    return int(audio_levels.get("playback_rms", 0))


def update_near_end_gate(
    policy: TurnPolicy,
    gate_above_since: float | None,
    now: float,
    mic_rms: int,
    playback_rms: int,
) -> tuple[float | None, bool, int, str]:
    threshold_rms = policy.dynamic_barge_in_threshold_rms(playback_rms)
    if mic_rms < threshold_rms:
        return None, False, threshold_rms, "low_rms"

    if gate_above_since is None:
        gate_above_since = now

    sustained_ms = (now - gate_above_since) * 1000
    if sustained_ms >= policy.barge_in_sustain_ms:
        return gate_above_since, True, threshold_rms, "substantial_partial"
    return gate_above_since, False, threshold_rms, "not_sustained"


def barge_in_telemetry(
    policy: TurnPolicy,
    mic_rms: int,
    playback_rms: int,
    threshold_rms: int,
    gate_open: bool,
    last_reason: str,
) -> dict[str, object]:
    return {
        "barge_in_enabled": policy.barge_in_enabled,
        "barge_in_threshold_rms": threshold_rms,
        "barge_in_mic_rms": mic_rms,
        "barge_in_playback_rms": playback_rms,
        "barge_in_gate_open": gate_open,
        "barge_in_last_reason": last_reason,
    }


VOICE_SWITCH_TOOL = {
    "type": "function",
    "name": VOICE_SWITCH_TOOL_NAME,
    "description": "Toggle between the default and alternate speaking voices. Only use when the user explicitly asks to switch voices.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}


@dataclass(frozen=True)
class VoiceSwitch:
    voice_id: str
    voice_name: str


@dataclass
class VoiceState:
    default_voice_id: str
    alternate_voice_id: str
    current_voice_id: str

    def toggle(self) -> VoiceSwitch:
        if self.current_voice_id == self.default_voice_id:
            self.current_voice_id = self.alternate_voice_id
            return VoiceSwitch(voice_id=self.current_voice_id, voice_name="alternate")
        self.current_voice_id = self.default_voice_id
        return VoiceSwitch(voice_id=self.current_voice_id, voice_name="default")


async def cancel_task(task: asyncio.Task[Any] | None) -> None:
    if task and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@dataclass
class ActiveTurn:
    turn_id: int
    prompt: str
    speculative: bool
    task: asyncio.Task[str]
    playback_event: asyncio.Event = field(default_factory=asyncio.Event)
    speaking_event: asyncio.Event = field(default_factory=asyncio.Event)
    playback_release_task: asyncio.Task[None] | None = None
    speech_started_at: float | None = None
    assistant_streamed_chunks: list[str] = field(default_factory=list)
    committed_text: str | None = None
    assistant_text: str | None = None
    history_committed: bool = False

    def __post_init__(self) -> None:
        if not self.speculative:
            self.committed_text = self.prompt
            self.open_playback()

    def is_active(self) -> bool:
        return not self.task.done()

    def is_speaking(self) -> bool:
        return self.speaking_event.is_set()

    def mark_speech_started(self, now: float) -> None:
        if self.speech_started_at is None:
            self.speech_started_at = now

    def speech_elapsed_secs(self, now: float) -> float | None:
        if self.speech_started_at is None:
            return None
        return now - self.speech_started_at

    def open_playback(self) -> None:
        self.playback_event.set()

    def assistant_streamed_text(self) -> str:
        return "".join(self.assistant_streamed_chunks)

    async def confirm(self, commit_text: str) -> None:
        self.committed_text = commit_text
        self.prompt = commit_text
        self.speculative = False
        self.open_playback()
        await cancel_task(self.playback_release_task)
        self.playback_release_task = None

    def request_cancel(self, reason: str) -> None:
        if self.playback_release_task and not self.playback_release_task.done():
            self.playback_release_task.cancel()
        self.playback_release_task = None
        self.playback_event.clear()
        self.speaking_event.clear()
        if not self.task.done():
            self.task.cancel()

    async def cancel(self, reason: str) -> None:
        playback_release_task = self.playback_release_task
        self.request_cancel(reason)
        await cancel_task(playback_release_task)
        await cancel_task(self.task)


async def stream_openai_words(
    openai_input: list[dict[str, str]],
    openai_client: Any,
    voice_state: VoiceState,
) -> AsyncIterator[str | VoiceSwitch]:
    pending = ""
    word_buffer: list[str] = []
    response_input: object = openai_input
    previous_response_id: str | None = None

    while True:
        create_kwargs: dict[str, Any] = {
            "model": OPENAI_MODEL,
            "input": response_input,
            "reasoning": {"effort": "none"},
            "tools": [VOICE_SWITCH_TOOL],
            "stream": True,
        }
        if previous_response_id:
            create_kwargs["previous_response_id"] = previous_response_id

        stream = await openai_client.responses.create(**create_kwargs)
        function_calls: list[Any] = []
        response_id = previous_response_id

        async for event in stream:
            event_type = getattr(event, "type", "")
            delta = getattr(event, "delta", "")

            if event_type in {"response.text_delta", "response.output_text.delta"}:
                if not delta:
                    continue

                pending += delta
                pieces = re.findall(r"\S+\s*", pending)

                if pending and not pending[-1].isspace() and pieces:
                    pending = pieces.pop()
                else:
                    pending = ""

                word_buffer.extend(pieces)
                while len(word_buffer) >= 3:
                    yield "".join(word_buffer[:3])
                    del word_buffer[:3]
                continue

            if event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                if getattr(item, "type", "") == "function_call":
                    function_calls.append(item)
                continue

            if event_type == "response.completed":
                response = getattr(event, "response", None)
                response_id = getattr(response, "id", response_id)

        if pending:
            word_buffer.append(pending)
            pending = ""
        if word_buffer:
            yield "".join(word_buffer)
            word_buffer.clear()

        if not function_calls:
            return

        tool_outputs: list[dict[str, str]] = []
        for function_call in function_calls:
            call_id = getattr(function_call, "call_id", "")
            if getattr(function_call, "name", "") != VOICE_SWITCH_TOOL_NAME:
                tool_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"error": "unsupported tool"}),
                    }
                )
                continue

            voice_switch = voice_state.toggle()
            yield voice_switch
            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(
                        {
                            "voice": voice_switch.voice_name,
                            "voice_id": voice_switch.voice_id,
                        }
                    ),
                }
            )

        previous_response_id = response_id
        response_input = tool_outputs


async def run_assistant_turn(
    turn_id: int,
    openai_input: list[dict[str, str]],
    playback_event: asyncio.Event,
    speaking_event: asyncio.Event,
    openai_client: Any,
    elevenlabs_api_key: str,
    voice_state: VoiceState,
    on_assistant_chunk: Callable[[str], None] | None = None,
    tts_speaker: Callable[..., Any] | None = None,
) -> str:
    from voice.elevenlabs_io import speak_with_eleven_flash

    assistant_chunks: list[str] = []

    async def captured_openai_words() -> AsyncIterator[str | VoiceSwitch]:
        async for chunk in stream_openai_words(openai_input, openai_client, voice_state):
            if isinstance(chunk, str):
                assistant_chunks.append(chunk)
                if on_assistant_chunk:
                    on_assistant_chunk(chunk)
            yield chunk

    speaker = tts_speaker or speak_with_eleven_flash
    await speaker(
        captured_openai_words(),
        elevenlabs_api_key,
        voice_state.current_voice_id,
        playback_event,
        speaking_event,
    )
    return "".join(assistant_chunks).strip()


async def handle_scribe_events(
    scribe_events: asyncio.Queue[dict[str, object]],
    openai_client: Any,
    elevenlabs_api_key: str,
    voice_state: VoiceState,
    stop_event: asyncio.Event,
    policy: TurnPolicy = DEFAULT_TURN_POLICY,
    audio_levels: dict[str, float | int] | None = None,
    conversation_history: ConversationHistory | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    on_status: Callable[[dict[str, object]], None] | None = None,
    on_event: Callable[[dict[str, object]], None] | None = None,
    assistant_runner: Callable[..., Any] = run_assistant_turn,
) -> None:
    active_turn: ActiveTurn | None = None
    history = conversation_history if conversation_history is not None else ConversationHistory()
    next_turn_id = 0
    debounce_task: asyncio.Task[None] | None = None
    last_local_speech_at = 0.0
    last_local_speech_rms = 0
    gate_above_since: float | None = None
    gate_open = False
    gate_threshold_rms = policy.barge_in_min_rms
    gate_last_reason = "assistant_not_speaking"
    barge_in_event_count = 0
    barge_in_hearing_reported = False
    levels = audio_levels if audio_levels is not None else {"mic_rms": 0, "playback_rms": 0, "playback_at": 0.0}
    recent_assistant_text = ""
    recent_assistant_echo_until = 0.0
    hearing_on = False
    thinking_on = False

    def status(**values: object) -> None:
        nonlocal hearing_on, thinking_on
        if on_status:
            on_status(values)
        if not on_event or "status" not in values:
            return
        new_status = values["status"]
        if new_status not in {"hearing", "thinking", "listening"}:
            return
        new_hearing = new_status == "hearing"
        new_thinking = new_status == "thinking"
        if new_hearing != hearing_on:
            hearing_on = new_hearing
            emit("phase", name="hearing", on=hearing_on)
        if new_thinking != thinking_on:
            thinking_on = new_thinking
            emit("phase", name="thinking", on=thinking_on)

    def emit(kind: str, **payload: object) -> None:
        if not on_event:
            return
        event = {"type": kind, "t": time.monotonic(), **payload}
        on_event(event)

    def publish_barge_in_state(now: float, mic_rms: int | None = None) -> None:
        nonlocal gate_above_since, gate_open, gate_threshold_rms, gate_last_reason
        mic = last_local_speech_rms if mic_rms is None else mic_rms
        playback_rms = effective_playback_rms(levels, now)
        if active_turn and active_turn.is_active() and active_turn.is_speaking():
            gate_above_since, gate_open, gate_threshold_rms, gate_last_reason = update_near_end_gate(
                policy,
                gate_above_since,
                now,
                mic,
                playback_rms,
            )
        else:
            gate_above_since = None
            gate_open = False
            gate_last_reason = "assistant_not_speaking"
            gate_threshold_rms = policy.dynamic_barge_in_threshold_rms(playback_rms)
        levels["threshold_rms"] = gate_threshold_rms
        levels["gate_open"] = 1 if gate_open else 0
        status(
            **barge_in_telemetry(
                policy,
                mic,
                playback_rms,
                gate_threshold_rms,
                gate_open,
                gate_last_reason,
            )
        )

    def publish_barge_in_event(source: str, reason: str) -> None:
        nonlocal barge_in_event_count
        barge_in_event_count += 1
        status(
            barge_in_event_count=barge_in_event_count,
            barge_in_last_event=f"{source}: {reason}",
        )
        emit("barge_in_fired", source=source, reason=reason)

    def publish_barge_in_hearing(source: str) -> None:
        nonlocal barge_in_hearing_reported
        if barge_in_hearing_reported:
            return
        barge_in_hearing_reported = True
        publish_barge_in_event(source, "hearing")

    async def cancel_active_turn(reason: str) -> None:
        nonlocal active_turn
        turn = active_turn
        active_turn = None
        if turn and (turn.is_active() or (turn.playback_release_task and not turn.playback_release_task.done())):
            emit("turn_cancel", turn_id=turn.turn_id, reason=reason, was_speaking=turn.is_speaking())
            streamed = turn.assistant_streamed_text().strip()
            if streamed:
                emit("assistant", turn_id=turn.turn_id, text=streamed, cancelled=True)
            turn.request_cancel(reason)

    async def release_speculative_playback(turn: ActiveTurn) -> None:
        await asyncio.sleep(policy.speculative_playback_delay_secs)
        while active_turn is turn and turn.is_active() and not turn.playback_event.is_set():
            quiet_remaining_secs = policy.local_quiet_remaining_secs(asyncio.get_running_loop().time(), last_local_speech_at)
            if quiet_remaining_secs <= 0:
                turn.open_playback()
                return
            await asyncio.sleep(quiet_remaining_secs)

    async def maybe_commit_history(turn: ActiveTurn) -> None:
        nonlocal recent_assistant_text, recent_assistant_echo_until
        if active_turn is not turn or turn.history_committed or turn.speculative or not turn.task.done():
            return
        try:
            assistant_text = turn.task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            status(status="error", last_error=str(exc))
            return
        turn.assistant_text = assistant_text
        recent_assistant_text = assistant_text
        recent_assistant_echo_until = asyncio.get_running_loop().time() + policy.assistant_echo_memory_secs
        history.append_exchange(turn.committed_text or turn.prompt, assistant_text)
        turn.history_committed = True
        if assistant_text:
            emit("assistant", turn_id=turn.turn_id, text=assistant_text)
        status(status="listening", assistant_speaking=False, last_assistant_text=assistant_text)

    async def start_turn(prompt: str, speculative: bool) -> None:
        nonlocal active_turn, next_turn_id, barge_in_hearing_reported
        await cancel_active_turn("new_turn")
        barge_in_hearing_reported = False
        next_turn_id += 1
        playback_event = asyncio.Event()
        speaking_event = asyncio.Event()
        assistant_streamed_chunks: list[str] = []
        openai_input = history.input_for(prompt, system_prompt)
        task = asyncio.create_task(
            assistant_runner(
                next_turn_id,
                openai_input,
                playback_event,
                speaking_event,
                openai_client,
                elevenlabs_api_key,
                voice_state,
                on_assistant_chunk=assistant_streamed_chunks.append,
            )
        )
        turn = ActiveTurn(
            turn_id=next_turn_id,
            prompt=prompt,
            speculative=speculative,
            task=task,
            playback_event=playback_event,
            speaking_event=speaking_event,
            assistant_streamed_chunks=assistant_streamed_chunks,
        )
        task.add_done_callback(lambda _task, completed_turn=turn: scribe_events.put_nowait({"type": "assistant_done", "turn": completed_turn}))
        active_turn = turn
        emit("turn_start", turn_id=next_turn_id, speculative=speculative, prompt=prompt)
        status(status="thinking", assistant_speaking=False)
        if speculative:
            active_turn.playback_release_task = asyncio.create_task(release_speculative_playback(active_turn))

    async def start_after_stable_partial(text: str) -> None:
        await asyncio.sleep(policy.speculative_partial_delay_secs)
        while True:
            should_start, _reason = policy.speculation_decision(text)
            if not should_start:
                return
            quiet_remaining_secs = policy.local_quiet_remaining_secs(asyncio.get_running_loop().time(), last_local_speech_at)
            if quiet_remaining_secs > 0:
                await asyncio.sleep(quiet_remaining_secs)
                continue
            if active_turn and active_turn.is_active() and policy.transcript_matches(text, active_turn.prompt):
                return
            await start_turn(text, speculative=True)
            return

    async def handle_partial(text: str) -> None:
        nonlocal debounce_task, gate_last_reason
        now = asyncio.get_running_loop().time()
        emit("partial", text=text)
        if is_recent_assistant_echo(text, now):
            await cancel_task(debounce_task)
            debounce_task = None
            emit("echo_suppressed", source="partial", text=text)
            status(status="listening")
            return

        status(status="hearing", partial_transcript=text)
        if active_turn and active_turn.is_active() and active_turn.is_speaking():
            active_turn.mark_speech_started(now)
            publish_barge_in_hearing("stt")
            publish_barge_in_state(now)
            playback_rms = effective_playback_rms(levels, now)
            should_barge_in, gate_last_reason = policy.barge_in_decision(
                text,
                assistant_speaking=True,
                gate_open=gate_open,
                assistant_speech_elapsed_secs=active_turn.speech_elapsed_secs(now),
                mic_rms=last_local_speech_rms,
                playback_rms=playback_rms,
                gate_reason=gate_last_reason,
                assistant_text=active_turn.assistant_streamed_text(),
            )
            emit(
                "barge_in_considered",
                source="partial",
                accepted=should_barge_in,
                reason=gate_last_reason,
                mic=last_local_speech_rms,
                playback=playback_rms,
                threshold=gate_threshold_rms,
            )
            status(
                **barge_in_telemetry(
                    policy,
                    last_local_speech_rms,
                    playback_rms,
                    gate_threshold_rms,
                    gate_open,
                    gate_last_reason,
                )
            )
            if should_barge_in:
                publish_barge_in_event("partial", gate_last_reason)
                await cancel_active_turn("barge_in")
                await cancel_task(debounce_task)
                debounce_task = asyncio.create_task(start_after_stable_partial(text))
            return

        if active_turn and active_turn.speculative and text != active_turn.prompt and policy.transcript_matches(text, active_turn.prompt):
            if policy.should_replace_speculative_prompt(text, active_turn.prompt):
                await start_turn(text, speculative=True)
            else:
                if policy.looks_incomplete_partial(text) and active_turn.playback_release_task:
                    await cancel_task(active_turn.playback_release_task)
                    active_turn.playback_release_task = None
            return

        await cancel_task(debounce_task)
        debounce_task = asyncio.create_task(start_after_stable_partial(text))

    async def handle_commit(text: str) -> None:
        nonlocal debounce_task, gate_last_reason
        await cancel_task(debounce_task)
        debounce_task = None
        now = asyncio.get_running_loop().time()
        emit("commit", text=text)
        if is_recent_assistant_echo(text, now):
            emit("echo_suppressed", source="commit", text=text)
            status(status="listening")
            return

        should_start_from_commit, commit_reason = policy.commit_decision(text)
        emit("commit_decision", accepted=should_start_from_commit, reason=commit_reason, text=text)

        if (
            active_turn
            and active_turn.is_active()
            and active_turn.is_speaking()
            and not policy.transcript_matches(text, active_turn.prompt)
        ):
            active_turn.mark_speech_started(now)
            publish_barge_in_hearing("stt")
            publish_barge_in_state(now)
            playback_rms = effective_playback_rms(levels, now)
            should_barge_in, gate_last_reason = policy.barge_in_decision(
                text,
                assistant_speaking=True,
                gate_open=gate_open,
                assistant_speech_elapsed_secs=active_turn.speech_elapsed_secs(now),
                mic_rms=last_local_speech_rms,
                playback_rms=playback_rms,
                gate_reason=gate_last_reason,
                assistant_text=active_turn.assistant_streamed_text(),
            )
            emit(
                "barge_in_considered",
                source="commit",
                accepted=should_barge_in,
                reason=gate_last_reason,
                mic=last_local_speech_rms,
                playback=playback_rms,
                threshold=gate_threshold_rms,
            )
            status(
                **barge_in_telemetry(
                    policy,
                    last_local_speech_rms,
                    playback_rms,
                    gate_threshold_rms,
                    gate_open,
                    gate_last_reason,
                )
            )
            if not should_barge_in:
                return

            publish_barge_in_event("commit", gate_last_reason)
            await cancel_active_turn("barge_in_commit")
            if should_start_from_commit:
                status(status="thinking", partial_transcript=None, last_committed_transcript=text)
                await start_turn(text, speculative=False)
            else:
                status(status="listening", partial_transcript=None, last_committed_transcript=text)
            return

        status(status="thinking", partial_transcript=None, last_committed_transcript=text)

        if active_turn and active_turn.is_active():
            if policy.transcript_matches(text, active_turn.prompt):
                await active_turn.confirm(text)
                await maybe_commit_history(active_turn)
            else:
                if not should_start_from_commit:
                    return
                await cancel_active_turn("commit_mismatch")
                await start_turn(text, speculative=False)
        elif active_turn and policy.transcript_matches(text, active_turn.prompt):
            await active_turn.confirm(text)
            await maybe_commit_history(active_turn)
        else:
            if not should_start_from_commit:
                return
            await start_turn(text, speculative=False)

    def is_recent_assistant_echo(text: str, now: float) -> bool:
        if policy.has_explicit_interrupt(text):
            return False
        assistant_text = ""
        if active_turn is not None:
            assistant_text = active_turn.assistant_streamed_text()
        if recent_assistant_text and now <= recent_assistant_echo_until:
            assistant_text = f"{assistant_text} {recent_assistant_text}".strip()
        return bool(assistant_text and policy.matches_assistant_echo(text, assistant_text))

    try:
        while not stop_event.is_set():
            event = await scribe_events.get()
            event_type = str(event["type"])
            if event_type == "assistant_done":
                await maybe_commit_history(event["turn"])
                continue

            text = str(event.get("text", ""))

            if event_type == "audio_activity":
                now = asyncio.get_running_loop().time()
                last_local_speech_rms = int(event.get("rms", 0))
                if last_local_speech_rms >= policy.local_speech_rms_threshold:
                    last_local_speech_at = now
                levels["mic_rms"] = last_local_speech_rms
                publish_barge_in_state(now, last_local_speech_rms)
                continue

            if event_type == "partial":
                await handle_partial(text)
                continue

            if event_type == "commit":
                await handle_commit(text)
    finally:
        await cancel_task(debounce_task)
        if active_turn:
            await active_turn.cancel("shutdown")
