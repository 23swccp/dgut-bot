from types import SimpleNamespace

import pytest

from dgutbot.experimental.ai_router import AiReplyValidationError, AiRouter
from dgutbot.experimental.ulearning_ai import AiModel, UlearningAiError
from dgutbot.experimental.ulearning_ai_bridge import BridgeReply


MODELS = (AiModel(2, "豆包"), AiModel(4, "视觉", vision=True),
          AiModel(1, "通义千问"), AiModel(6, "通义千问3.8-本地版"))
MESSAGES = [{"role": "user", "content": "synthetic question"}]


def setup(replies, models=MODELS):
    calls, logs = [], []

    def complete(messages, *, model_id):
        calls.append((model_id, messages))
        item = replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return BridgeReply(item, "")

    bridge = SimpleNamespace(models=lambda: models, complete=complete)
    return AiRouter(bridge, lambda *args: logs.append(args)), calls, logs


def test_selected_model_is_first_and_success_has_actual_model():
    router, calls, logs = setup(["answer"])
    result = router.complete(MESSAGES, model_id=1)
    assert result.model_id == 1 and result.model_name == "通义千问"
    assert result.reply.text == "answer"
    assert [c[0] for c in calls] == [1]
    assert not any("切换" in text for text, _ in logs)


@pytest.mark.parametrize("message", ["doubao每日token数或联网搜索次数已用完", "request timed out", "service unavailable"])
def test_upstream_failure_immediately_switches_to_local_text_model(message):
    router, calls, logs = setup([UlearningAiError(message), "answer"])
    result = router.complete(MESSAGES, model_id=2)
    assert result.model_id == 6
    assert [c[0] for c in calls] == [2, 6]
    assert any(message in text for text, _ in logs)
    assert any("自动切换" in text and "本地版" in text for text, _ in logs)


def test_invalid_answer_is_repaired_once_then_routed_to_next_model():
    router, calls, _ = setup(["bad", "still bad", "valid"])

    def validate(reply):
        if reply.text != "valid":
            raise AiReplyValidationError("invalid format")
        return {"validated": True}

    result = router.complete(MESSAGES, model_id=2, validate=validate,
                             repair=lambda reason: [{"role": "user", "content": reason}])
    assert result.value == {"validated": True}
    assert [c[0] for c in calls] == [2, 2, 6]
    assert calls[1][1][0]["content"] == "invalid format"
    assert calls[2][1] == MESSAGES  # 新模型不继承上一模型的错误上下文。


def test_all_candidates_tried_once_on_transport_failure_and_report_all_errors():
    router, calls, _ = setup([UlearningAiError(f"failure{i}") for i in range(4)])
    with pytest.raises(UlearningAiError, match="所有可用") as caught:
        router.complete(MESSAGES, model_id=2)
    assert [c[0] for c in calls] == [2, 6, 1, 4]
    assert all(f"failure{i}" in str(caught.value) for i in range(4))


def test_removed_preferred_model_still_uses_an_enabled_model():
    router, calls, _ = setup(["answer"])
    assert router.complete(MESSAGES, model_id=99).model_id == 6
    assert [c[0] for c in calls] == [6]


def test_duplicate_models_are_not_retried_as_separate_candidates():
    router, calls, _ = setup([UlearningAiError("failed")], models=(MODELS[0], MODELS[0]))
    with pytest.raises(UlearningAiError):
        router.complete(MESSAGES, model_id=2)
    assert [c[0] for c in calls] == [2]


def test_no_models_or_invalid_client_input_does_not_make_requests():
    router, calls, _ = setup([], models=())
    with pytest.raises(UlearningAiError, match="No enabled"):
        router.complete(MESSAGES, model_id=2)
    with pytest.raises(ValueError):
        router.complete([], model_id=2)
    assert calls == []


def test_session_guard_failure_cannot_trigger_model_switch():
    router, calls, _ = setup([UlearningAiError("quota"), "answer"])

    def guard():
        if calls:
            raise RuntimeError("session changed")

    with pytest.raises(RuntimeError, match="session changed"):
        router.complete(MESSAGES, model_id=2, guard=guard)
    assert [c[0] for c in calls] == [2]


def test_reasoning_only_response_routes_to_a_model_with_answer_text():
    router, calls, _ = setup(["", "answer"])
    assert router.complete(MESSAGES, model_id=2).model_id == 6
    assert [c[0] for c in calls] == [2, 6]
