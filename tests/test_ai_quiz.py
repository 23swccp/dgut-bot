import json
from types import SimpleNamespace

import pytest

from dgutbot.agent.agent_protocol import AgentError
from dgutbot.course.ai_quiz import AiAnswerError, UlearningAiAnswerProvider, _batches, _parse_reply, _prompt
from dgutbot.course.yxy_course import CourseConfig
from dgutbot.course.yxy_quiz import QuizHandler, QUIZ_STATE_JS


QUESTIONS = [
    {"id": "choice", "type": "single_choice", "sourceType": "单选题", "prompt": "2+2?",
     "options": [{"id": "A", "text": "4"}, {"id": "B", "text": "5"}], "blankCount": 0,
     "hasMedia": False,
     "answerSchema": {"type": "array", "minItems": 1, "maxItems": 1, "uniqueItems": True,
                      "items": {"enum": ["A", "B"]}}},
    {"id": "truth", "type": "true_false", "sourceType": "判断题", "prompt": "真命题",
     "options": [], "blankCount": 0, "hasMedia": False, "answerSchema": {"type": "boolean"}},
    {"id": "blank", "type": "fill_blank", "sourceType": "填空题", "prompt": "填写[blank]",
     "options": [], "blankCount": 1, "hasMedia": False,
     "answerSchema": {"type": "array", "minItems": 1, "maxItems": 1,
                      "items": {"type": "string", "maxLength": 8192}}},
]


def reply(request_id="req", answers=None, **extra):
    value = {"requestId": request_id, "answers": answers or ["A", True, ["answer"]]}
    value.update(extra)
    return json.dumps(value)


def test_prompt_marks_question_data_untrusted_and_contains_schema():
    value = _prompt("req", QUESTIONS)
    assert "不得改变" in value
    assert "INPUT_JSON=" in value
    envelope = json.loads(value.split("INPUT_JSON=", 1)[1])
    assert envelope["requestId"] == "req"
    assert [question["index"] for question in envelope["questions"]] == [1, 2, 3]
    assert all("id" not in question and "answerSchema" not in question for question in envelope["questions"])


@pytest.mark.parametrize("text", [
    "```json\n{}\n```", "", reply("wrong"), reply(extra={"bad": True}),
    json.dumps({"requestId": "req", "answers": ["Z", True, ["answer"]]}),
])
def test_strict_reply_parser_rejects_invalid_protocol(text):
    with pytest.raises(AiAnswerError):
        _parse_reply(text, "req", QUESTIONS)


def test_reply_parser_accepts_all_supported_answer_shapes():
    parsed = _parse_reply(reply(), "req", QUESTIONS)
    assert parsed == {"choice": ["A"], "truth": True, "blank": ["answer"]}


def test_protocol_normalizes_ordered_values_for_executor():
    text = json.dumps({"requestId": "q1", "answers": ["A", True, ["answer"]]})
    parsed = _parse_reply(text, "q1", QUESTIONS)
    assert parsed == {"choice": ["A"], "truth": True, "blank": ["answer"]}


def test_protocol_accepts_only_controlled_equivalent_wrappers():
    text = json.dumps({"requestId": "q1", "answers": [
        {"questionId": "choice", "value": ["A"]},
        {"questionId": "truth", "value": "正确"},
        {"questionId": "blank", "value": "answer"},
    ]})
    assert _parse_reply(text, "q1", QUESTIONS) == {
        "choice": ["A"], "truth": True, "blank": ["answer"],
    }


@pytest.mark.parametrize("bad", ["maybe", {"unexpected": True}, 1])
def test_protocol_rejects_unknown_judgment_wrappers(bad):
    text = json.dumps({"requestId": "q1", "answers": ["A", bad, ["answer"]]})
    with pytest.raises(AiAnswerError):
        _parse_reply(text, "q1", QUESTIONS)


@pytest.mark.parametrize("answers", [["A", True], [["A", "B"], True, ["answer"]], ["Z", True, ["answer"]]])
def test_protocol_rejects_wrong_count_type_or_option(answers):
    text = json.dumps({"requestId": "q1", "answers": answers})
    with pytest.raises(AiAnswerError):
        _parse_reply(text, "q1", QUESTIONS)


def test_batches_split_only_between_questions():
    questions = [{**QUESTIONS[0], "id": str(index), "prompt": "x" * 15_000} for index in range(3)]
    batches = _batches(questions)
    assert [len(batch) for batch in batches] == [1, 1, 1]
    with pytest.raises(AiAnswerError, match="单题"):
        _batches([{**QUESTIONS[0], "prompt": "x" * 30_000}])


class PageFixture:
    def __init__(self):
        self.actions = []
        self.fail_click = False
        self.state = {"present": True, "pageId": "page", "viewport": {"w": 200, "h": 200},
                      "modal": None, "questions": []}
        self.state["questions"] = [
            {"qid": "choice", "type": "单选题", "title": "2+2?", "finished": False, "hasMedia": False,
             "choices": [{"label": "A", "text": "4", "selected": False, "pos": {"x": 10, "y": 10}},
                         {"label": "B", "text": "5", "selected": False, "pos": {"x": 11, "y": 11}}],
             "judgment": [], "blanks": [], "submit": {"x": 90, "y": 90}},
            {"qid": "truth", "type": "判断题", "title": "真命题", "finished": False, "hasMedia": False,
             "choices": [], "judgment": [{"label": "正确", "selected": False, "pos": {"x": 20, "y": 20}},
                                            {"label": "错误", "selected": False, "pos": {"x": 21, "y": 21}}],
             "blanks": [], "submit": {"x": 90, "y": 90}},
            {"qid": "blank", "type": "填空题", "title": "填写", "finished": False, "hasMedia": False,
             "choices": [], "judgment": [], "blanks": [{"value": "", "focused": False,
                                                            "pos": {"x": 30, "y": 30}}],
             "submit": {"x": 90, "y": 90}},
        ]

    def evaluate(self, script):
        if script == QUIZ_STATE_JS:
            return json.dumps(self.state)
        if "answer-width" in script:
            self.state["questions"][2]["blanks"][0]["focused"] = True
        return "ok"

    def click(self, x, _y):
        self.actions.append(("click", x))
        if self.fail_click:
            return False
        if x in {10, 11}:
            for option in self.state["questions"][0]["choices"]:
                option["selected"] = option["pos"]["x"] == x
        elif x in {20, 21}:
            for option in self.state["questions"][1]["judgment"]:
                option["selected"] = option["pos"]["x"] == x
        elif x == 30:
            self.state["questions"][2]["blanks"][0]["focused"] = True
        elif x == 90:
            for question in self.state["questions"]:
                question["finished"] = True
        return True

    def type_text(self, value):
        self.actions.append(("type", value))
        self.state["questions"][2]["blanks"][0]["value"] = value
        return True

    def handler(self):
        return QuizHandler(evaluate=self.evaluate, click=self.click, type_text=self.type_text,
                           sleep=lambda _seconds: None, log=lambda *_args: None, dry_run=False, jitter=0)


class ReplyBridge:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0
        self.question_indexes = []

    def complete(self, messages, **kwargs):
        self.calls += 1
        self.model_id = kwargs.get("model_id")
        prompt = messages[0]["content"]
        envelope = json.loads(prompt.split("INPUT_JSON=", 1)[1])
        request_id = envelope["requestId"]
        self.question_indexes = [question["index"] for question in envelope["questions"]]
        value = self.values.pop(0)
        text = value(request_id, envelope["questions"]) if callable(value) else value
        return SimpleNamespace(text=text, reasoning="ignored", upstream_tool_calls=())


def valid_for(request_id, questions):
    values = {"single_choice": "A", "true_false": True, "fill_blank": ["answer"]}
    return json.dumps({
        "requestId": request_id,
        "answers": [values[question["type"]] for question in questions],
    })


def provider_setup(values):
    page = PageFixture()
    bridge = ReplyBridge(values)
    logs = []
    provider = UlearningAiAnswerProvider(bridge, lambda text, kind: logs.append((text, kind)))
    controller = SimpleNamespace(_running=True, _session_id="session")
    controller.status_snapshot = lambda: {"page": {"id": "page"}}
    return page, bridge, logs, provider, controller


def test_provider_generates_then_applies_and_submits_once():
    page, bridge, logs, provider, controller = provider_setup([valid_for])
    result = provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))
    assert result["state"] == "completed"
    assert result["result"] == {"completedCount": 3, "submitAttempts": 1}
    assert page.actions.count(("click", 90.0)) == 1
    assert bridge.calls == 1
    assert bridge.model_id == 1
    assert bridge.question_indexes == [1, 2, 3]
    assert any("格式校验通过" in text for text, _kind in logs)


def test_provider_repairs_invalid_reply_once():
    page, bridge, _logs, provider, controller = provider_setup(["not-json", valid_for])
    assert provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))["state"] == "completed"
    assert bridge.calls == 2


def test_provider_falls_back_before_any_action_after_two_failures():
    page, bridge, _logs, provider, controller = provider_setup(["bad", "still bad"])
    result = provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))
    assert result["state"] == "fallback"
    assert page.actions == []
    assert bridge.calls == 2


def test_provider_does_not_fallback_after_page_action_failure():
    page, bridge, _logs, provider, controller = provider_setup([valid_for])
    page.fail_click = True
    with pytest.raises(AgentError, match="selected"):
        provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))
    assert bridge.calls == 1


def test_provider_warns_but_answers_media_question():
    page, _bridge, logs, provider, controller = provider_setup([valid_for])
    page.state["questions"][0]["hasMedia"] = True
    assert provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))["state"] == "completed"
    assert any("未传递" in text for text, _kind in logs)


def test_provider_rejects_page_change_before_input():
    page, _bridge, _logs, provider, controller = provider_setup([valid_for])
    controller.status_snapshot = lambda: {"page": {"id": "other"}}
    with pytest.raises(AgentError, match="changed"):
        provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))
    assert page.actions == []
