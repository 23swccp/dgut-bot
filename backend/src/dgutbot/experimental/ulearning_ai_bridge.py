"""Small, opt-in bridge from chat-style messages to the course AI service."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .ulearning_ai import AiModel, UlearningAiClient, UlearningAiError, new_protocol_id
from .ulearning_ai_browser import BrowserAiAccess, discover_browser_access


MAX_MESSAGES = 64
MAX_PROMPT_CHARS = 32_768
ALLOWED_ROLES = {"system", "user", "assistant"}
DEFAULT_OPERATION_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class BridgeReply:
    text: str
    reasoning: str
    upstream_tool_calls: tuple[dict[str, Any], ...] = ()


def flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Convert an OpenAI-style transcript into one upstream query."""
    if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES:
        raise ValueError("messages must contain between 1 and 64 entries")
    normalized = []
    for item in messages:
        if not isinstance(item, dict):
            raise ValueError("each message must be an object")
        role = str(item.get("role") or "")
        content = item.get("content")
        if role not in ALLOWED_ROLES or not isinstance(content, str) or not content.strip():
            raise ValueError("messages require a supported role and non-empty text content")
        normalized.append((role, content.strip()))
    if normalized[-1][0] != "user":
        raise ValueError("the final message must have role user")
    if len(normalized) == 1:
        prompt = normalized[0][1]
    else:
        transcript = "\n\n".join(f"[{role}]\n{content}" for role, content in normalized)
        prompt = "以下是本地客户端提供的对话上下文。请回答最后一条 user 消息。\n\n" + transcript
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError("the combined prompt is too large")
    return prompt


class UlearningAiBridge:
    """Serialize upstream calls and refresh browser credentials per request."""

    def __init__(
        self,
        *,
        debug_port: int | Callable[[], int] = 9222,
        access_factory: Callable[..., BrowserAiAccess] = discover_browser_access,
        client_factory: Callable[..., UlearningAiClient] = UlearningAiClient,
        operation_interval: float = DEFAULT_OPERATION_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._debug_port = debug_port
        self._access_factory = access_factory
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._operation_interval = max(0.0, float(operation_interval))
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_operation_finished_at: float | None = None

    def _wait_for_operation_slot(self) -> None:
        """Keep logical upstream operations serial and conservatively paced."""
        if self._last_operation_finished_at is None:
            return
        remaining = self._operation_interval - (self._monotonic() - self._last_operation_finished_at)
        if remaining > 0:
            self._sleep(remaining)

    def _finish_operation(self) -> None:
        self._last_operation_finished_at = self._monotonic()

    def models(self) -> tuple[AiModel, ...]:
        """Return the currently enabled upstream models without exposing access data."""
        with self._lock:
            self._wait_for_operation_slot()
            try:
                debug_port = self._debug_port() if callable(self._debug_port) else self._debug_port
                access = self._access_factory(int(debug_port))
                return self._client_factory(access.create_session()).list_models()
            finally:
                self._finish_operation()

    def probe(self, model_id: int | None = None) -> None:
        """Verify that a workbench and the selected model are usable."""
        models = self.models()
        if model_id is not None and int(model_id) not in {model.id for model in models}:
            raise UlearningAiError("The selected AI model is unavailable.")

    def complete(self, messages: list[dict[str, Any]], *, model_id: int = 1) -> BridgeReply:
        prompt = flatten_messages(messages)
        with self._lock:
            self._wait_for_operation_slot()
            try:
                debug_port = self._debug_port() if callable(self._debug_port) else self._debug_port
                access = self._access_factory(int(debug_port))
                client = self._client_factory(access.create_session())
                models = client.list_models()
                if int(model_id) not in {model.id for model in models}:
                    raise UlearningAiError("The selected AI model is unavailable.")
                chunks = list(client.stream_chat(
                    access.context,
                    request_id=new_protocol_id(),
                    query=prompt,
                    model_id=int(model_id),
                ))
            finally:
                self._finish_operation()
        text = "".join(chunk.text for chunk in chunks)
        reasoning = "".join(chunk.reasoning for chunk in chunks)
        tool_calls = tuple(call for chunk in chunks for call in chunk.tool_calls)
        if not text and not reasoning and not tool_calls:
            raise UlearningAiError("The AI service returned an empty response.")
        return BridgeReply(text=text, reasoning=reasoning, upstream_tool_calls=tool_calls)
