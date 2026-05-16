from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


SPECULATIVE_PARTIAL_DELAY_SECS = 0.35
SPECULATIVE_PLAYBACK_DELAY_SECS = 0.8
SPECULATIVE_LOCAL_QUIET_SECS = 0.65
SPECULATIVE_CONFIRM_SIMILARITY = 0.82
LOCAL_SPEECH_RMS_THRESHOLD = 500
LOCAL_SPEECH_WINDOW_SECS = 1.2


@dataclass(frozen=True)
class TurnPolicy:
    speculative_partial_delay_secs: float = SPECULATIVE_PARTIAL_DELAY_SECS
    speculative_playback_delay_secs: float = SPECULATIVE_PLAYBACK_DELAY_SECS
    speculative_local_quiet_secs: float = SPECULATIVE_LOCAL_QUIET_SECS
    confirm_similarity: float = SPECULATIVE_CONFIRM_SIMILARITY
    min_speculative_words: int = 3
    min_speculative_chars: int = 10
    complete_unpunctuated_speculative_words: int = 6
    min_barge_in_words: int = 3
    min_barge_in_chars: int = 12
    local_speech_rms_threshold: int = LOCAL_SPEECH_RMS_THRESHOLD
    local_speech_window_secs: float = LOCAL_SPEECH_WINDOW_SECS
    assistant_speech_barge_in_cooldown_secs: float = 0.35
    assistant_echo_similarity: float = 0.9
    assistant_echo_recent_words: int = 60
    assistant_echo_window_slop_words: int = 2
    explicit_interrupt_scan_words: int = 3
    incomplete_partial_suffixes: tuple[str, ...] = ("-", ",", ":", ";")
    complete_partial_suffixes: tuple[str, ...] = (".", "?", "!")
    explicit_interrupt_words: frozenset[str] = field(
        default_factory=lambda: frozenset({"stop", "wait", "no", "cancel", "pause"})
    )

    def normalized_transcript(self, text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()

    def transcript_matches(self, left: str, right: str) -> bool:
        normalized_left = self.normalized_transcript(left)
        normalized_right = self.normalized_transcript(right)
        if normalized_left in normalized_right or normalized_right in normalized_left:
            return True
        return difflib.SequenceMatcher(None, normalized_left, normalized_right).ratio() >= self.confirm_similarity

    def looks_incomplete_partial(self, text: str) -> bool:
        return text.strip().endswith(self.incomplete_partial_suffixes)

    def transcript_features(self, text: str) -> dict[str, object]:
        normalized = self.normalized_transcript(text)
        words = re.findall(r"\S+", text)
        return {
            "text": text,
            "normalized": normalized,
            "word_count": len(words),
            "char_count": len(text),
        }

    def speculation_decision(self, text: str) -> tuple[bool, str]:
        words = re.findall(r"\S+", text)
        if len(words) < self.min_speculative_words or len(text) < self.min_speculative_chars:
            return False, "too_short"
        if self.looks_incomplete_partial(text):
            return False, "incomplete_partial"
        if text.rstrip().endswith(self.complete_partial_suffixes):
            return True, "terminal_punctuation"
        if len(words) >= self.complete_unpunctuated_speculative_words:
            return True, "enough_words"
        return False, "waiting_for_more_words"

    def should_speculate(self, text: str) -> bool:
        should_speculate, _reason = self.speculation_decision(text)
        return should_speculate

    def local_quiet_remaining_secs(self, now: float, last_local_speech_at: float) -> float:
        return max(0.0, self.speculative_local_quiet_secs - (now - last_local_speech_at))

    def commit_decision(self, text: str) -> tuple[bool, str]:
        features = self.transcript_features(text)
        if self.has_explicit_interrupt(text) and features["word_count"] <= self.explicit_interrupt_scan_words:
            return False, "interrupt_only"
        if features["word_count"] < self.min_speculative_words or features["char_count"] < self.min_speculative_chars:
            return False, "too_short_commit"
        return True, "committed_transcript"

    def is_barge_in_candidate(self, text: str) -> bool:
        words = re.findall(r"\S+", text)
        normalized = self.normalized_transcript(text)
        normalized_words = normalized.split()
        early_words = normalized_words[: self.explicit_interrupt_scan_words]
        return (
            len(words) >= self.min_barge_in_words
            and len(normalized) >= self.min_barge_in_chars
        ) or any(word in self.explicit_interrupt_words for word in early_words)

    def has_explicit_interrupt(self, text: str) -> bool:
        normalized_words = self.normalized_transcript(text).split()
        return any(word in self.explicit_interrupt_words for word in normalized_words[: self.explicit_interrupt_scan_words])

    def matches_assistant_echo(self, text: str, assistant_text: str) -> bool:
        partial = self.normalized_transcript(text)
        assistant = self.normalized_transcript(assistant_text)
        partial_words = partial.split()
        assistant_words = assistant.split()[-self.assistant_echo_recent_words :]

        if (
            len(partial_words) < self.min_barge_in_words
            or len(partial) < self.min_barge_in_chars
            or len(assistant_words) < len(partial_words)
        ):
            return False

        recent_assistant = " ".join(assistant_words)
        if partial in recent_assistant:
            return True

        min_window_words = max(1, len(partial_words) - self.assistant_echo_window_slop_words)
        max_window_words = len(partial_words) + self.assistant_echo_window_slop_words
        for window_word_count in range(min_window_words, max_window_words + 1):
            if window_word_count > len(assistant_words):
                continue
            for start in range(0, len(assistant_words) - window_word_count + 1):
                window = " ".join(assistant_words[start : start + window_word_count])
                if difflib.SequenceMatcher(None, partial, window).ratio() >= self.assistant_echo_similarity:
                    return True

        return False

    def should_accept_barge_in(
        self,
        text: str,
        assistant_speaking: bool,
        local_speech_recent: bool,
        assistant_speech_elapsed_secs: float | None = None,
        local_speech_rms: int | None = None,
        assistant_text: str = "",
    ) -> bool:
        should_accept, _reason = self.barge_in_decision(
            text,
            assistant_speaking,
            local_speech_recent,
            assistant_speech_elapsed_secs,
            local_speech_rms,
            assistant_text,
        )
        return should_accept

    def barge_in_decision(
        self,
        text: str,
        assistant_speaking: bool,
        local_speech_recent: bool,
        assistant_speech_elapsed_secs: float | None = None,
        local_speech_rms: int | None = None,
        assistant_text: str = "",
    ) -> tuple[bool, str]:
        if not assistant_speaking or not local_speech_recent:
            return False, "no_local_speech" if assistant_speaking else "assistant_not_speaking"
        if local_speech_rms is not None and local_speech_rms < self.local_speech_rms_threshold:
            return False, "low_rms"
        if self.has_explicit_interrupt(text):
            return True, "explicit_interrupt"
        if (
            assistant_speech_elapsed_secs is not None
            and assistant_speech_elapsed_secs < self.assistant_speech_barge_in_cooldown_secs
        ):
            return False, "cooldown"
        if self.matches_assistant_echo(text, assistant_text):
            return False, "assistant_echo"
        if self.is_barge_in_candidate(text):
            return True, "substantial_partial"
        return False, "too_short"

    def should_replace_speculative_prompt(self, partial_text: str, active_prompt: str) -> bool:
        return (
            partial_text != active_prompt
            and self.transcript_matches(partial_text, active_prompt)
            and self.should_speculate(partial_text)
        )


DEFAULT_TURN_POLICY = TurnPolicy()


def normalized_transcript(text: str) -> str:
    return DEFAULT_TURN_POLICY.normalized_transcript(text)


def transcript_matches(left: str, right: str) -> bool:
    return DEFAULT_TURN_POLICY.transcript_matches(left, right)


def should_speculate(text: str) -> bool:
    return DEFAULT_TURN_POLICY.should_speculate(text)


def pcm16_rms(chunk: bytes) -> int:
    samples = memoryview(chunk).cast("h")
    if len(samples) == 0:
        return 0
    return int((sum(sample * sample for sample in samples) / len(samples)) ** 0.5)


def should_accept_barge_in(
    text: str,
    assistant_speaking: bool,
    local_speech_recent: bool,
    assistant_speech_elapsed_secs: float | None = None,
    local_speech_rms: int | None = None,
) -> bool:
    return DEFAULT_TURN_POLICY.should_accept_barge_in(
        text,
        assistant_speaking,
        local_speech_recent,
        assistant_speech_elapsed_secs,
        local_speech_rms,
    )
