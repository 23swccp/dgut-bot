"""Offline checks for selecting and typing into the homework editor."""

import unittest
from unittest.mock import patch

from dgutbot.app.homework_input import EDITOR_HTML_JS, EDITOR_TEXT_JS, FOCUS_JS, PROBE_JS, HomeworkInput


class HomeworkInputTests(unittest.TestCase):
    def test_only_exact_lms_homework_pages_are_candidates(self):
        good = {"type": "page", "url": "https://lms.dgut.edu.cn/homework/123", "webSocketDebuggerUrl": "ws://local"}
        self.assertTrue(HomeworkInput._is_homework_target(good))
        for url in ("https://evil.example/homework/123", "https://lms.dgut.edu.cn.evil.example/homework/123",
                    "https://lms.dgut.edu.cn/courseweb/ulearning/index.html"):
            self.assertFalse(HomeworkInput._is_homework_target({**good, "url": url}))

    def test_multiple_ready_editors_are_not_selected_silently(self):
        helper = HomeworkInput(lambda: 9222)
        with patch.object(helper, "_targets", return_value=[{"id": "one"}, {"id": "two"}]), \
                patch.object(helper, "_probe", return_value={"ready": True}):
            target, status = helper._ready_target()
        self.assertIsNone(target)
        self.assertEqual(status["state"], "ambiguous")

    def test_send_uses_cdp_input_and_verifies_each_chunk(self):
        class FakeSocket:
            def close(self):
                pass

        class FakeHomework(HomeworkInput):
            def __init__(self):
                super().__init__(lambda: 9222)
                self.content = "原有内容"
                self.html = "<p>原有内容</p>"
                self.focused = True
                self.calls = []

            def _ready_target(self):
                return {"webSocketDebuggerUrl": "ws://local"}, {"state": "ready", "message": "ready"}

            def _evaluate(self, _ws, _message_id, expression):
                if expression == PROBE_JS:
                    return {"ready": True, "x": 100, "y": 120}
                if expression == FOCUS_JS:
                    return self.focused
                if expression == EDITOR_TEXT_JS:
                    return self.content
                if expression == EDITOR_HTML_JS:
                    return self.html
                raise AssertionError("Unexpected evaluation")

            def _call(self, _ws, _message_id, method, params=None):
                self.calls.append((method, params))
                if method == "Input.insertText":
                    if self.content == "\n":  # Empty contenteditable drops its placeholder line on first input.
                        self.content = ""
                    self.content += params["text"]
                    self.html += params["text"]
                elif method == "Input.dispatchKeyEvent" and params["type"] == "keyDown" and params["key"] == "Enter":
                    self.content += "\n"
                    self.html += "<p><br></p>"
                return {}

        helper = FakeHomework()
        with patch("dgutbot.app.homework_input.create_connection", return_value=FakeSocket()):
            result = helper.send("第一段\n第二段")
        self.assertEqual(helper.content, "原有内容第一段\n第二段")
        self.assertEqual(result["state"], "sent")
        methods = [method for method, _ in helper.calls]
        self.assertEqual(methods[0], "Page.bringToFront")
        self.assertEqual([params["text"] for method, params in helper.calls if method == "Input.insertText"], ["第一段", "第二段"])
        self.assertNotIn("Runtime.evaluate", methods)
        blank = FakeHomework()
        blank.content = "\n"
        with patch("dgutbot.app.homework_input.create_connection", return_value=FakeSocket()):
            self.assertEqual(blank.send("测试")["state"], "sent")
        self.assertEqual(blank.content, "测试")

    def test_focus_failure_never_sends_text(self):
        helper = HomeworkInput(lambda: 9222)
        calls = []

        def evaluate(_ws, _id, expression):
            return {"ready": True, "x": 100, "y": 120} if expression == PROBE_JS else "" if expression == EDITOR_TEXT_JS else False

        with patch.object(helper, "_evaluate", side_effect=evaluate), patch.object(helper, "_call", side_effect=lambda _ws, _id, method, params=None: calls.append(method)):
            with self.assertRaisesRegex(RuntimeError, "焦点"):
                helper._send_to_editor(object(), "hello")
        self.assertNotIn("Input.insertText", calls)


if __name__ == "__main__":
    unittest.main()
