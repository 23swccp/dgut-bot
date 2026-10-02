"""Stable JSON protocol primitives shared by the Agent HTTP API and adapters."""

from __future__ import annotations

import re
import copy
import math
from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4


SCHEMA_VERSION = 1
TOOL_NAME_RE = re.compile(r"^[a-z0-9._]+$")


class AgentError(Exception):
    """A safe, structured failure that may cross the Agent API boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        recovery: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})
        self.recovery = dict(recovery) if recovery else None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
        if self.recovery is not None:
            value["recovery"] = self.recovery
        return value


def request_id() -> str:
    return f"req_{uuid4().hex}"


def response(
    *,
    ok: bool,
    request_id_value: str,
    tool: str | None,
    server_version: str,
    instance_id: str,
    result: Any = None,
    error: AgentError | dict[str, Any] | None = None,
) -> dict[str, Any]:
    error_value = error.as_dict() if isinstance(error, AgentError) else error
    return {
        "schemaVersion": SCHEMA_VERSION,
        "ok": bool(ok),
        "requestId": request_id_value,
        "tool": tool,
        "result": result if ok else None,
        "error": None if ok else error_value,
        "meta": {"serverVersion": server_version, "instanceId": instance_id},
    }


def error_response(
    code: str,
    message: str,
    *,
    request_id_value: str,
    tool: str | None = None,
    server_version: str = "",
    instance_id: str = "",
    retryable: bool = False,
    details: dict[str, Any] | None = None,
    recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return response(
        ok=False,
        request_id_value=request_id_value,
        tool=tool,
        server_version=server_version,
        instance_id=instance_id,
        error=AgentError(
            code,
            message,
            retryable=retryable,
            details=details,
            recovery=recovery,
        ),
    )


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate the deliberately small JSON Schema subset used by this protocol."""
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        return [f"{path} must equal {schema['const']!r}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path} is not an allowed value"]
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        if not any(not validate_schema(value, item, path) for item in alternatives):
            return [f"{path} does not match any allowed schema"]
        return []
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_matches_type(value, item) for item in expected):
            return [f"{path} has an invalid type"]
    elif isinstance(expected, str) and not _matches_type(value, expected):
        return [f"{path} must be {expected}"]

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key} is not allowed")
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, dict):
                errors.extend(validate_schema(item, child, f"{path}.{key}"))
    elif isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            errors.append(f"{path} has too few items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path} has too many items")
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for item in value:
                if item in seen:
                    errors.append(f"{path} contains duplicate items")
                    break
                seen.append(item)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_schema(item, item_schema, f"{path}[{index}]"))
    elif isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            errors.append(f"{path} is too short")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{path} is too long")
        pattern = schema.get("pattern")
        if pattern and not re.fullmatch(str(pattern), value):
            errors.append(f"{path} has an invalid format")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            return [f"{path} must be finite"]
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} is above the maximum")
    return errors


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    read_only: bool
    idempotent: bool
    task_behavior: str
    handler: Callable[[dict[str, Any]], dict[str, Any]]

    def __post_init__(self) -> None:
        if not TOOL_NAME_RE.fullmatch(self.name):
            raise ValueError(f"Invalid tool name: {self.name}")
        if self.task_behavior not in {"none", "creates_task", "waits_bounded", "cancels_task"}:
            raise ValueError(f"Invalid task behavior: {self.task_behavior}")

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "readOnly": self.read_only,
            "idempotent": self.idempotent,
            "taskBehavior": self.task_behavior,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"Duplicate tool: {definition.name}")
        self._tools[definition.name] = definition

    def capabilities(self) -> list[dict[str, Any]]:
        return [self._tools[name].public_dict() for name in sorted(self._tools)]

    def call(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        definition = self._tools.get(name)
        if definition is None:
            raise AgentError("TOOL_NOT_FOUND", "The requested tool does not exist.", details={"tool": name})
        errors = validate_schema(payload, definition.input_schema)
        if errors:
            raise AgentError("TOOL_INPUT_INVALID", "Tool input does not match its schema.", details={"errors": errors})
        normalized = copy.deepcopy(payload)
        for key, schema in definition.input_schema.get("properties", {}).items():
            if key not in normalized and "default" in schema:
                normalized[key] = copy.deepcopy(schema["default"])
        result = definition.handler(normalized)
        output_errors = validate_schema(result, definition.output_schema)
        if output_errors:
            raise AgentError("TOOL_OUTPUT_INVALID", "Tool output does not match its schema.", details={})
        return result
