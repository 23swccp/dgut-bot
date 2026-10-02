"""Agent tool registry and server-side business adapters."""

from __future__ import annotations

from typing import Any

from dgutbot.agent.agent_protocol import AgentError, SCHEMA_VERSION, ToolDefinition, ToolRegistry
from version import APP_NAME, APP_VERSION
from dgutbot.agent.agent_schemas import OUTPUTS


EMPTY_INPUT = {"type": "object", "properties": {}, "additionalProperties": False}
OBJECT_OUTPUT = {"type": "object"}


def build_registry(backend: Any, *, instance_id: str = "", services=None) -> ToolRegistry:
    """Build the registry around the one backend instance owned by the service."""
    registry = ToolRegistry()

    def system_version(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "appName": APP_NAME,
            "appVersion": APP_VERSION,
            "schemaVersion": SCHEMA_VERSION,
            "instanceId": instance_id,
        }

    def system_health(_payload: dict[str, Any]) -> dict[str, Any]:
        status = backend.course_helper_status()
        return {
            "service": "ready",
            "loggedIn": bool(getattr(backend, "token", "")),
            "coursesLoaded": bool(getattr(backend, "courses", [])),
            "courseSelected": getattr(backend, "selected_course", None) is not None,
            "taskManager": {"ready": True, "active": services.tasks.has_active() if services else False},
            "courseController": {
                "running": bool(status.get("running")),
                "connected": bool(status.get("connected")),
                "state": str(status.get("controllerState") or "IDLE"),
            },
        }

    def course_status(_payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = backend.course_helper_status()
        page = snapshot.get("page") if isinstance(snapshot.get("page"), dict) else {}
        video = snapshot.get("video") if isinstance(snapshot.get("video"), dict) else {}
        plan = snapshot.get("pagePlan") if isinstance(snapshot.get("pagePlan"), list) else []
        return {
            "running": bool(snapshot.get("running")),
            "completed": bool(snapshot.get("completed")),
            "connected": bool(snapshot.get("connected")),
            "state": str(snapshot.get("controllerState") or "IDLE"),
            "sessionId": str(snapshot.get("sessionId") or ""),
            "courseName": str(snapshot.get("courseName") or ""),
            "page": {
                "id": str(page.get("id") or ""),
                "name": str(page.get("name") or ""),
                "index": int(page.get("index") or 0),
                "total": int(page.get("total") or 0),
                "completed": bool(page.get("completed")),
            },
            "currentTask": str(snapshot.get("currentTask") or ""),
            "pagePlan": [{"kind": str(item.get("kind") or ""), "state": str(item.get("state") or ""), "count": int(item.get("count") or 0)} for item in plan if isinstance(item, dict)],
            "video": {
                "currentTime": float(video.get("currentTime") or 0),
                "duration": float(video.get("duration") or 0),
                "rate": float(video.get("rate") or 0),
            },
            "stalled": bool(snapshot.get("stalled")),
            "stallReason": str(snapshot.get("stallReason") or ""),
            "retryCount": int(snapshot.get("retryCount") or 0),
            "taskId": services.task_id if services else None,
        }

    definitions = [
        ToolDefinition(
            "system.version",
            "Return application and Agent protocol versions.",
            EMPTY_INPUT,
            OUTPUTS["system.version"],
            True,
            True,
            "none",
            system_version,
        ),
        ToolDefinition(
            "system.health",
            "Check the local service and its course controller.",
            EMPTY_INPUT,
            OUTPUTS["system.health"],
            True,
            True,
            "none",
            system_health,
        ),
        ToolDefinition(
            "course.get_status",
            "Return a sanitized course automation snapshot.",
            EMPTY_INPUT,
            OUTPUTS["course.get_status"],
            True,
            True,
            "none",
            course_status,
        ),
    ]
    for definition in definitions:
        registry.register(definition)

    if services is not None:
        key_schema = {"type": "string", "minLength": 1, "maxLength": 128}
        task_id_schema = {"type": "string", "minLength": 1, "maxLength": 128}
        timeout_schema = {"type": "integer", "minimum": 0, "maximum": 30000, "default": 0}

        def add(name, description, properties, required, handler, *, read_only=True, behavior="none"):
            if not read_only:
                properties = {**properties, "idempotencyKey": key_schema}
                required = [*required, "idempotencyKey"]
            def dispatch(payload):
                if read_only:
                    return handler(payload)
                return services.idempotency.execute(name, payload, lambda: handler(payload))
            registry.register(ToolDefinition(name, description, {
                "type": "object", "properties": properties, "required": required, "additionalProperties": False,
            }, OUTPUTS[name], read_only, True, behavior, dispatch))

        def list_courses(_payload):
            return {"courses": [{"id": str(c.id), "name": c.name, "teacherName": c.teacher_name} for c in backend.courses]}

        def load_courses(payload):
            if not backend.load_session_and_courses(wait_seconds=payload["waitSeconds"], automatic=False):
                raise AgentError("LOGIN_REQUIRED", "Log in using the application's browser before loading courses.", retryable=True)
            return list_courses({})

        def select_course(payload):
            if not backend.courses:
                raise AgentError("COURSES_NOT_LOADED", "Load courses before selecting a course.")
            course = next((c for c in backend.courses if str(c.id) == payload["courseId"]), None)
            if course is None:
                raise AgentError("COURSE_NOT_FOUND", "The course ID was not found.")
            backend.select_course_id(str(course.id))
            return {"course": {"id": str(course.id), "name": course.name, "teacherName": course.teacher_name}}

        def speed(payload):
            controller = getattr(backend, "_course_controller", None)
            if controller is None or not controller._running:
                raise AgentError("COURSE_NOT_RUNNING", "The course controller is not running.")
            backend.set_course_speed(payload["rate"])
            return {"rate": payload["rate"]}

        add("session.load_courses", "Load courses from the currently signed-in debugging browser.",
            {"waitSeconds": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1}}, [], load_courses, read_only=False)
        add("course.list", "List course IDs, names and teachers.", {}, [], list_courses)
        add("course.select", "Select an exact course ID; this does not open or navigate the course page.",
            {"courseId": {"type": "string", "minLength": 1, "maxLength": 128}}, ["courseId"], select_course, read_only=False)
        add("course.start", "Start automation in the already-open course page and return a task immediately.", {
            "rate": {"type": "number", "minimum": 1, "maximum": 16, "default": 8},
            "quizMode": {"type": "string", "enum": ["disabled", "fixed", "agent"], "default": "agent"},
            "quizRequestTimeoutMs": {"type": "integer", "minimum": 1000, "maximum": 3600000, "default": 600000},
        }, [], services.start_course, read_only=False, behavior="creates_task")
        add("course.stop", "Stop course automation without closing the service or browser.", {}, [], lambda p: services.stop_course(), read_only=False)
        add("course.set_speed", "Set the running course playback speed.",
            {"rate": {"type": "number", "minimum": 1, "maximum": 16}}, ["rate"], speed, read_only=False)
        add("task.get", "Get a task snapshot.", {"taskId": task_id_schema}, ["taskId"], lambda p: services.tasks.get(p["taskId"]))
        add("task.wait", "Wait at most 30 seconds for a task revision or terminal state.", {
            "taskId": task_id_schema, "afterRevision": {"type": "integer", "minimum": 0, "default": 0}, "timeoutMs": timeout_schema,
        }, ["taskId"], services.wait_task, behavior="waits_bounded")
        add("task.cancel", "Cancel a task using the existing course stop operation.", {"taskId": task_id_schema},
            ["taskId"], lambda p: services.cancel_task(p["taskId"]), read_only=False, behavior="cancels_task")
        add("events.wait", "Wait for sanitized events, with a resumable sequence cursor.", {
            "afterSeq": {"type": "integer", "minimum": 0, "default": 0}, "timeoutMs": timeout_schema,
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            "categories": {"type": "array", "items": {"type": "string", "enum": ["course", "quiz", "session", "recovery", "navigation", "media"]}, "uniqueItems": True, "default": []},
        }, [], services.wait_events, behavior="waits_bounded")
        request_id_schema = {"type": "string", "minLength": 1, "maxLength": 128}
        answer_properties = {
            "requestId": request_id_schema, "revision": {"type": "integer", "minimum": 1},
            "answers": {"type": "array", "maxItems": 100, "items": {
                "type": "object", "required": ["questionId", "value"], "additionalProperties": False,
                "properties": {"questionId": request_id_schema, "value": {},
                               "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
            }},
        }
        add("quiz.list_pending", "List unfinished quiz requests without question content.", {}, [], lambda p: services.quizzes.list_pending())
        add("quiz.get_request", "Read unfinished question semantics; no platform solutions or page targets.",
            {"requestId": request_id_schema}, ["requestId"], lambda p: services.quizzes.get(p["requestId"]))
        add("quiz.get_result", "Read quiz execution state and verified completion summary.",
            {"requestId": request_id_schema}, ["requestId"], lambda p: services.quizzes.result(p["requestId"]))
        add("quiz.validate_answers", "Validate all answers and freshness without clicking or typing.",
            answer_properties, ["requestId", "revision", "answers"], lambda p: services.quizzes.validate_or_submit(p))
        add("quiz.submit_answers", "Validate all answers, asynchronously apply them and submit at most once; poll quiz.get_result.",
            {**answer_properties, "mode": {"type": "string", "enum": ["validate_only", "apply_and_commit"], "default": "apply_and_commit"}},
            ["requestId", "revision", "answers"], lambda p: services.quizzes.validate_or_submit(p, submit=p["mode"] == "apply_and_commit"), read_only=False)

    registry.register(
        ToolDefinition(
            "system.capabilities",
            "List all Agent tools and their JSON Schemas.",
            EMPTY_INPUT,
            OUTPUTS["system.capabilities"],
            True,
            True,
            "none",
            lambda _payload: {"schemaVersion": SCHEMA_VERSION, "tools": registry.capabilities()},
        )
    )
    return registry
