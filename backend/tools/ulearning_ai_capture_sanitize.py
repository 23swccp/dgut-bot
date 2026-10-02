"""Extract a structural, credential-free summary from a raw CDP capture log."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit


REQUEST_BODY = re.compile(r">> 请求体\s+([A-Z]+)\s+(https?://\S+)")
RESPONSE = re.compile(r"<<\s+(\d{3})\s+\S+\s+(\S+)\s+(https?://\S+)")
WEBSOCKET = re.compile(r"WS\s+(<<|>>)\s+(wss?://\S+)\s+\|\s+(.*)$")
ANY_URL = re.compile(r"https?://[^\s|\\\"']+")
REQUEST_LINE = re.compile(r">>\s+([A-Z]+)\s+https?://")
STATUS_LINE = re.compile(r"<<\s+(\d{3})\s+")
TIMESTAMP = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s?")


def safe_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def shape(value: Any, depth: int = 0) -> Any:
    if depth >= 6:
        return "<nested>"
    if isinstance(value, dict):
        return {str(key): shape(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return {"type": "array", "length": len(value), "item": shape(value[0], depth + 1) if value else None}
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return {"type": "string", "length": len(str(value))}


def identifier_shape(value: str) -> dict[str, Any]:
    text = str(value)
    if text.isdigit():
        alphabet = "digits"
    elif re.fullmatch(r"[0-9a-fA-F]+", text):
        alphabet = "hex"
    elif re.fullmatch(r"[0-9a-fA-F-]+", text):
        alphabet = "hex-hyphen"
    else:
        alphabet = "mixed"
    return {"length": len(text), "alphabet": alphabet}


def payload_from_line(line: str) -> Any:
    text = line.split("] ", 1)[-1].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"type": "unparsed", "length": len(text)}


def ai_endpoint_inventory(lines: list[str]) -> list[dict[str, Any]]:
    observations: dict[tuple[str, str, str], int] = {}
    for line in lines:
        for matched in ANY_URL.finditer(line):
            url = safe_url(matched.group())
            if (urlsplit(url).hostname or "").lower() != "aijx.dgut.edu.cn":
                continue
            request = REQUEST_LINE.search(line)
            response = STATUS_LINE.search(line)
            if not request and not response:
                continue
            kind = "request" if request else "response"
            detail = request.group(1) if request else response.group(1) if response else ""
            key = (kind, detail, url)
            observations[key] = observations.get(key, 0) + 1
    return [
        {"kind": kind, "detail": detail, "url": url, "count": count}
        for (kind, detail, url), count in sorted(observations.items())
    ]


def event_stream_shape(lines: list[str], endpoint: str) -> dict[str, Any] | None:
    title = next((i for i, line in enumerate(lines) if f"响应正文 {endpoint}" in line), None)
    if title is None or title + 1 >= len(lines):
        return None
    records = []
    for index in range(title + 1, len(lines)):
        line = lines[index]
        if index > title + 1 and TIMESTAMP.match(line):
            break
        if index == title + 1:
            line = TIMESTAMP.sub("", line)
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        try:
            records.append(shape(json.loads(line)))
        except json.JSONDecodeError:
            records.append({"type": "unparsed", "length": len(line)})
    unique = []
    seen = set()
    for record in records:
        key = json.dumps(record, sort_keys=True, ensure_ascii=True)
        if key not in seen:
            seen.add(key)
            unique.append(record)
    return {"recordCount": len(records), "recordShapes": unique}


def response_body_shapes(lines: list[str], endpoint: str) -> list[Any]:
    """Return JSON type/length structure only, never response values."""
    results = []
    for index, line in enumerate(lines[:-1]):
        if f"响应正文 {endpoint}" not in line:
            continue
        results.append(shape(payload_from_line(lines[index + 1])))
    unique = []
    seen = set()
    for result in results:
        key = json.dumps(result, sort_keys=True, ensure_ascii=True)
        if key not in seen:
            seen.add(key)
            unique.append(result)
    return unique


def request_contract(lines: list[str], endpoint: str) -> dict[str, Any] | None:
    request_indexes = [
        index for index, line in enumerate(lines)
        if f">> POST {endpoint}" in line and "请求头" not in line and "请求体" not in line
    ]
    if not request_indexes:
        return None
    index = request_indexes[-1]
    url_match = ANY_URL.search(lines[index])
    query_items = parse_qsl(urlsplit(url_match.group()).query) if url_match else []
    query_keys = sorted({key for key, _ in query_items})

    header_names = set()
    header_titles = [
        position for position in range(index + 1, min(len(lines), index + 1000))
        if endpoint in lines[position] and "请求头" in lines[position]
    ]
    for header_index in header_titles:
        for candidate in lines[header_index + 1:header_index + 100]:
            if candidate.startswith("[") and "]   [" in candidate:
                break
            matched = re.match(r"^\[\d{2}:\d{2}:\d{2}\]\s{7}([^:]+):", candidate)
            if matched:
                header_names.add(matched.group(1).strip())

    body = None
    for body_index in range(index, min(len(lines), index + 1000)):
        if f"请求体 POST {endpoint}" not in lines[body_index]:
            continue
        if body_index + 1 < len(lines):
            body = shape(payload_from_line(lines[body_index + 1]))
        break

    response = None
    for candidate in lines[index + 1:min(len(lines), index + 2000)]:
        if endpoint not in candidate:
            continue
        matched = RESPONSE.search(candidate)
        if matched:
            response = {"status": int(matched.group(1)), "mimeType": matched.group(2)}
            break
    return {
        "method": "POST",
        "url": endpoint,
        "queryParameterNames": query_keys,
        "queryParameterShapes": {key: identifier_shape(value) for key, value in query_items},
        "headerNames": sorted(header_names, key=str.lower),
        "requestShape": body,
        "response": response,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    lines = args.capture.read_text(encoding="utf-8", errors="replace").splitlines()
    instruction_endpoints = (
        "https://aijx.dgut.edu.cn/api/assistant/instruction/byAssistantId",
        "https://aijx.dgut.edu.cn/api/assistant/instructionGraph/byAssistantId",
        "https://aijx.dgut.edu.cn/api/assistant/getInuse",
        "https://aijx.dgut.edu.cn/api/assistant/getInuseNew",
    )
    print(json.dumps({
        "aiEndpoints": ai_endpoint_inventory(lines),
        "chatContract": request_contract(lines, "https://aijx.dgut.edu.cn/api/kbChat/chat"),
        "chatEventStream": event_stream_shape(lines, "https://aijx.dgut.edu.cn/api/kbChat/chat"),
        "instructionResponseShapes": {
            endpoint: response_body_shapes(lines, endpoint)
            for endpoint in instruction_endpoints
        },
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
