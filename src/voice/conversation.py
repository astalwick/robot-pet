from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class ConversationExchange:
    user_text: str
    assistant_text: str


class ConversationHistory:
    def __init__(self, max_exchanges: int = 20) -> None:
        self._exchanges: deque[ConversationExchange] = deque(maxlen=max_exchanges)

    def append_exchange(self, user_text: str, assistant_text: str) -> None:
        self._exchanges.append(ConversationExchange(user_text=user_text, assistant_text=assistant_text))

    def input_for(self, prompt: str, system_prompt: str) -> list[dict[str, str]]:
        openai_input = [{"role": "system", "content": system_prompt}]
        for exchange in self._exchanges:
            openai_input.append({"role": "user", "content": exchange.user_text})
            openai_input.append({"role": "assistant", "content": exchange.assistant_text})
        openai_input.append({"role": "user", "content": prompt})
        return openai_input

    def exchanges(self) -> tuple[ConversationExchange, ...]:
        return tuple(self._exchanges)
