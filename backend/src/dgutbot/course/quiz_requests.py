"""Semantic quiz inbox and stale-answer/at-most-once execution protection."""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from dgutbot.agent.agent_protocol import AgentError, validate_schema
from dgutbot.course.yxy_quiz import QuizExecutor, QuizReader

QUIZ_TERMINAL = {"completed", "expired", "failed", "cancelled"}


class AnswerValidator:
    @staticmethod
    def validate(questions: list[dict[str, Any]], answers: list[dict[str, Any]]) -> dict[str, Any]:
        if any(q["type"] == "unsupported" for q in questions):
            raise AgentError("QUIZ_UNSUPPORTED_TYPE", "The quiz contains an unsupported question type.")
        if not isinstance(answers, list):
            raise AgentError("QUIZ_ANSWER_INVALID", "Answers must be an array.")
        by_id = {}
        for answer in answers:
            if not isinstance(answer, dict) or set(answer) - {"questionId", "value", "confidence"}:
                raise AgentError("QUIZ_ANSWER_INVALID", "An answer has invalid fields.")
            qid = answer.get("questionId")
            if not isinstance(qid, str) or qid in by_id or "value" not in answer:
                raise AgentError("QUIZ_ANSWER_INVALID", "Answer question IDs must be unique.")
            confidence = answer.get("confidence")
            if "confidence" in answer and validate_schema(confidence, {"type": "number", "minimum": 0, "maximum": 1}):
                raise AgentError("QUIZ_ANSWER_INVALID", "Answer confidence is invalid.")
            by_id[qid] = copy.deepcopy(answer["value"])
        if set(by_id) != {q["id"] for q in questions}:
            raise AgentError("QUIZ_ANSWER_INVALID", "Answers must cover exactly all unfinished questions.")
        for question in questions:
            if validate_schema(by_id[question["id"]], question["answerSchema"]):
                raise AgentError("QUIZ_ANSWER_INVALID", "An answer does not match the question schema.")
        return by_id


class QuizRequestManager:
    def __init__(self, tasks, events, leases, *, clock=time.monotonic, capacity=256):
        self.tasks, self.events, self.leases = tasks, events, leases
        self._clock, self._capacity = clock, capacity
        self._lock = threading.Lock()
        self._requests: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def create(self, task_id, session_id, page_id, handler, context, timeout_ms):
        reader = QuizReader(handler)
        state = reader.read()
        questions = reader.questions(state)
        if not state.present or not page_id or state.page_id != page_id or not questions:
            raise AgentError("QUIZ_PAGE_CHANGED", "The quiz page is unavailable or changed.")
        if any(not q["id"] for q in questions) or len({q["id"] for q in questions}) != len(questions):
            raise AgentError("QUIZ_UNSUPPORTED_TYPE", "The question IDs are invalid.")
        now = datetime.now().astimezone()
        dto = {
            "requestId": f"quiz_{uuid4().hex}", "revision": 1, "taskId": task_id,
            "sessionId": session_id, "pageId": page_id, "state": "pending",
            "createdAt": now.isoformat(), "expiresAt": (now + timedelta(milliseconds=timeout_ms)).isoformat(),
            "submitPolicy": "apply_and_commit", "questions": questions,
        }
        entry = {"dto": dto, "reader": reader, "executor": QuizExecutor(handler), "context": context,
                 "deadline": self._clock() + timeout_ms / 1000, "busy": False, "submitted": False,
                 "done": threading.Event(), "result": None, "error": None}
        with self._lock:
            if len(self._requests) >= self._capacity:
                victim = next((key for key, value in self._requests.items() if value["dto"]["state"] in QUIZ_TERMINAL), None)
                if victim is None:
                    raise AgentError("QUIZ_BUSY", "The quiz inbox is full.", retryable=True)
                del self._requests[victim]
            self._requests[dto["requestId"]] = entry
        self.leases.set("quiz_pending", dto["requestId"], True)
        self.tasks.update(task_id, state="waiting_for_input", waiting={"type": "quiz_answers", "requestId": dto["requestId"]})
        self.events.emit_event("QUIZ_PENDING", "info", "quiz", "Quiz answers are required.", session_id=session_id,
                               page={"id": page_id}, data={"requestId": dto["requestId"], "questionCount": len(questions)})
        return copy.deepcopy(dto)

    def _entry(self, request_id):
        with self._lock:
            entry = self._requests.get(request_id)
        if entry is None:
            raise AgentError("QUIZ_REQUEST_NOT_FOUND", "The quiz request does not exist.")
        return entry

    def _finish(self, entry, state, error=None, result=None):
        with self._lock:
            if entry["dto"]["state"] in QUIZ_TERMINAL:
                return
            entry["dto"]["state"] = state
            entry["error"] = error.as_dict() if error else None
            entry["result"] = result
            entry["busy"] = False
            dto = copy.deepcopy(entry["dto"])
        self.leases.set("quiz_pending", dto["requestId"], False)
        if state == "completed":
            self.tasks.update(dto["taskId"], state="running", waiting=None)
        elif state in {"failed", "expired"}:
            self.tasks.update(dto["taskId"], state="failed", waiting=None, error=entry["error"])
        entry["done"].set()
        self.events.emit_event("QUIZ_" + state.upper(), "success" if state == "completed" else "info", "quiz",
                               "Quiz request " + state + ".", session_id=dto["sessionId"],
                               page={"id": dto["pageId"]}, data={"requestId": dto["requestId"]})

    def _expire(self, entry):
        if self._clock() >= entry["deadline"] and entry["dto"]["state"] in {"pending", "rejected", "validating", "staged"}:
            self._finish(entry, "expired", AgentError("QUIZ_REQUEST_EXPIRED", "The quiz request expired."))

    def get(self, request_id):
        entry = self._entry(request_id)
        self._expire(entry)
        with self._lock:
            return copy.deepcopy(entry["dto"])

    def list_pending(self):
        with self._lock:
            ids = list(self._requests)
        requests = [self.get(key) for key in ids]
        return {"requests": [{k: dto[k] for k in ("requestId", "revision", "taskId", "sessionId", "pageId", "state", "expiresAt")} | {"questionCount": len(dto["questions"])}
                             for dto in requests if dto["state"] in {"pending", "rejected"}]}

    def result(self, request_id):
        self.get(request_id)
        entry = self._entry(request_id)
        with self._lock:
            return {"requestId": request_id, "revision": entry["dto"]["revision"], "state": entry["dto"]["state"],
                    "result": copy.deepcopy(entry["result"]), "error": copy.deepcopy(entry["error"])}

    def _guard(self, entry):
        dto = entry["dto"]
        if dto["state"] in QUIZ_TERMINAL:
            code = "QUIZ_ALREADY_COMPLETED" if dto["state"] == "completed" else "QUIZ_REQUEST_EXPIRED"
            raise AgentError(code, "The quiz request is no longer actionable.")
        if self._clock() >= entry["deadline"]:
            raise AgentError("QUIZ_REQUEST_EXPIRED", "The quiz request expired.")
        context = entry["context"]()
        if (not context.get("running") or any(context.get(k) != dto[k] for k in ("taskId", "sessionId", "pageId"))):
            raise AgentError("QUIZ_PAGE_CHANGED", "The course, session or page has changed.")
        state = entry["reader"].read()
        if not state.present or state.page_id != dto["pageId"] or state.modal:
            raise AgentError("QUIZ_PAGE_CHANGED", "The quiz page has changed or is blocked.")
        if any(q.finished and q.qid in {item["id"] for item in dto["questions"]} for q in state.questions):
            raise AgentError("QUIZ_ALREADY_COMPLETED", "A requested question is already completed.")
        if entry["reader"].questions(state) != dto["questions"]:
            raise AgentError("QUIZ_PAGE_CHANGED", "The question structure has changed.")
        return state

    def validate_or_submit(self, payload, *, submit=False):
        entry = self._entry(payload["requestId"])
        self._expire(entry)
        with self._lock:
            dto = entry["dto"]
            if dto["state"] == "completed":
                raise AgentError("QUIZ_ALREADY_COMPLETED", "The quiz is already completed.")
            if dto["state"] in QUIZ_TERMINAL:
                raise AgentError("QUIZ_REQUEST_EXPIRED", "The quiz request is no longer actionable.")
            if payload["revision"] != dto["revision"]:
                raise AgentError("QUIZ_REVISION_MISMATCH", "The quiz revision does not match.")
            if entry["busy"]:
                raise AgentError("QUIZ_BUSY", "The quiz request is already being processed.", retryable=True)
            entry["busy"] = True
            dto["state"] = "validating"
        try:
            answers = AnswerValidator.validate(dto["questions"], payload["answers"])
            self._guard(entry)
            with self._lock:
                if dto["state"] in QUIZ_TERMINAL:
                    raise AgentError("QUIZ_REQUEST_EXPIRED", "The quiz request is no longer actionable.")
                dto["state"] = "staged" if submit else "pending"
            if not submit:
                with self._lock:
                    entry["busy"] = False
                return {"requestId": dto["requestId"], "revision": dto["revision"], "valid": True, "state": "pending"}
            threading.Thread(target=self._execute, args=(entry, answers), name="agent-quiz-submit", daemon=True).start()
            return {"requestId": dto["requestId"], "revision": dto["revision"], "state": "staged"}
        except AgentError as error:
            if error.code in {"QUIZ_PAGE_CHANGED", "QUIZ_REQUEST_EXPIRED", "QUIZ_ALREADY_COMPLETED"}:
                self._finish(entry, "expired", error)
            else:
                with self._lock:
                    if dto["state"] not in QUIZ_TERMINAL:
                        dto["state"] = "rejected"
                    entry["busy"] = False
            raise
        except Exception:
            error = AgentError("QUIZ_APPLY_FAILED", "Quiz validation could not be completed.")
            self._finish(entry, "failed", error)
            raise error

    def _execute(self, entry, answers):
        def before_submit():
            latest = self._guard(entry)
            with self._lock:
                if entry["submitted"] or entry["dto"]["state"] in QUIZ_TERMINAL:
                    raise AgentError("QUIZ_BUSY", "A submit attempt has already been reserved.")
                entry["submitted"] = True
                entry["dto"]["state"] = "submitting"
            return latest
        try:
            self._guard(entry)
            with self._lock:
                if entry["dto"]["state"] in QUIZ_TERMINAL:
                    return
                entry["dto"]["state"] = "applying"
            result = entry["executor"].execute(answers, lambda: self._guard(entry), before_submit)
            self._finish(entry, "completed", result=result)
        except AgentError as error:
            self._finish(entry, "expired" if error.code in {"QUIZ_PAGE_CHANGED", "QUIZ_REQUEST_EXPIRED"} else "failed", error)
        except Exception:
            self._finish(entry, "failed", AgentError("QUIZ_APPLY_FAILED", "Quiz execution failed."))

    def wait(self, request_id):
        entry = self._entry(request_id)
        if not entry["done"].wait(max(0, entry["deadline"] - self._clock())):
            self._expire(entry)
            # An already-started application may finish after the answer deadline.
            entry["done"].wait(30)
        return self.result(request_id)

    def cancel_task(self, task_id):
        with self._lock:
            entries = [e for e in self._requests.values() if e["dto"]["taskId"] == task_id]
        for entry in entries:
            self._finish(entry, "cancelled")

    def fail_task(self, task_id, code, message):
        with self._lock:
            entries = [e for e in self._requests.values() if e["dto"]["taskId"] == task_id]
        for entry in entries:
            self._finish(entry, "failed", AgentError(code, message))


class AgentAnswerProvider:
    def __init__(self, manager, task_id, timeout_ms):
        self.manager, self.task_id, self.timeout_ms = manager, task_id, timeout_ms

    def answer(self, handler, controller):
        def context():
            status = controller.status_snapshot()
            return {"taskId": self.task_id, "sessionId": controller._session_id,
                    "pageId": str((status.get("page") or {}).get("id") or ""), "running": controller._running}
        identity = context()
        try:
            request = self.manager.create(self.task_id, identity["sessionId"], identity["pageId"], handler, context, self.timeout_ms)
        except AgentError as error:
            self.manager.tasks.update(self.task_id, state="failed", error=error.as_dict(), waiting=None)
            raise
        return self.manager.wait(request["requestId"])
