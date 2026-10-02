"""Experimental client for the course AI assistant's observed SSE protocol.

Authentication is intentionally outside this module.  Callers must provide a
short-lived, in-memory ``requests.Session`` obtained from the active debugging
browser; credentials must never be serialized or logged.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

import requests


DEFAULT_BASE_URL = "https://aijx.dgut.edu.cn"
MAX_IDENTIFIER_LENGTH = 128
IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]+$")


class UlearningAiError(RuntimeError):
    """A safe error that never includes request headers or credential values."""


@dataclass(frozen=True)
class ChatContext:
    assistant_id: str
    course_id: str
    session_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("assistant_id", self.assistant_id),
            ("course_id", self.course_id),
            ("session_id", self.session_id),
        ):
            validate_identifier(name, value)


@dataclass(frozen=True)
class ChatChunk:
    text: str = ""
    reasoning: str = ""
    done: bool = False
    tool_calls: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class AiModel:
    id: int
    name: str
    vision: bool = False
    online: bool = False
    thinking: bool = False


def validate_identifier(name: str, value: str) -> str:
    """Validate opaque protocol identifiers without assuming their generator."""
    text = str(value)
    if not text or len(text) > MAX_IDENTIFIER_LENGTH or not IDENTIFIER.fullmatch(text):
        raise ValueError(f"{name} is not a valid opaque identifier")
    return text


def new_protocol_id(*, timestamp_ms: int | None = None, random_fraction: float | None = None) -> str:
    """Mirror the web client's integer ID shape without weak global RNG state."""
    timestamp_ms = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    if timestamp_ms <= 0:
        raise ValueError("timestamp_ms must be positive")
    fraction = secrets.randbelow(1_000_000) / 1_000_000 if random_fraction is None else random_fraction
    if not 0 <= fraction < 1:
        raise ValueError("random_fraction must be in [0, 1)")
    return str(int(timestamp_ms * (100 * fraction + 1)))


def parse_event_stream(lines: Iterable[str | bytes]) -> Iterator[ChatChunk]:
    """Parse the observed ``data: JSON`` stream without retaining raw events."""
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            yield ChatChunk(done=True)
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise UlearningAiError("The AI service returned an invalid event stream.") from error
        if not isinstance(payload, dict):
            raise UlearningAiError("The AI service returned an unsupported event shape.")
        yield ChatChunk(
            text=str(payload.get("data") or ""),
            reasoning=str(payload.get("reasoningContent") or ""),
            done=bool(payload.get("done", False)),
            tool_calls=tuple(item for item in (payload.get("toolCalls") or ()) if isinstance(item, dict)),
        )


class UlearningAiClient:
    """Thin transport adapter; no browser discovery and no credential storage."""

    def __init__(
        self,
        session: requests.Session,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: tuple[float, float] = (10, 120),
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def list_models(self) -> tuple[AiModel, ...]:
        """Read the models currently enabled for the signed-in organization."""
        response = None
        try:
            response = self._session.get(
                f"{self._base_url}/api/kbChat/getModelListByOrgId",
                headers={"Origin": self._base_url},
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            raise UlearningAiError("The AI model list could not be loaded.") from error
        finally:
            if response is not None:
                response.close()
        values = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise UlearningAiError("The AI service returned an invalid model list.")
        models = []
        for item in values:
            if not isinstance(item, dict) or not item.get("enable"):
                continue
            try:
                model_id = int(item["modelId"])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(item.get("modelName") or "").strip()
            if model_id <= 0 or not name:
                continue
            models.append(AiModel(
                id=model_id, name=name, vision=bool(item.get("vision")),
                online=bool(item.get("online")), thinking=bool(item.get("isThinkingModel")),
            ))
        if not models:
            raise UlearningAiError("No enabled AI model is available.")
        return tuple(models)

    def stream_chat(
        self,
        context: ChatContext,
        *,
        request_id: str,
        query: str,
        files: list[dict[str, Any]] | None = None,
        tools_content: list[dict[str, Any]] | None = None,
        remarks: str = "",
        model_id: int = 1,
        session_sign: int = 1,
        instruction_id: int = 0,
        ask_type: int = 1,
        workflow_retry: int = 0,
        thinking: bool = False,
        online: bool = False,
    ) -> Iterator[ChatChunk]:
        request_id = validate_identifier("request_id", request_id)
        if not query.strip():
            raise ValueError("query must not be empty")
        params = {
            "sessionId": context.session_id,
            "assistantId": context.assistant_id,
            "requestId": request_id,
            "courseId": context.course_id,
            "modelId": int(model_id),
            "sessionSign": int(session_sign),
            "instructionId": int(instruction_id),
            "askType": int(ask_type),
            "num": int(workflow_retry),
            "thinking": "enabled" if thinking else "disabled",
            "online": 1 if online else 0,
        }
        payload = {
            "query": query,
            "files": files or [],
            "toolsContentDTOS": tools_content,
            "remarks": remarks,
        }
        response = None
        try:
            response = self._session.post(
                f"{self._base_url}/api/kbChat/chat",
                params=params,
                json=payload,
                headers={"Accept": "text/event-stream", "Origin": self._base_url},
                stream=True,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            if response is not None:
                response.close()
            raise UlearningAiError("The AI service request failed.") from error
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if "text/event-stream" not in content_type:
            response.close()
            raise UlearningAiError("The AI service did not return an event stream.")
        try:
            yield from parse_event_stream(response.iter_lines())
        finally:
            response.close()
