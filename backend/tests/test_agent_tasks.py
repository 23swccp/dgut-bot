import threading
import time
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

from dgutbot.agent.agent_protocol import AgentError
from dgutbot.agent.agent_tasks import IdempotencyStore, TaskManager
from dgutbot.agent.agent_leases import LeaseManager
from dgutbot.app.backend_commands import EventBuffer
from dgutbot.agent.agent_service import AgentService
from dgutbot.agent.agent_tools import build_registry


class AgentTaskTests(unittest.TestCase):
    def test_waiting_for_agent_is_not_a_watchdog_stall(self):
        from dgutbot.course.yxy_course import CourseController, CourseConfig
        controller = CourseController(lambda *_: None)
        controller._active_config = CourseConfig(quiz_mode="agent")
        controller._quiz_busy = True
        controller._running = True
        controller._stop_event = Mock()
        controller._stop_event.wait.side_effect = [False, True]
        controller.status_snapshot = Mock(side_effect=AssertionError("Waiting must not recover media"))
        controller._watchdog_loop()
        controller.status_snapshot.assert_not_called()

    def test_course_task_start_idempotency_cancel_and_leases(self):
        events = EventBuffer()
        controller = SimpleNamespace(_running=False, ws_url="synthetic")
        backend = SimpleNamespace(_course_controller=controller, _course_operation_lock=threading.RLock(),
                                  monitor_thread=None, reserve_course_start=lambda _: True, release_course_start=lambda _: None)
        calls = []
        def start(**kwargs):
            calls.append(kwargs)
            controller._running = True
            events.emit_event("SESSION_STARTED", "info", "session", "Started", session_id="synthetic_session")
            return True
        def stop():
            controller._running = False
            events.emit_event("SESSION_STOPPED", "info", "session", "Stopped", session_id="synthetic_session")
        backend.start_course_helper, backend.stop_course_helper = start, stop
        service = AgentService(backend, events)
        registry = build_registry(backend, services=service)
        first = registry.call("course.start", {"idempotencyKey": "synthetic_start"})
        second = registry.call("course.start", {"idempotencyKey": "synthetic_start", "rate": 8, "quizMode": "agent", "quizRequestTimeoutMs": 600000})
        self.assertEqual(first, second)
        value = service.tasks.wait(first["taskId"], first["revision"], 1000)
        self.assertEqual(value["task"]["state"], "running")
        self.assertEqual(len(calls), 1)
        self.assertTrue(service.active())
        cancelled = registry.call("task.cancel", {"taskId": first["taskId"], "idempotencyKey": "synthetic_cancel"})
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertFalse(service.active())
        self.assertFalse(controller._running)

    def test_revision_wait_and_terminal_state(self):
        manager = TaskManager()
        task = manager.create()
        observed = []
        thread = threading.Thread(target=lambda: observed.append(manager.wait(task["taskId"], 1, 1000)))
        thread.start()
        manager.update(task["taskId"], state="running")
        thread.join(2)
        self.assertFalse(observed[0]["timedOut"])
        self.assertEqual(observed[0]["task"]["revision"], 2)
        final = manager.update(task["taskId"], state="completed")
        self.assertEqual(manager.update(task["taskId"], state="running"), final)

    def test_wait_timeout_and_invalid_transition(self):
        manager = TaskManager()
        task = manager.create()
        self.assertTrue(manager.wait(task["taskId"], 1, 1)["timedOut"])
        with self.assertRaises(ValueError):
            manager.update(task["taskId"], state="made_up")

    def test_idempotency_conflict_expiry_and_capacity(self):
        now = [0.0]
        store = IdempotencyStore(capacity=1, ttl=10, clock=lambda: now[0])
        handler = Mock(return_value={"taskId": "task_synthetic"})
        payload = {"idempotencyKey": "key", "rate": 8}
        self.assertEqual(store.execute("course.start", payload, handler), store.execute("course.start", payload, handler))
        handler.assert_called_once()
        with self.assertRaises(AgentError) as caught:
            store.execute("course.start", {**payload, "rate": 4}, handler)
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        with self.assertRaises(AgentError):
            store.execute("course.start", {"idempotencyKey": "other"}, handler)
        now[0] = 11
        store.execute("course.start", {"idempotencyKey": "other"}, handler)
        self.assertEqual(handler.call_count, 2)

    def test_duplicate_inflight_runs_only_once(self):
        store = IdempotencyStore()
        entered, release = threading.Event(), threading.Event()
        calls, results = [], []
        def handler():
            calls.append(1)
            entered.set()
            release.wait(2)
            return {"ok": True}
        def invoke():
            results.append(store.execute("test", {"idempotencyKey": "same"}, handler))
        a, b = threading.Thread(target=invoke), threading.Thread(target=invoke)
        a.start()
        self.assertTrue(entered.wait(1))
        b.start()
        release.set()
        a.join(2)
        b.join(2)
        self.assertEqual(calls, [1])
        self.assertEqual(len(results), 2)

    def test_lease_released_on_failure(self):
        leases = LeaseManager()
        with self.assertRaises(RuntimeError):
            with leases.hold("agent_wait"):
                self.assertTrue(leases.active())
                raise RuntimeError()
        self.assertFalse(leases.active())

    def test_events_are_bounded_and_diagnostics_filtered(self):
        events = EventBuffer(maxlen=2)
        events.emit_event("DEBUG_LOG", "info", "debug", "must not cross boundary", data={"token": "synthetic"})
        events.emit_event("PAGE_ENTERED", "info", "course", "raw diagnostic", data={"url": "synthetic", "count": 2})
        events.emit_event("QUIZ_PENDING", "info", "quiz", "pending", data={"requestId": "quiz_synthetic"})
        value = events.wait_events(0, 0, 1, [])
        self.assertEqual(value["nextSeq"], 2)
        self.assertEqual(value["droppedBeforeSeq"], 1)
        self.assertEqual(value["events"][0]["data"], {"count": 2})
        self.assertEqual(value["events"][0]["message"], "PAGE_ENTERED")
        self.assertTrue(events.wait_events(3, 1, 10, [])["timedOut"])


if __name__ == "__main__":
    unittest.main()
