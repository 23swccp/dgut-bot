"""Discover the active course-AI context from the app's local debug browser.

Secrets returned by this module are intentionally memory-only.  Their repr is
redacted and callers must not serialize, log, or persist the result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener

import requests
from websocket import create_connection

from .ulearning_ai import ChatContext, UlearningAiError, new_protocol_id


AI_HOST = "aijx.dgut.edu.cn"
WORKBENCH_MARKER = "course/workbench"


class _NoCourseAiAssistant(UlearningAiError):
    pass


@dataclass(frozen=True)
class BrowserAiAccess:
    context: ChatContext
    authorization: str = field(repr=False)
    referer: str = field(repr=False)
    user_agent: str = field(repr=False)
    cookies: tuple[dict[str, Any], ...] = field(repr=False, default=())

    def create_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "Authorization": self.authorization,
            "Referer": self.referer,
            "User-Agent": self.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        for cookie in self.cookies:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            domain = str(cookie.get("domain") or "")
            if not name or not domain or not _is_dgut_domain(domain):
                continue
            session.cookies.set(
                name,
                value,
                domain=domain,
                path=str(cookie.get("path") or "/"),
                secure=bool(cookie.get("secure", False)),
            )
        return session


class _CdpConnection:
    def __init__(self, websocket_url: str) -> None:
        parts = urlsplit(websocket_url)
        if parts.scheme != "ws" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise UlearningAiError("The debug browser returned a non-local target.")
        self._socket = create_connection(websocket_url, timeout=5, enable_multithread=True)
        self._command_id = 0

    def close(self) -> None:
        self._socket.close()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._command_id += 1
        command_id = self._command_id
        self._socket.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self._socket.recv())
            if message.get("id") != command_id:
                continue
            if "error" in message:
                raise UlearningAiError(f"The debug browser rejected {method}.")
            return message.get("result") or {}


def _is_dgut_domain(domain: str) -> bool:
    normalized = domain.lower().lstrip(".")
    return normalized == "dgut.edu.cn" or normalized.endswith(".dgut.edu.cn")


def _child_frames(frame_tree: dict[str, Any]):
    for child in frame_tree.get("childFrames") or []:
        yield child
        yield from _child_frames(child)


def _decode_ai_frame(frame_url: str) -> tuple[str, str, str, str] | None:
    parts = urlsplit(frame_url)
    if (parts.hostname or "").lower() != AI_HOST:
        return None
    path_parts = [part for part in parts.path.split("/") if part]
    if len(path_parts) != 2 or path_parts[0] != "ai":
        return None
    assistant_id = path_parts[1]
    query = parse_qs(parts.query, keep_blank_values=True)
    auth = (query.get("auth") or [""])[0]
    course_id = (query.get("courseId") or [""])[0]
    if not auth or not course_id:
        return None
    return assistant_id, course_id, auth, frame_url


def _decode_workbench_frame(frame_url: str) -> tuple[str, str, str] | None:
    parts = urlsplit(frame_url)
    if (parts.hostname or "").lower() != AI_HOST or parts.path.lower() != "/ai/workbench":
        return None
    query = parse_qs(parts.query, keep_blank_values=True)
    auth = (query.get("auth") or [""])[0]
    course_id = (query.get("ocId") or query.get("courseId") or [""])[0]
    if not auth or not course_id:
        return None
    return course_id, auth, frame_url


def _assistant_from_workbench(access: BrowserAiAccess) -> str:
    response = None
    try:
        response = access.create_session().get(
            f"https://{AI_HOST}/api/workbenches/assistantListByOcId",
            params={"ocId": access.context.course_id},
            headers={"Origin": f"https://{AI_HOST}"},
            timeout=(5, 15),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise UlearningAiError("The course AI assistant list could not be loaded.") from error
    finally:
        if response is not None:
            response.close()
    result = payload.get("result") if isinstance(payload, dict) else None
    assistants = result.get("assistantList") if isinstance(result, dict) else None
    if not isinstance(assistants, list):
        raise UlearningAiError("The course AI assistant list is invalid.")
    usable = [item for item in assistants if isinstance(item, dict) and item.get("id") and not item.get("isDelete")]

    def assistant_rank(item: dict[str, Any]) -> tuple[bool, int]:
        try:
            order = int(item.get("sort") or 0)
        except (TypeError, ValueError):
            order = 0
        return not bool(item.get("isInuse")), order

    usable.sort(key=assistant_rank)
    if not usable:
        raise _NoCourseAiAssistant("No course AI assistant is available.")
    return str(usable[0]["id"])


def discover_cached_access(
    authorization: str,
    course_ids: Iterable[str | int],
    *,
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
) -> BrowserAiAccess:
    """Build AI access directly from the app's memory-only LMS login state."""
    token = str(authorization or "").strip()
    normalized = [str(value).strip() for value in course_ids if value is not None]
    candidates = tuple(dict.fromkeys(value for value in normalized if value))
    if not token:
        raise UlearningAiError("No cached LMS login is available.")
    if not candidates:
        raise UlearningAiError("No cached course is available for the AI service.")

    for course_id in candidates:
        workbench_query = urlencode({
            "auth": token, "ocId": course_id, "theme": "ai", "courseWeb": "true",
        })
        workbench_referer = urlunsplit(("https", AI_HOST, "/ai/Workbench", workbench_query, ""))
        pending = BrowserAiAccess(
            context=ChatContext("pending", course_id, new_protocol_id()),
            authorization=token,
            referer=workbench_referer,
            user_agent=str(user_agent or "Mozilla/5.0"),
        )
        try:
            assistant_id = _assistant_from_workbench(pending)
        except _NoCourseAiAssistant:
            continue
        direct_query = urlencode({"auth": token, "courseId": course_id, "theme": "ai"})
        return BrowserAiAccess(
            context=ChatContext(assistant_id, course_id, new_protocol_id()),
            authorization=token,
            referer=urlunsplit(("https", AI_HOST, f"/ai/{assistant_id}", direct_query, "")),
            user_agent=pending.user_agent,
        )
    raise UlearningAiError("None of the cached courses has an available AI assistant.")


def _targets(port: int) -> list[dict[str, Any]]:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
            payload = response.read(1_048_577)
        targets = json.loads(payload)
    except Exception as error:
        raise UlearningAiError("The local debug browser is unavailable.") from error
    if not isinstance(targets, list):
        raise UlearningAiError("The local debug browser returned an invalid target list.")
    return targets


def discover_browser_access(port: int = 9222, *, target_id: str | None = None) -> BrowserAiAccess:
    matches = [
        target for target in _targets(port)
        if target.get("type") == "page"
        and WORKBENCH_MARKER in str(target.get("url") or "")
        and (target_id is None or target.get("id") == target_id)
    ]
    if len(matches) != 1:
        raise UlearningAiError(
            "No active AI workbench was found."
            if not matches else
            "Multiple AI workbenches are open; select a browser target first."
        )
    websocket_url = str(matches[0].get("webSocketDebuggerUrl") or "")
    connection = _CdpConnection(websocket_url)
    try:
        tree = connection.call("Page.getFrameTree").get("frameTree") or {}
        decoded = []
        workbenches = []
        for node in _child_frames(tree):
            frame = node.get("frame") or {}
            frame_url = str(frame.get("url") or "")
            access = _decode_ai_frame(frame_url)
            if access is not None:
                decoded.append(access)
            workbench = _decode_workbench_frame(frame_url)
            if workbench is not None:
                workbenches.append(workbench)
        if len(decoded) > 1 or (not decoded and len(workbenches) != 1):
            raise UlearningAiError("The active page does not contain one unambiguous course AI context.")
        if decoded:
            assistant_id, course_id, authorization, referer = decoded[0]
        else:
            course_id, authorization, referer = workbenches[0]
            assistant_id = "pending"
        connection.call("Network.enable")
        cookies = tuple(connection.call("Network.getAllCookies").get("cookies") or ())
        user_agent = str(connection.call("Browser.getVersion").get("userAgent") or "")
        if not user_agent:
            raise UlearningAiError("The debug browser did not report its user agent.")
    finally:
        connection.close()
    access = BrowserAiAccess(
        context=ChatContext(assistant_id, course_id, new_protocol_id()),
        authorization=authorization,
        referer=referer,
        user_agent=user_agent,
        cookies=cookies,
    )
    if assistant_id != "pending":
        return access
    assistant_id = _assistant_from_workbench(access)
    landing_query = parse_qs(urlsplit(referer).query, keep_blank_values=True)
    direct_params = {"auth": authorization, "courseId": course_id}
    if (landing_query.get("theme") or [""])[0]:
        direct_params["theme"] = landing_query["theme"][0]
    direct_query = urlencode(direct_params)
    return BrowserAiAccess(
        context=ChatContext(assistant_id, course_id, new_protocol_id()),
        authorization=authorization,
        referer=urlunsplit(("https", AI_HOST, f"/ai/{assistant_id}", direct_query, "")),
        user_agent=user_agent,
        cookies=cookies,
    )
