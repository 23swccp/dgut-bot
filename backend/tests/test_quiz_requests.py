import copy
import json
import threading
import unittest

from dgutbot.agent.agent_leases import LeaseManager
from dgutbot.agent.agent_protocol import AgentError
from dgutbot.agent.agent_tasks import IdempotencyStore, TaskManager
from dgutbot.app.backend_commands import EventBuffer
from dgutbot.course.quiz_requests import QuizRequestManager
from dgutbot.course.yxy_quiz import QuizHandler, QUIZ_STATE_JS


class QuizFixture:
    def __init__(self):
        self.actions = []
        self.verify_success = True
        self.state = {"present": True, "pageId": "page_synthetic", "viewport": {"w": 200, "h": 200}, "modal": None, "questions": []}
        for qid, kind in [("q_choice", "单选题"), ("q_bool", "判断题"), ("q_blank", "填空题")]:
            self.state["questions"].append({
                "qid": qid, "type": kind, "title": "Synthetic question", "finished": False,
                "choices": [{"label": "A", "text": "Synthetic option", "selected": False, "pos": {"x": 10, "y": 10}}] if kind == "单选题" else [],
                "judgment": [{"label": "错误", "selected": False, "pos": {"x": 20, "y": 20}}] if kind == "判断题" else [],
                "blanks": [{"value": "", "pos": {"x": 30, "y": 30}}] if kind == "填空题" else [],
                "submit": {"x": 90, "y": 90},
            })
        self.block_entered = None
        self.block_release = None

    def evaluate(self, script):
        return json.dumps(self.state) if script == QUIZ_STATE_JS else "ok"

    def click(self, x, y):
        self.actions.append(("click", x))
        if self.block_entered is not None:
            self.block_entered.set()
            self.block_release.wait(2)
        if x == 10:
            self.state["questions"][0]["choices"][0]["selected"] = True
        elif x == 20:
            self.state["questions"][1]["judgment"][0]["selected"] = True
        elif x == 30:
            self.state["questions"][2]["blanks"][0]["focused"] = True
        elif x == 90 and self.verify_success:
            for question in self.state["questions"]:
                question["finished"] = True
        return True

    def type_text(self, text):
        self.actions.append(("type", text))
        self.state["questions"][2]["blanks"][0]["value"] = text
        return True

    def handler(self):
        return QuizHandler(evaluate=self.evaluate, click=self.click, type_text=self.type_text,
                           sleep=lambda _: None, log=lambda *_: None, dry_run=False, jitter=0)


class QuizRequestTests(unittest.TestCase):
    def setUp(self):
        self.fixture = QuizFixture()
        self.tasks = TaskManager()
        self.task = self.tasks.create()
        self.tasks.update(self.task["taskId"], state="running")
        self.clock = [10.0]
        self.manager = QuizRequestManager(self.tasks, EventBuffer(), LeaseManager(), clock=lambda: self.clock[0])
        self.context = {"running": True, "taskId": self.task["taskId"], "sessionId": "session_synthetic", "pageId": "page_synthetic"}
        self.request = self.manager.create(self.task["taskId"], "session_synthetic", "page_synthetic", self.fixture.handler(), lambda: dict(self.context), 10000)
        self.payload = {"requestId": self.request["requestId"], "revision": 1, "answers": [
            {"questionId": "q_choice", "value": ["A"]}, {"questionId": "q_bool", "value": False},
            {"questionId": "q_blank", "value": ["synthetic answer"]},
        ]}

    def assert_error(self, code, payload=None, submit=True):
        with self.assertRaises(AgentError) as caught:
            self.manager.validate_or_submit(payload or self.payload, submit=submit)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.fixture.actions, [])

    def test_validate_is_action_free_and_dto_has_no_targets(self):
        value = self.manager.validate_or_submit(self.payload)
        self.assertTrue(value["valid"])
        self.assertEqual(self.fixture.actions, [])
        encoded = json.dumps(self.manager.get(self.request["requestId"]))
        self.assertNotIn('"pos"', encoded)
        self.assertNotIn('"submit"', encoded)
        self.assertNotIn("synthetic answer", encoded)

    def test_complete_and_at_most_one_submit(self):
        store = IdempotencyStore()
        payload = {**self.payload, "idempotencyKey": "synthetic_key"}
        first = store.execute("quiz.submit_answers", payload, lambda: self.manager.validate_or_submit(payload, submit=True))
        result = self.manager.wait(self.request["requestId"])
        second = store.execute("quiz.submit_answers", payload, lambda: self.fail("repeated execution"))
        self.assertEqual(first, second)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.fixture.actions.count(("click", 90)), 1)
        self.assertEqual(self.tasks.get(self.task["taskId"])["state"], "running")

    def test_all_answers_validated_before_any_action(self):
        invalids = [
            [{"questionId": "q_choice", "value": ["Z"]}, *self.payload["answers"][1:]],
            [{"questionId": "q_choice", "value": ["A", "A"]}, *self.payload["answers"][1:]],
            self.payload["answers"][:-1],
            [*self.payload["answers"][:2], {"questionId": "q_blank", "value": []}],
            [*self.payload["answers"], {"questionId": "extra", "value": True}],
        ]
        for answers in invalids:
            self.assert_error("QUIZ_ANSWER_INVALID", {**self.payload, "answers": answers})

    def test_stale_revision(self):
        self.assert_error("QUIZ_REVISION_MISMATCH", {**self.payload, "revision": 2})

    def test_page_change(self):
        self.context["pageId"] = "page_other"
        self.assert_error("QUIZ_PAGE_CHANGED")

    def test_session_change(self):
        self.context["sessionId"] = "session_other"
        self.assert_error("QUIZ_PAGE_CHANGED")

    def test_question_structure_change(self):
        self.fixture.state["questions"][0]["choices"][0]["label"] = "B"
        self.assert_error("QUIZ_PAGE_CHANGED")

    def test_expiration(self):
        self.clock[0] = 21
        self.assert_error("QUIZ_REQUEST_EXPIRED")

    def test_concurrent_submission_rejected(self):
        self.fixture.block_entered = threading.Event()
        self.fixture.block_release = threading.Event()
        self.manager.validate_or_submit(self.payload, submit=True)
        self.assertTrue(self.fixture.block_entered.wait(1))
        with self.assertRaises(AgentError) as caught:
            self.manager.validate_or_submit(self.payload, submit=True)
        self.assertEqual(caught.exception.code, "QUIZ_BUSY")
        self.fixture.block_release.set()
        self.assertEqual(self.manager.wait(self.request["requestId"])["state"], "completed")
        self.assertEqual(self.fixture.actions.count(("click", 90)), 1)

    def test_failed_verification_never_resubmits(self):
        self.fixture.verify_success = False
        self.manager.validate_or_submit(self.payload, submit=True)
        value = self.manager.wait(self.request["requestId"])
        self.assertEqual(value["state"], "failed")
        self.assertEqual(value["error"]["code"], "QUIZ_VERIFY_FAILED")
        self.assertEqual(self.fixture.actions.count(("click", 90)), 1)

    def test_cancel_pending(self):
        self.manager.cancel_task(self.task["taskId"])
        self.assertEqual(self.manager.get(self.request["requestId"])["state"], "cancelled")
        self.assertEqual(self.fixture.actions, [])

    def test_multiple_choice_is_unsupported(self):
        self.fixture.state["questions"][0]["type"] = "多选题"
        request = self.manager.create(self.task["taskId"], "session_synthetic", "page_synthetic", self.fixture.handler(), lambda: self.context, 10000)
        self.assertEqual(request["questions"][0]["type"], "unsupported")
        self.assert_error("QUIZ_UNSUPPORTED_TYPE", {**self.payload, "requestId": request["requestId"]})


if __name__ == "__main__":
    unittest.main()
