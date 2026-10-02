"""Stateless UTF-8 JSON client for the single local DgutBot service."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener

from dgutbot.agent.agent_protocol import SCHEMA_VERSION, error_response, request_id
from dgutbot.agent.agent_runtime import RuntimeInfo, load_runtime, verify_runtime_health
from version import APP_VERSION


MAX_INPUT_BYTES = 1_048_576


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_open(request, timeout):
    return build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout)


class CliFailure(Exception):
    def __init__(self, code: str, message: str, exit_code: int, *, tool: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.tool = tool


def _write_json(payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _read_input(path: str | None) -> dict[str, Any]:
    try:
        if path:
            with Path(path).open("rb") as stream:
                raw = stream.read(MAX_INPUT_BYTES + 1)
        else:
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    except OSError as error:
        raise CliFailure("INVALID_JSON", "The input could not be read.", 2) from error
    if len(raw) > MAX_INPUT_BYTES:
        raise CliFailure("INPUT_TOO_LARGE", "The input exceeds the maximum size.", 2)
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise CliFailure("INVALID_JSON", "The input is not valid UTF-8 JSON.", 2) from error
    if not isinstance(value, dict):
        raise CliFailure("INVALID_JSON", "The input must be a JSON object.", 2)
    return value


def _parse_args(argv: list[str]) -> tuple[str, str | None, str | None]:
    if not argv or argv[0] not in {"capabilities", "call"}:
        raise CliFailure("TOOL_INPUT_INVALID", "Expected capabilities or call <tool-name>.", 2)
    action = argv[0]
    if action == "capabilities":
        if len(argv) != 1:
            raise CliFailure("TOOL_INPUT_INVALID", "capabilities does not accept arguments.", 2)
        return action, "system.capabilities", None
    if len(argv) not in {2, 4} or (len(argv) == 4 and argv[2] != "--input"):
        raise CliFailure("TOOL_INPUT_INVALID", "Expected call <tool-name> [--input <path>].", 2)
    tool = argv[1].strip()
    if not tool:
        raise CliFailure("TOOL_INPUT_INVALID", "A tool name is required.", 2)
    return action, tool, argv[3] if len(argv) == 4 else None


def _request(info: RuntimeInfo, action: str, tool: str, payload: dict[str, Any], req_id: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {info.auth_token}",
        "X-Dgut-Request-Id": req_id,
        "X-Dgut-Schema-Version": str(SCHEMA_VERSION),
    }
    if action == "capabilities":
        request = Request(f"http://127.0.0.1:{info.port}/api/agent/capabilities", headers=headers)
    else:
        body = json.dumps({"tool": tool, "input": payload}, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(
            f"http://127.0.0.1:{info.port}/api/agent/call",
            data=body,
            headers=headers,
            method="POST",
        )
    try:
        with local_open(request, timeout=35) as response:
            raw = response.read(MAX_INPUT_BYTES + 1)
    except HTTPError as error:
        raw = error.read(MAX_INPUT_BYTES + 1)
    except (OSError, URLError) as error:
        raise CliFailure("SERVICE_UNREACHABLE", "The local service cannot be reached.", 3, tool=tool) from error
    if len(raw) > MAX_INPUT_BYTES:
        raise CliFailure("PROTOCOL_RESPONSE_INVALID", "The service response is too large.", 5, tool=tool)
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise CliFailure("PROTOCOL_RESPONSE_INVALID", "The service response is invalid.", 5, tool=tool) from error
    if isinstance(value, dict) and type(value.get("schemaVersion")) is int and value["schemaVersion"] != SCHEMA_VERSION:
        raise CliFailure("SCHEMA_VERSION_UNSUPPORTED", "The protocol schema version is unsupported.", 5, tool=tool)
    if (not isinstance(value, dict) or type(value.get("schemaVersion")) is not int
            or type(value.get("ok")) is not bool or "result" not in value or "error" not in value
            or value.get("requestId") != req_id or value.get("tool") not in (tool, None)):
        raise CliFailure("PROTOCOL_RESPONSE_INVALID", "The service response is invalid.", 5, tool=tool)
    failure = value.get("error")
    if value["ok"]:
        if failure is not None or not isinstance(value["result"], dict):
            raise CliFailure("PROTOCOL_RESPONSE_INVALID", "The service response is invalid.", 5, tool=tool)
    elif (value["result"] is not None or not isinstance(failure, dict)
            or not isinstance(failure.get("code"), str) or not isinstance(failure.get("message"), str)
            or type(failure.get("retryable")) is not bool or not isinstance(failure.get("details"), dict)):
        raise CliFailure("PROTOCOL_RESPONSE_INVALID", "The service response is invalid.", 5, tool=tool)
    if failure and failure.get("code") == "AGENT_AUTH_FAILED":
        raise CliFailure("AGENT_AUTH_FAILED", "Agent authentication failed.", 3, tool=tool)
    meta = value.get("meta")
    if not isinstance(meta, dict) or meta.get("instanceId") != info.instance_id:
        raise CliFailure("SERVICE_INSTANCE_CHANGED", "The local service instance has changed.", 3, tool=tool)
    return value


def execute(argv: list[str] | None = None) -> tuple[dict[str, Any], int]:
    req_id = request_id()
    tool: str | None = None
    try:
        action, tool, input_path = _parse_args(list(sys.argv[1:] if argv is None else argv))
        payload = {} if action == "capabilities" else _read_input(input_path)
        info = load_runtime()
        verify_runtime_health(info)
        result = _request(info, action, tool, payload, req_id)
        if result.get("ok") is True:
            return result, 0
        code = str((result.get("error") or {}).get("code") or "")
        if code in {"SCHEMA_VERSION_UNSUPPORTED", "PROTOCOL_RESPONSE_INVALID", "TOOL_OUTPUT_INVALID"}:
            return result, 5
        if code in {"AGENT_AUTH_FAILED", "SERVICE_INSTANCE_CHANGED", "SERVICE_UNREACHABLE"}:
            return result, 3
        return result, 4
    except CliFailure as error:
        result = (
            error_response(
                error.code,
                error.message,
                request_id_value=req_id,
                tool=error.tool or tool,
                server_version=APP_VERSION,
                instance_id="",
                retryable=error.exit_code == 3,
            )
        )
        return result, error.exit_code
    except Exception as error:  # noqa: BLE001 - stdout must remain a single safe JSON response
        code = getattr(error, "code", "INTERNAL_ERROR")
        message = getattr(error, "message", "The CLI could not complete the request.")
        exit_code = 3 if code in {
            "SERVICE_NOT_RUNNING",
            "RUNTIME_FILE_INVALID",
            "SERVICE_INSTANCE_CHANGED",
            "SERVICE_UNREACHABLE",
        } else 70
        result = (
            error_response(
                str(code),
                str(message),
                request_id_value=req_id,
                tool=tool,
                server_version=APP_VERSION,
                instance_id="",
                retryable=exit_code == 3,
            )
        )
        return result, exit_code


def run(argv: list[str] | None = None) -> int:
    result, exit_code = execute(argv)
    try:
        _write_json(result)
    except (OSError, ValueError):
        # Never attempt a second JSON document after a partial stdout write.
        return 70
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
