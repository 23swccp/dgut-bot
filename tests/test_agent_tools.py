import unittest
from types import SimpleNamespace

from dgutbot.agent.agent_protocol import validate_schema
from dgutbot.agent.agent_schemas import OUTPUTS
from dgutbot.agent.agent_tools import build_registry


class FakeBackend:
    token = "present"
    courses = [object()]
    selected_course = object()

    def course_helper_status(self):
        return {"running": False, "connected": False, "controllerState": "IDLE", "page": {}, "video": {}}


class AgentToolTests(unittest.TestCase):
    def test_first_read_only_tools_are_self_describing(self):
        registry = build_registry(FakeBackend(), instance_id="instance_test")
        names = [item["name"] for item in registry.capabilities()]
        self.assertEqual(names, sorted(names))
        self.assertEqual(names, ["course.get_status", "system.capabilities", "system.health", "system.version"])
        self.assertTrue(all(item["readOnly"] for item in registry.capabilities()))
        self.assertEqual(registry.call("system.version", {})["instanceId"], "instance_test")
        self.assertEqual(registry.call("system.health", {})["service"], "ready")

    def test_quiz_request_schema_accepts_media_presence_flag(self):
        request = {
            "requestId": "quiz_test", "revision": 1, "taskId": "task_test",
            "sessionId": "session_test", "pageId": "page_test", "state": "pending",
            "createdAt": "2026-09-06T00:00:00+08:00", "expiresAt": "2026-09-06T00:10:00+08:00",
            "submitPolicy": "apply_and_commit",
            "questions": [{
                "id": "question_test", "type": "single_choice", "sourceType": "单选题",
                "prompt": "synthetic", "options": [{"id": "A", "text": "synthetic"}],
                "blankCount": 0, "hasMedia": True,
                "answerSchema": {"type": "array", "items": {"type": "string"}},
            }],
        }
        self.assertEqual(validate_schema(request, OUTPUTS["quiz.get_request"]), [])


if __name__ == "__main__":
    unittest.main()
