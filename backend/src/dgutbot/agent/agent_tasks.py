"""Bounded task snapshots, notification waits and per-instance idempotency."""

from __future__ import annotations

import copy
import json
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from dgutbot.agent.agent_protocol import AgentError

TERMINAL = {"completed", "failed", "cancelled"}
TRANSITIONS = {
    "queued": {"running", "waiting_for_input", "failed", "cancelled"},
    "running": {"waiting_for_input", "completed", "failed", "cancelled"},
    "waiting_for_input": {"running", "completed", "failed", "cancelled"},
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class TaskManager:
    def __init__(self, capacity: int = 256) -> None:
        self._condition = threading.Condition()
        self._tasks: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._capacity = capacity

    def create(self, kind: str = "course") -> dict[str, Any]:
        with self._condition:
            if len(self._tasks) >= self._capacity:
                victim = next((key for key, task in self._tasks.items() if task["state"] in TERMINAL), None)
                if victim is None:
                    raise AgentError("TASK_CAPACITY_EXCEEDED", "Task capacity has been reached.", retryable=True)
                del self._tasks[victim]
            stamp = now_iso()
            task = {
                "taskId": f"task_{uuid4().hex}", "kind": kind, "state": "queued", "revision": 1,
                "createdAt": stamp, "updatedAt": stamp,
                "progress": {"current": 0, "total": 0, "unit": "page"},
                "waiting": None, "result": None, "error": None,
            }
            self._tasks[task["taskId"]] = task
            self._condition.notify_all()
            return copy.deepcopy(task)

    def _get(self, task_id: str) -> dict[str, Any]:
        if task_id not in self._tasks:
            raise AgentError("TASK_NOT_FOUND", "The task does not exist.")
        return self._tasks[task_id]

    def get(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            return copy.deepcopy(self._get(task_id))

    def update(self, task_id: str, **changes: Any) -> dict[str, Any]:
        with self._condition:
            task = self._get(task_id)
            state = changes.get("state", task["state"])
            if task["state"] in TERMINAL:
                return copy.deepcopy(task)
            if state != task["state"] and state not in TRANSITIONS.get(task["state"], set()):
                raise ValueError("Illegal task state transition")
            allowed = {"state", "progress", "waiting", "result", "error"}
            changes = {key: copy.deepcopy(value) for key, value in changes.items() if key in allowed}
            if any(task.get(key) != value for key, value in changes.items()):
                task.update(changes)
                task["revision"] += 1
                task["updatedAt"] = now_iso()
                self._condition.notify_all()
            return copy.deepcopy(task)

    def wait(self, task_id: str, after_revision: int, timeout_ms: int) -> dict[str, Any]:
        if not 0 <= timeout_ms <= 30000:
            raise AgentError("TOOL_INPUT_INVALID", "Wait timeout must be between 0 and 30000 ms.")
        with self._condition:
            self._get(task_id)
            changed = self._condition.wait_for(
                lambda: self._get(task_id)["revision"] > after_revision or self._get(task_id)["state"] in TERMINAL,
                timeout=timeout_ms / 1000,
            )  # Condition releases its lock for the actual wait.
            return {"task": copy.deepcopy(self._get(task_id)), "timedOut": not changed}

    def has_active(self) -> bool:
        with self._condition:
            return any(task["state"] not in TERMINAL for task in self._tasks.values())


class IdempotencyStore:
    def __init__(self, capacity: int = 512, ttl: float = 3600, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._capacity, self._ttl, self._clock = capacity, ttl, clock

    def execute(self, tool: str, payload: dict[str, Any], handler: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        key = (tool, payload["idempotencyKey"])
        canonical = json.dumps({k: v for k, v in payload.items() if k != "idempotencyKey"}, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self._lock:
            now = self._clock()
            for old_key, entry in list(self._entries.items()):
                if entry["done"].is_set() and now - entry["time"] >= self._ttl:
                    del self._entries[old_key]
            entry = self._entries.get(key)
            owner = entry is None
            if entry is not None and entry["input"] != canonical:
                raise AgentError("IDEMPOTENCY_CONFLICT", "The idempotency key was used with different input.")
            if owner:
                if len(self._entries) >= self._capacity:
                    # Do not evict unexpired results: replaying a side effect is worse than backpressure.
                    raise AgentError("IDEMPOTENCY_CAPACITY_EXCEEDED", "Idempotency capacity has been reached.", retryable=True)
                entry = {"input": canonical, "done": threading.Event(), "time": now, "result": None, "error": None}
                self._entries[key] = entry
        if not owner:
            if not entry["done"].wait(30):
                raise AgentError("REQUEST_IN_PROGRESS", "The original request is still running.", retryable=True)
        else:
            try:
                entry["result"] = copy.deepcopy(handler())
            except AgentError as error:
                entry["error"] = error
            except Exception:
                entry["error"] = AgentError("INTERNAL_ERROR", "The operation failed internally.")
            finally:
                entry["time"] = self._clock()
                entry["done"].set()
        if entry["error"]:
            raise entry["error"]
        return copy.deepcopy(entry["result"])
