"""浏览器前端与 Python 后端之间的统一命令层。"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Any
from uuid import uuid4

from dgutbot.agent.agent_protocol import AgentError
from dgutbot.agent.agent_tools import build_registry
from dgutbot.agent.agent_service import AgentService
from dgutbot.app.app_paths import data_root
from dgutbot.domain.yxy_backend import SignBackend
from dgutbot.app.velopack_updater import UpdateManager
from dgutbot.experimental.ulearning_ai import UlearningAiError
from dgutbot.experimental.ulearning_ai_bridge import UlearningAiBridge
from dgutbot.experimental.ulearning_ai_browser import discover_browser_access, discover_cached_access
from dgutbot.course.ai_quiz import UlearningAiAnswerProvider
from version import APP_NAME, APP_VERSION, GITHUB_REPO


# 配置、登录缓存、浏览器资料、日志与更新状态统一写入 LocalAppData。
ROOT = data_root()
ROOT.mkdir(parents=True, exist_ok=True)
class EventBuffer:
    """线程安全的有限事件流；读取使用游标，不会消费事件。"""

    def __init__(self, maxlen: int = 1500) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._listeners = []
        self._seq = 0
        self._default_session_id = f"app-{datetime.now().astimezone():%Y%m%d}-{uuid4().hex[:8]}"

    def emit_event(
        self,
        code: str,
        level: str,
        category: str,
        message: str,
        *,
        session_id: str = "",
        page: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "sessionId": session_id or self._default_session_id,
                "code": str(code),
                "level": str(level),
                "category": str(category),
                "message": str(message),
                "page": dict(page or {}),
                "data": dict(data or {}),
            }
            self._events.append(event)
            self._condition.notify_all()
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(dict(event))
            except Exception:
                pass
        return dict(event)

    def subscribe(self, listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def wait_events(self, after_seq: int, timeout_ms: int, limit: int, categories: list[str]) -> dict[str, Any]:
        def matching():
            return [event for event in self._events if event["seq"] > after_seq
                    and event["code"] not in {"DEBUG_LOG", "LEGACY_LOG"}
                    and (not categories or event["category"] in categories)]
        with self._condition:
            found = self._condition.wait_for(lambda: bool(matching()), timeout=timeout_ms / 1000)
            events = matching()[:limit]
            dropped = max(0, self._events[0]["seq"] - 1) if self._events else 0
            latest = events[-1]["seq"] if len(events) == limit else self._seq
            # Explicit allowlist: raw diagnostic messages/data never cross the Agent boundary.
            safe = []
            for event in events:
                page = event["page"]
                allowed_data = {"requestId", "questionCount", "count", "playbackRate", "completed", "skipped", "failed", "elapsedSeconds"}
                safe.append({
                    "seq": event["seq"], "time": event["time"], "sessionId": event["sessionId"],
                    "code": event["code"], "level": event["level"], "category": event["category"],
                    "message": event["code"],
                    "page": {k: page[k] for k in ("id", "name", "index", "total") if k in page},
                    "data": {k: v for k, v in event["data"].items() if k in allowed_data and isinstance(v, (str, int, float, bool))},
                })
            return {"events": safe, "nextSeq": latest, "timedOut": not found, "droppedBeforeSeq": dropped}

    def get_events(self, after_seq: int = 0) -> dict[str, Any]:
        with self._lock:
            events = [dict(event) for event in self._events if int(event["seq"]) > after_seq]
            return {"events": events, "latestSeq": self._seq}


EVENT_BUFFER = EventBuffer()


def emit(message: str, kind: str) -> None:
    """旧字符串日志兼容入口；课程内部日志默认归入详细日志。"""
    is_course = kind.startswith("course:") or message.startswith(("[刷课]", "[yxy]"))
    raw_level = kind.split(":", 1)[-1]
    level = {"muted": "info", "warn": "warning"}.get(raw_level, raw_level)
    EVENT_BUFFER.emit_event(
        "DEBUG_LOG" if is_course else "LEGACY_LOG",
        level if level in {"info", "success", "warning", "error"} else "info",
        "debug" if is_course else "general",
        message,
        data={"legacyKind": kind},
    )


def emit_event(code: str, level: str, category: str, message: str, **kwargs: Any) -> dict[str, Any]:
    return EVENT_BUFFER.emit_event(code, level, category, message, **kwargs)


backend = SignBackend(emit=emit, emit_event=emit_event, root=ROOT)


def _cached_course_ids() -> list[int]:
    selected = [backend.selected_course.id] if backend.selected_course is not None else []
    return list(dict.fromkeys(selected + [course.id for course in backend.courses]))


def _resolve_ai_access(debug_port: int):
    """Prefer the app login cache; keep the open-browser path as a fallback."""
    cached_error: UlearningAiError | None = None
    if not backend.courses:
        backend.load_saved_courses()
    if backend.token and backend.courses:
        try:
            return discover_cached_access(
                backend.token,
                _cached_course_ids(),
                user_agent=backend.headers.get("User-Agent", ""),
            )
        except UlearningAiError as error:
            cached_error = error
            # A stale token may be recoverable through the existing, bounded
            # optional account-login flow; credentials never leave SignBackend.
            if backend.load_saved_courses():
                try:
                    return discover_cached_access(
                        backend.token,
                        _cached_course_ids(),
                        user_agent=backend.headers.get("User-Agent", ""),
                    )
                except UlearningAiError as retry_error:
                    cached_error = retry_error
    try:
        return discover_browser_access(int(debug_port))
    except UlearningAiError as browser_error:
        raise UlearningAiError(
            "未找到可用的登录缓存或课程 AI 上下文；请先让程序读取课程。"
        ) from (cached_error or browser_error)


AI_BRIDGE = UlearningAiBridge(
    debug_port=lambda: int(backend.config.debug_port),
    access_factory=_resolve_ai_access,
)

# 应用内自动更新由 Velopack 执行；这里只适配现有前端状态接口。
update_manager = UpdateManager(
    ROOT,
    version=APP_VERSION,
    repository=GITHUB_REPO,
    emit_event=emit_event,
    debug_port=lambda: backend.config.debug_port,
)
update_manager.restore()


AGENT_INSTANCE_ID = ""
AGENT_SERVICE = AgentService(backend, EVENT_BUFFER)
AGENT_REGISTRY = build_registry(backend, instance_id=AGENT_INSTANCE_ID, services=AGENT_SERVICE)


def configure_agent_registry(instance_id: str) -> None:
    """Bind capability metadata to the service instance published by the launcher."""
    global AGENT_INSTANCE_ID, AGENT_REGISTRY
    AGENT_INSTANCE_ID = str(instance_id)
    AGENT_REGISTRY = build_registry(backend, instance_id=AGENT_INSTANCE_ID, services=AGENT_SERVICE)


def agent_capabilities() -> dict[str, Any]:
    return {"schemaVersion": 1, "tools": AGENT_REGISTRY.capabilities()}


def handle_agent(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch an authenticated Agent request without exposing backend internals."""
    if not isinstance(payload, dict):
        raise AgentError("TOOL_INPUT_INVALID", "Tool input must be a JSON object.")
    return AGENT_REGISTRY.call(tool, payload)


def courses() -> list[dict[str, Any]]:
    return [{"id": course.id, "name": course.name, "teacherName": course.teacher_name} for course in backend.courses]


def handle(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    if command == "get_events":
        try:
            after_seq = max(0, int(payload.get("afterSeq", 0)))
        except (TypeError, ValueError):
            after_seq = 0
        return {"ok": True, **EVENT_BUFFER.get_events(after_seq)}
    if command == "load_saved_courses":
        return {"ok": backend.load_saved_courses(), "courses": courses()}
    if command == "ai_chat":
        try:
            model_id = int(payload.get("modelId", backend.config.course_ai_model_id))
            reply = AI_BRIDGE.complete(payload.get("messages"), model_id=model_id)
            return {
                "ok": True,
                "answer": reply.text,
                "reasoning": reply.reasoning,
                "upstreamToolCallCount": len(reply.upstream_tool_calls),
            }
        except (ValueError, UlearningAiError) as error:
            return {"ok": False, "error": str(error)}
    if command == "ai_models":
        try:
            models = AI_BRIDGE.models()
            selected = int(backend.config.course_ai_model_id)
            if selected not in {model.id for model in models}:
                selected = models[0].id
                backend.update_settings(course_ai_model_id=selected)
            return {"ok": True, "models": [
                {"id": model.id, "name": model.name, "vision": model.vision,
                 "online": model.online, "thinking": model.thinking}
                for model in models
            ], "selectedModelId": selected}
        except (OSError, ValueError, UlearningAiError) as error:
            return {"ok": False, "error": str(error)}
    if command == "start_browser":
        url = str(payload.get("url", ""))

        def launch() -> None:
            try:
                backend.start_browser(url)
            except Exception as error:
                emit(f"浏览器启动异常：{error}", "warn")

        threading.Thread(target=launch, name="browser-launch", daemon=True).start()
        return {"ok": True}
    if command == "load_session_and_courses":
        try:
            wait_seconds = max(1, min(5, int(payload.get("waitSeconds", 1))))
        except (TypeError, ValueError):
            wait_seconds = 1
        automatic = bool(payload.get("automatic", False))
        return {
            "ok": backend.load_session_and_courses(wait_seconds=wait_seconds, automatic=automatic),
            "courses": courses(),
        }
    if command == "select_course":
        course = backend.select_course(str(payload.get("query", "")))
        value = {"id": course.id, "name": course.name, "teacherName": course.teacher_name} if course else None
        return {"ok": course is not None, "course": value}
    if command == "clear_selected_course":
        backend.clear_selected_course()
        return {"ok": True}
    if command == "start_monitor":
        if backend.selected_course is None:
            return {"ok": False, "error": "尚未选择课程"}
        started = backend.start_monitor()
        return {"ok": started, "error": "签到监测已在运行" if not started else ""}
    if command == "stop_monitor":
        backend.stop_monitor()
        return {"ok": True}
    if command == "get_sign_monitor_status":
        return {"ok": True, "signStatus": backend.sign_monitor_status()}
    if command == "start_course_helper":
        if backend.config.course_quiz_auto_answer:
            try:
                AI_BRIDGE.probe(backend.config.course_ai_model_id)
            except UlearningAiError:
                return {"ok": False, "error": "未找到可用的登录缓存、课程 AI 或所选模型；请先在程序中读取课程。"}
            provider = UlearningAiAnswerProvider(
                AI_BRIDGE, backend.course_controller.emit, backend.config.course_ai_model_id,
            )
            started = backend.start_course_helper(quiz_mode="ai", ai_provider=provider)
            if started:
                # CourseConfig 已为本次任务生成快照；解除全局开关，下一次启动必须重新明确开启。
                backend.config.course_quiz_auto_answer = False
                backend.save_config()
            return {"ok": started, "quizAutoAnswerArmed": backend.config.course_quiz_auto_answer}
        return {"ok": backend.start_course_helper()}
    if command == "stop_course_helper":
        backend.stop_course_helper()
        return {"ok": True}
    if command == "set_course_speed":
        try:
            backend.set_course_speed(float(payload.get("rate", backend.config.course_playback_rate)))
            return {"ok": True}
        except (TypeError, ValueError) as error:
            return {"ok": False, "error": str(error)}
    if command == "get_course_helper_status":
        return {"ok": True, "status": backend.course_helper_status()}
    if command == "get_settings":
        return {"ok": True, "config": backend.config.to_mapping()}
    if command == "detect_browsers":
        return {"ok": True, "browsers": backend.detect_browsers()}
    if command == "get_account_login_status":
        return {"ok": True, "account": backend.account_login_status()}
    if command == "update_account_login":
        ok = backend.update_account_login(
            str(payload.get("username", "")),
            str(payload.get("password", "")),
            bool(payload.get("enabled")),
        )
        return {"ok": ok, "account": backend.account_login_status()}
    if command == "update_settings":
        try:
            backend.update_settings(**payload)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "config": backend.config.to_mapping()}
    if command == "open_log":
        try:
            path = backend.open_log_file(str(payload.get("path", "")))
            return {"ok": True, "path": str(path)}
        except OSError as error:
            return {"ok": False, "error": f"打开日志失败：{error}"}
    if command == "get_app_info":
        info = handle_agent("system.version", {})
        return {"ok": True, "info": {"appName": info["appName"], "version": info["appVersion"], "repo": GITHUB_REPO}}
    if command == "get_update_status":
        return {"ok": True, "update": update_manager.snapshot()}
    if command == "check_update":
        def check() -> None:
            update_manager.check(manual=True)

        threading.Thread(target=check, name="update-check", daemon=True).start()
        return {"ok": True}
    if command == "download_update":
        return update_manager.start_download()
    if command == "install_update":
        return update_manager.install()
    if command == "mark_update_read":
        update_manager.mark_read()
        return {"ok": True}
    if command == "ack_update_failure":
        update_manager.ack_failure()
        return {"ok": True}
    return {"ok": False, "error": f"未知命令：{command}"}
