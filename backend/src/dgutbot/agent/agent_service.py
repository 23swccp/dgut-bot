"""Compose task ownership around the existing backend; never owns a CDP connection."""

from __future__ import annotations

import threading
from typing import Any

from dgutbot.agent.agent_leases import LeaseManager
from dgutbot.agent.agent_protocol import AgentError
from dgutbot.agent.agent_tasks import IdempotencyStore, TaskManager, TERMINAL
from dgutbot.course.quiz_requests import AgentAnswerProvider, QuizRequestManager


class AgentService:
    def __init__(self, backend: Any, events: Any) -> None:
        self.backend, self.events = backend, events
        self.tasks = TaskManager()
        self.idempotency = IdempotencyStore()
        self.leases = LeaseManager()
        self.quizzes = QuizRequestManager(self.tasks, events, self.leases)
        self._lock = threading.Lock()
        self.task_id: str | None = None
        self._session_id = ""
        self._cancel_requested = threading.Event()
        self._starting = False
        events.subscribe(self.on_event)

    def on_event(self, event: dict[str, Any]) -> None:
        task_id = self.task_id
        if not task_id:
            return
        task = self.tasks.get(task_id)
        if task["state"] in TERMINAL:
            return
        code = event["code"]
        if code == "SESSION_STARTED":
            self._session_id = event["sessionId"]
        if not self._session_id or event["sessionId"] != self._session_id:
            return
        changes: dict[str, Any] = {}
        page = event.get("page") or {}
        if page.get("index"):
            changes["progress"] = {"current": int(page["index"]), "total": int(page.get("total") or 0), "unit": "page"}
        if code == "SESSION_STARTED":
            changes["state"] = "running"
        elif code == "COURSE_COMPLETED":
            changes.update(state="completed", waiting=None, result={"completed": True})
        elif code == "RECOVERY_FAILED":
            changes.update(state="failed", waiting=None, error=AgentError("COURSE_ATTACH_FAILED", "Course recovery failed; manual review is required.").as_dict())
        elif code == "SESSION_STOPPED":
            self.quizzes.cancel_task(task_id)
            if not self._starting:
                changes.update(state="cancelled", waiting=None)
        if changes:
            self.tasks.update(task_id, **changes)

    def active(self) -> bool:
        controller = getattr(self.backend, "_course_controller", None)
        monitor = getattr(self.backend, "monitor_thread", None)
        self.leases.set("course_task", "controller", bool(controller and controller._running))
        self.leases.set("course_task", "agent", self.tasks.has_active())
        self.leases.set("monitor_task", "monitor", bool(monitor and monitor.is_alive()))
        return self.leases.active()

    def start_course(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            controller = getattr(self.backend, "_course_controller", None)
            if self._starting or self.tasks.has_active() or (controller and controller._running):
                raise AgentError("COURSE_ALREADY_RUNNING", "A course task is already running.")
            task = self.tasks.create()
            if not self.backend.reserve_course_start(task["taskId"]):
                self.tasks.update(task["taskId"], state="failed")
                raise AgentError("COURSE_ALREADY_RUNNING", "A course task is already starting or running.")
            self.task_id = task["taskId"]
            self._session_id = ""
            self._cancel_requested.clear()
            self._starting = True
        threading.Thread(target=self._start_course, args=(task["taskId"], payload), name="agent-course-start", daemon=True).start()
        return task

    def _start_course(self, task_id: str, payload: dict[str, Any]) -> None:
        try:
            with self.backend._course_operation_lock:
                if self._cancel_requested.is_set():
                    self.tasks.update(task_id, state="cancelled")
                    return
                provider = AgentAnswerProvider(self.quizzes, task_id, payload["quizRequestTimeoutMs"]) if payload["quizMode"] == "agent" else None
                ok = self.backend.start_course_helper(rate=payload["rate"], quiz_mode=payload["quizMode"], agent_provider=provider, task_id=task_id)
                if ok and not self._cancel_requested.is_set():
                    if self.tasks.get(task_id)["state"] == "queued":
                        self.tasks.update(task_id, state="running")
                elif self._cancel_requested.is_set():
                    self.backend.stop_course_helper()
                    self.tasks.update(task_id, state="cancelled", waiting=None)
                else:
                    controller = getattr(self.backend, "_course_controller", None)
                    code = "COURSE_PAGE_NOT_FOUND" if controller is not None and not controller.ws_url else "COURSE_ATTACH_FAILED"
                    self.tasks.update(task_id, state="failed", error=AgentError(code, "The course page could not be attached.", retryable=True).as_dict())
        except Exception:
            self.tasks.update(task_id, state="failed", error=AgentError("COURSE_ATTACH_FAILED", "The course task could not start.", retryable=True).as_dict())
        finally:
            self._starting = False
            self.backend.release_course_start(task_id)

    def stop_course(self) -> dict[str, Any]:
        self._cancel_requested.set()
        if self.task_id:
            self.quizzes.cancel_task(self.task_id)
        done = threading.Event()
        outcome = []
        def stop():
            try:
                self.backend.stop_course_helper()
                outcome.append(True)
            except Exception:
                outcome.append(False)
            finally:
                done.set()
        threading.Thread(target=stop, name="agent-course-stop", daemon=True).start()
        stopped = done.wait(20) and outcome == [True]
        controller = getattr(self.backend, "_course_controller", None)
        if not stopped or (controller and controller._running):
            if self.task_id:
                self.tasks.update(self.task_id, state="failed", error=AgentError("TASK_CANCEL_FAILED", "The course task did not stop.").as_dict())
            raise AgentError("TASK_CANCEL_FAILED", "The course task did not stop.")
        if self.task_id:
            self.tasks.update(self.task_id, state="cancelled", waiting=None)
        return {"stopped": True, "taskId": self.task_id}

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task["state"] in TERMINAL:
            raise AgentError("TASK_ALREADY_TERMINAL", "The task is already terminal.")
        if task_id != self.task_id:
            raise AgentError("TASK_CANCEL_FAILED", "The task is not owned by the active controller.")
        self.stop_course()
        return self.tasks.get(task_id)

    def wait_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.leases.hold("agent_wait"):
            return self.tasks.wait(payload["taskId"], payload["afterRevision"], payload["timeoutMs"])

    def wait_events(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.leases.hold("agent_wait"):
            return self.events.wait_events(payload["afterSeq"], payload["timeoutMs"], payload["limit"], payload["categories"])
