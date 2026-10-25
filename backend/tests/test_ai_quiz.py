import json
from types import SimpleNamespace

import pytest

from dgutbot.agent.protocol import AgentError
from dgutbot.course.ai_quiz import AiAnswerError, UlearningAiAnswerProvider, _batches, _parse_reply, _prompt
from dgutbot.course.yxy_course import CourseConfig
from dgutbot.course.yxy_quiz import QuizHandler, QUIZ_STATE_JS
from dgutbot.experimental.ulearning_ai import AiModel, UlearningAiError


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
    assert [q["answerFormat"] for q in envelope["questions"]] == [q["answerSchema"] for q in QUESTIONS]


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
        self.model_ids = []

    def models(self):
        return (AiModel(1, "通义千问"),)

    def complete(self, messages, **kwargs):
        self.calls += 1
        self.model_id = kwargs.get("model_id")
        self.model_ids.append(self.model_id)
        prompt = messages[0]["content"]
        envelope = json.loads(prompt.split("INPUT_JSON=", 1)[1])
        request_id = envelope["requestId"]
        self.question_indexes = [question["index"] for question in envelope["questions"]]
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
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


def test_provider_reports_failure_before_any_action_after_two_invalid_replies():
    page, bridge, _logs, provider, controller = provider_setup(["bad", "still bad"])
    result = provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))
    assert result["state"] == "failed"
    assert page.actions == []
    assert bridge.calls == 2


def test_provider_does_not_generate_again_after_page_action_failure():
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


@pytest.mark.parametrize("change", ["session", "page", "title", "stopped", "modal"])
@pytest.mark.parametrize("bad_reply", [False, True])
def test_provider_rejects_changes_during_ai_request_without_retry_or_model_switch(change, bad_reply):
    page, bridge, _logs, provider, controller = provider_setup([])

    def changed(request_id, questions):
        if change == "session":
            controller._session_id = "restarted"
        elif change == "page":
            page.state["pageId"] = "other"
        elif change == "title":
            page.state["questions"][0]["title"] = "new question"
        elif change == "modal":
            page.state["modal"] = {"title": "blocked"}
        else:
            controller._running = False
        return "bad-json" if bad_reply else valid_for(request_id, questions)

    bridge.values = [changed]
    with pytest.raises(AgentError):
        provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))
    assert page.actions == []
    assert bridge.calls == 1


def test_provider_starts_timed_quiz_before_reading_new_questions():
    page, bridge, _logs, provider, controller = provider_setup([])
    page.state.update(startRequired=True, startButton={"x": 50, "y": 50, "enabled": True, "pointMatches": True})
    questions = page.state["questions"]
    page.state["questions"] = []
    handler = page.handler()

    def click(x, y):
        if x == 50:
            page.actions.append(("start", x))
            page.state["startRequired"] = False
            page.state["questions"] = questions
            return True
        return page.click(x, y)

    def answer(request_id, questions):
        assert page.actions == [("start", 50)]
        return valid_for(request_id, questions)

    handler._click = click
    bridge.values = [answer]
    result = provider.answer(handler, controller, CourseConfig(quiz_mode="ai"))
    assert result["state"] == "completed"
    assert page.actions.count(("start", 50)) == 1
    assert page.actions.count(("click", 90)) == 1


def test_provider_does_not_request_ai_after_timed_start_failure():
    page, bridge, _logs, provider, controller = provider_setup([])
    page.state.update(startRequired=True, startButton=None)
    with pytest.raises(AgentError, match="started"):
        provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))
    assert bridge.calls == 0
    assert page.actions == []


def test_provider_multiple_choice_removes_extra_selection_and_applies_unordered_answers():
    page, bridge, _logs, provider, controller = provider_setup([])
    question = page.state["questions"][0]
    question["type"] = "多选题"
    question["choices"].append({"label": "C", "text": "6", "selected": True, "pos": {"x": 12, "y": 12}})
    question["choices"][0]["selected"] = True
    handler = page.handler()

    def click(x, y):
        if x in {10, 11, 12}:
            page.actions.append(("click", x))
            option = next(item for item in question["choices"] if item["pos"]["x"] == x)
            option["selected"] = not option["selected"]
            return True
        return page.click(x, y)

    def answer(request_id, questions):
        assert questions[0]["type"] == "multiple_choice"
        assert questions[0]["answerFormat"]["uniqueItems"] is True
        return json.dumps({"requestId": request_id, "answers": [["B", "A"], True, ["answer"]]})

    handler._click = click
    bridge.values = [answer]
    result = provider.answer(handler, controller, CourseConfig(quiz_mode="ai"))
    assert result["state"] == "completed"
    assert [o["label"] for o in question["choices"] if o["selected"]] == ["A", "B"]
    assert ("click", 10) not in page.actions
    assert page.actions.count(("click", 12)) == 1
    assert page.actions.count(("click", 90)) == 1


@pytest.mark.parametrize("answer", [[], ["A", "A"], ["Z"], "AB", [True]])
def test_multiple_choice_invalid_reply_fails_before_input(answer):
    page, bridge, _logs, provider, controller = provider_setup([])
    page.state["questions"][0]["type"] = "多选题"

    def invalid(request_id, _questions):
        return json.dumps({"requestId": request_id, "answers": [answer, True, ["answer"]]})

    bridge.values = [invalid, invalid]
    assert provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))["state"] == "failed"
    assert page.actions == []


def test_provider_does_not_report_submission_when_all_detected_types_are_disabled():
    page, bridge, _logs, provider, controller = provider_setup([])
    config = CourseConfig(quiz_mode="ai", quiz_choice_enabled=False, quiz_judgment_enabled=False, quiz_blank_enabled=False)
    assert provider.answer(page.handler(), controller, config)["state"] == "skipped"
    assert bridge.calls == 0
    assert page.actions == []


def test_provider_routes_across_quota_and_format_failures_before_submitting_once():
    page, bridge, logs, provider, controller = provider_setup([
        UlearningAiError("quota exhausted"), "bad json", "still bad", valid_for,
    ])
    bridge.models = lambda: (AiModel(2, "豆包"), AiModel(1, "通义千问"), AiModel(6, "本地版"))
    provider.model_id = 2
    result = provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))
    assert result["state"] == "completed"
    assert bridge.model_ids == [2, 6, 6, 1]
    assert page.actions.count(("click", 90)) == 1
    assert any("自动切换" in text for text, _ in logs)


def test_provider_reports_failure_after_all_enabled_models_fail():
    page, bridge, _logs, provider, controller = provider_setup([
        UlearningAiError("quota"), UlearningAiError("timeout"),
    ])
    bridge.models = lambda: (AiModel(1, "通义千问"), AiModel(6, "本地版"))
    result = provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))
    assert result["state"] == "failed" and "所有可用" in result["reason"]
    assert bridge.model_ids == [1, 6]
    assert page.actions == []


def test_provider_does_not_guess_unsupported_question_types():
    page, bridge, _logs, provider, controller = provider_setup([])
    page.state["questions"][0]["type"] = "简答题"
    result = provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))
    assert result["state"] == "failed"
    assert "不支持" in result["reason"]
    assert bridge.calls == 0
    assert page.actions == []


def test_provider_does_not_apply_successful_early_batch_if_later_batch_fails():
    page, bridge, _logs, provider, controller = provider_setup([valid_for, "bad", "still bad"])
    for question in page.state["questions"]:
        question["title"] = "x" * 15_000
    result = provider.answer(page.handler(), controller, CourseConfig(quiz_mode="ai"))
    assert result["state"] == "failed"
    assert bridge.calls == 3
    assert page.actions == []
