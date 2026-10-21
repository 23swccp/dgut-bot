"""浏览器前端与 Python 后端之间的统一命令层。"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from typing import Any
from uuid import uuid4

from websocket import WebSocketException

from dgutbot.agent.protocol import AgentError
from dgutbot.agent.tools import build_registry
from dgutbot.agent.service import AgentService
from dgutbot.app.paths import data_root
from dgutbot.app.campus_startup import configure_campus_login_startup
from dgutbot.app.homework_input import HomeworkInput
from dgutbot.domain.yxy_backend import SignBackend
from dgutbot.domain.independent_login import IndependentLoginError
from dgutbot.logs import RunLogSink, append_block, append_block_with_header, redact, session_header, stamp
from dgutbot.app.updater import UpdateManager
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

    def __init__(self, maxlen: int = 4000) -> None:
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
        module: str = "",
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
                "module": str(module),
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

    def log_page(
        self, *, after_seq: int = 0, before_seq: int | None = None,
        level: str = "", module: str = "", query: str = "", limit: int = 400,
    ) -> dict[str, Any]:
        """日志页读取：按级别/模块/关键词过滤，after_seq 用于增量跟随。"""
        limit = max(1, min(2000, int(limit)))
        keyword = query.strip().casefold()
        with self._lock:
            snapshot = [dict(event) for event in self._events]
            latest = self._seq
        matching: list[dict[str, Any]] = []
        for event in snapshot:
            seq = int(event["seq"])
            if after_seq and seq <= after_seq:
                continue
            if before_seq is not None and seq >= before_seq:
                continue
            if level and event["level"] != level:
                continue
            if module and module != "all" and log_module(event) != module:
                continue
            if keyword and keyword not in f"{event['message']} {event['code']}".casefold():
                continue
            matching.append({
                "seq": seq, "time": event["time"], "level": event["level"],
                "module": log_module(event), "category": event["category"],
                "code": event["code"], "message": event["message"], "data": event["data"],
            })
        has_more = len(matching) > limit
        # 增量跟随取最早的一批，回看历史取最新的一批。
        window = matching[:limit] if after_seq else matching[-limit:]
        return {"events": window, "latestSeq": latest, "hasMore": has_more,
                "bufferSize": len(snapshot)}


EVENT_BUFFER = EventBuffer()


LOG_MODULE_LABELS: dict[str, str] = {
    "sign": "签到", "course": "刷课", "homework": "作业", "agent": "Agent", "update": "更新", "app": "程序",
}
_COURSE_CATEGORIES = {"course", "content", "quiz", "debug"}


def log_module(event: dict[str, Any]) -> str:
    """事件的模块归属。

    优先用发出方写的 ``module``（``emit(text, kind, module)``）；没有才靠
    code/category 反推，保证旧调用和第三方事件仍能归类。
    """
    declared = str(event.get("module") or "")
    if declared in LOG_MODULE_LABELS:
        return declared
    code = str(event.get("code") or "")
    category = str(event.get("category") or "")
    if code == "DEBUG_LOG" or category in _COURSE_CATEGORIES:
        return "course"
    if code.startswith("HOMEWORK_") or category == "homework":
        return "homework"
    if code.startswith("UPDATE_") or category == "update":
        return "update"
    if code.startswith("AGENT_") or category == "agent":
        return "agent"
    if category == "sign":
        return "sign"
    # 未声明归属的通用事件按“程序”处理：签到事件必须由发出方显式声明，
    # 否则漏标的模块会被静默算成签到，正是这里要避免的误导。
    return "app"


def emit(message: str, kind: str, module: str = "") -> None:
    """旧字符串日志兼容入口；课程内部日志默认归入详细日志。

    ``module`` 是发出方声明的归属（``sign``/``app``…），留空则按 code/category 反推。
    """
    is_course = kind.startswith("course:") or message.startswith(("[刷课]", "[yxy]"))
    raw_level = kind.split(":", 1)[-1]
    level = {"muted": "info", "warn": "warning"}.get(raw_level, raw_level)
    EVENT_BUFFER.emit_event(
        "DEBUG_LOG" if is_course else "LEGACY_LOG",
        level if level in {"info", "success", "warning", "error"} else "info",
        "debug" if is_course else "general",
        message,
        data={"legacyKind": kind},
        module=module,
    )


def emit_event(code: str, level: str, category: str, message: str, **kwargs: Any) -> dict[str, Any]:
    return EVENT_BUFFER.emit_event(code, level, category, message, **kwargs)


backend = SignBackend(emit=emit, emit_event=emit_event, root=ROOT)
# 事件流全部落到 data.md，真实环境出问题可按时间回看整段运行过程。
RUN_LOG = RunLogSink(lambda: (bool(backend.config.save_log), backend._resolve_log_path()))
EVENT_BUFFER.subscribe(RUN_LOG.on_event)
HOMEWORK_INPUT = HomeworkInput(lambda: int(backend.config.debug_port))


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


# 每秒轮询的只读命令不写进 data.md，否则日志会被心跳淹没；它们抛错时仍然记录。
QUIET_COMMANDS = frozenset({
    "get_login_status", "load_session_and_courses", "get_sign_monitor_status",
    "get_course_scan_status", "get_course_helper_status", "get_update_status",
    "get_events", "get_homework_scan_status", "get_log_events",
})


def _record_command(
    command: str, payload: dict[str, Any], result: dict[str, Any] | None,
    error: BaseException | None, elapsed_ms: float,
) -> None:
    """把一个命令调用写进事件流：日志页按模块看，data.md 按时间看。

    只记 payload 的键名，绝不记取值；轮询类命令干脆不记。
    """
    try:
        if command in QUIET_COMMANDS and error is None:
            return
        outcome = "异常" if error is not None else ("成功" if (result or {}).get("ok") else "失败")
        detail = redact(error) if error is not None else redact((result or {}).get("error") or "")
        keys = ",".join(sorted(str(key) for key in payload)) if isinstance(payload, dict) else ""
        parts = [f"{redact(command)} · {outcome} · {elapsed_ms:.0f}ms"]
        if keys:
            parts.append(f"payload: {keys}")
        if detail:
            parts.append(detail)
        level = "error" if error is not None else ("info" if outcome == "成功" else "warning")
        EVENT_BUFFER.emit_event("COMMAND", level, "command", " · ".join(parts))
    except Exception:  # 日志失败绝不能影响命令本身
        pass


def _export_log_report(payload: dict[str, Any]) -> dict[str, Any]:
    """把当前筛选结果导出成一份 Markdown，路径随命令返回。"""
    module = str(payload.get("module") or "all")
    level = str(payload.get("level") or "")
    query = str(payload.get("query") or "")
    page = EVENT_BUFFER.log_page(module=module, level=level, query=query, limit=2000)
    events = page["events"]
    now = datetime.now()
    label = LOG_MODULE_LABELS.get(module, "全部") if module != "all" else "全部模块"
    # 单独放一层目录，避免把根数据目录刷满导出文件。
    path = backend.root / "日志导出" / f"日志-{now:%Y%m%d-%H%M%S}.md"
    lines = [f"\n## 日志导出 · {now:%Y-%m-%d %H:%M:%S}\n",
             f"- 范围 {label}"
             + (f" · 级别 {level}" if level else "")
             + (f" · 关键词 {query}" if query else "")
             + f" · {len(events)} 条"
             + ("（缓冲区已满，仅最近若干条）" if page["hasMore"] else "")
             + "\n\n"]
    lines.extend(RunLogSink.line(event) for event in events)
    lines.append("\n")
    try:
        append_block(path, lines)
    except OSError as error:
        return {"ok": False, "error": f"写入导出文件失败：{error}"}
    return {"path": str(path), "count": len(events), "module": module, "level": level, "query": query}


def handle(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = _dispatch(command, payload)
    except Exception as error:
        _record_command(command, payload, None, error, (time.perf_counter() - started) * 1000)
        raise
    _record_command(command, payload, result, None, (time.perf_counter() - started) * 1000)
    return result


def _dispatch(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    if command == "get_log_events":
        page = EVENT_BUFFER.log_page(
            after_seq=int(payload.get("afterSeq") or 0),
            before_seq=(int(payload["beforeSeq"]) if payload.get("beforeSeq") not in (None, "") else None),
            level=str(payload.get("level") or ""),
            module=str(payload.get("module") or "all"),
            query=str(payload.get("query") or ""),
            limit=int(payload.get("limit") or 400),
        )
        return {"ok": True, "logEvents": page["events"], "latestSeq": page["latestSeq"],
                "hasMore": page["hasMore"], "bufferSize": page["bufferSize"],
                "modules": LOG_MODULE_LABELS}
    if command == "export_log_report":
        return {"ok": True, "report": _export_log_report(payload)}
    if command == "get_homework_input_status":
        return {"ok": True, "homework": HOMEWORK_INPUT.status()}
    if command == "send_homework_text":
        try:
            return {"ok": True, "homework": HOMEWORK_INPUT.send(payload.get("text"))}
        except (OSError, RuntimeError, ValueError, WebSocketException) as error:
            return {"ok": False, "error": str(error)}
    if command == "start_homework_scan":
        return {"ok": True, "homeworkScan": backend.start_homework_scan(force=bool(payload.get("force")))}
    if command == "cancel_homework_scan":
        return {"ok": True, "homeworkScan": backend.cancel_homework_scan()}
    if command == "get_homework_scan_status":
        return {"ok": True, "homeworkScan": backend.homework_scan_status()}
    if command == "set_homework_archive":
        try:
            return {"ok": True, "homeworkScan": backend.set_homework_archive(
                str(payload.get("kind") or ""), str(payload.get("id") or ""), bool(payload.get("archived")),
            )}
        except (OSError, RuntimeError, ValueError) as error:
            return {"ok": False, "error": str(error)}
    if command == "get_homework_review":
        try:
            return {"ok": True, "homeworkReview": backend.homework_review(str(payload.get("itemId") or ""))}
        except (OSError, RuntimeError, ValueError) as error:
            return {"ok": False, "error": str(error)}
    if command == "submit_homework_reviews":
        try:
            return {"ok": True, "reviewSubmit": backend.submit_homework_reviews(
                str(payload.get("itemId") or ""), payload.get("drafts"),
            )}
        except (OSError, RuntimeError, ValueError) as error:
            return {"ok": False, "error": str(error)}
    if command == "get_pending_homework_review_candidates":
        try:
            return {"ok": True, "bulkReviewCandidates": backend.pending_homework_review_candidates()}
        except (OSError, RuntimeError, ValueError) as error:
            return {"ok": False, "error": str(error)}
    if command == "submit_pending_homework_reviews":
        try:
            return {"ok": True, "bulkReviewSubmit": backend.submit_pending_homework_reviews(payload.get("itemIds"))}
        except (OSError, RuntimeError, ValueError) as error:
            return {"ok": False, "error": str(error)}
    if command == "get_events":
        try:
            after_seq = max(0, int(payload.get("afterSeq", 0)))
        except (TypeError, ValueError):
            after_seq = 0
        return {"ok": True, **EVENT_BUFFER.get_events(after_seq)}
    if command == "load_saved_courses":
        ok = backend.load_saved_courses()
        return {"ok": ok, "courses": courses(), "login": backend.login_status(),
                "hasCachedCredentials": bool(backend.token)}
    if command == "get_login_status":
        return {"ok": True, "login": backend.login_status()}
    if command == "get_independent_login_status":
        return {"ok": True, "independentLogin": backend.independent_login.status()}
    if command == "list_independent_login_sources":
        configured = int(backend.config.debug_port)
        return {"ok": True, "configuredPort": configured,
                "sources": backend.independent_login.discover([configured])}
    if command == "clear_independent_login":
        return {"ok": True, "independentLogin": backend.independent_login.clear()}
    if command == "clear_independent_account":
        backend.independent_login.clear_saved_account()
        return {"ok": True, "independentLogin": backend.independent_login.status()}
    if command in ("login_independent_account", "import_independent_login"):
        try:
            if command == "login_independent_account":
                result = backend.independent_login.login(
                    payload.get("username"), payload.get("password"), bool(payload.get("remember", False)))
            else:
                result = backend.independent_login.import_browser(payload.get("port"))
            return {"ok": True, "independentLogin": result}
        except IndependentLoginError as error:
            return {"ok": False, "error": str(error), "independentLogin": backend.independent_login.status()}
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
            "login": backend.login_status(),
        }
    if command == "select_course":
        course = backend.select_course(str(payload.get("query", "")))
        value = {"id": course.id, "name": course.name, "teacherName": course.teacher_name} if course else None
        return {"ok": course is not None, "course": value}
    if command == "clear_selected_course":
        backend.clear_selected_course()
        return {"ok": True}
    if command in {"get_sign_classrooms", "select_sign_classroom"}:
        try:
            status = (backend.sign_monitor.prepare_classrooms() if command == "get_sign_classrooms"
                      else backend.sign_monitor.select_classroom(payload.get("classroomId")))
            return {"ok": True, "signStatus": status}
        except (OSError, RuntimeError, ValueError) as error:
            return {"ok": False, "error": backend._redact(error)}
    if command == "start_monitor":
        if backend.selected_course is None and not backend.sign_monitor.membership.pending:
            return {"ok": False, "error": "尚未选择课程"}
        started = backend.start_monitor()
        return {"ok": started, "error": "签到监测已在运行" if not started else ""}
    if command == "stop_monitor":
        backend.stop_monitor()
        return {"ok": True}
    if command == "get_sign_monitor_status":
        return {"ok": True, "signStatus": backend.sign_monitor_status()}
    if command == "recover_course_membership":
        backend.sign_monitor.membership.stop()
        resumed = backend.resume_course_recovery()
        return {"ok": resumed, "signStatus": backend.sign_monitor_status(),
                "error": "" if resumed else "请先登录原学生账号；恢复记录将保留"}
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
    if command == "start_course_scan":
        return {"ok": True, "scan": backend.start_course_scan(force=bool(payload.get("force")))}
    if command == "cancel_course_scan":
        return {"ok": True, "scan": backend.cancel_course_scan()}
    if command == "get_course_scan_status":
        return {"ok": True, "scan": backend.course_scan_status()}
    if command == "open_scanned_course":
        item_id = str(payload.get("itemId") or "")
        if not item_id:
            return {"ok": False, "error": "缺少课件标识"}
        if backend.course_helper_status().get("running"):
            return {"ok": False, "error": "已有刷课任务正在运行，请先停止当前任务"}
        if backend.course_scan_status().get("openPhase") in {"opening", "waiting_page", "validating", "connected", "starting"}:
            return {"ok": False, "error": "正在打开所选课件，请勿重复点击"}

        def open_and_start() -> None:
            backend.auto_open_course(
                item_id,
                lambda: bool(handle("start_course_helper", {}).get("ok")),
            )

        backend.course_scan.set_open_state("opening", selected_id=item_id)
        threading.Thread(target=open_and_start, name="auto-open-course", daemon=True).start()
        return {"ok": True, "scan": backend.course_scan_status()}
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
        previous_campus_startup = backend.config.campus_login_on_startup
        try:
            backend.update_settings(**payload)
            configure_campus_login_startup(backend.config.campus_login_on_startup)
        except (OSError, ValueError) as error:
            if backend.config.campus_login_on_startup != previous_campus_startup:
                backend.update_settings(campus_login_on_startup=previous_campus_startup)
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
