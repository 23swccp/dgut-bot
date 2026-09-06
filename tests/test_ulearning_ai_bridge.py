from types import SimpleNamespace

import pytest

from dgutbot.experimental.ulearning_ai import AiModel, ChatChunk, ChatContext, UlearningAiError
from dgutbot.experimental.ulearning_ai_bridge import UlearningAiBridge, flatten_messages


def test_flatten_messages_preserves_single_user_query():
    assert flatten_messages([{"role": "user", "content": "  hello  "}]) == "hello"


def test_flatten_messages_includes_multiturn_roles():
    prompt = flatten_messages([
        {"role": "system", "content": "be concise"},
        {"role": "assistant", "content": "ready"},
        {"role": "user", "content": "answer"},
    ])
    assert "[system]\nbe concise" in prompt
    assert prompt.endswith("[user]\nanswer")


def test_flatten_messages_requires_final_user_message():
    with pytest.raises(ValueError, match="final"):
        flatten_messages([{"role": "assistant", "content": "hello"}])


def test_bridge_collects_text_reasoning_and_upstream_calls():
    access = SimpleNamespace(
        context=ChatContext("1", "2", "3"),
        create_session=lambda: object(),
    )

    class FakeClient:
        def __init__(self, _session):
            pass

        def list_models(self):
            return (AiModel(1, "Qwen"), AiModel(6, "DeepSeek"))

        def stream_chat(self, _context, **_kwargs):
            yield ChatChunk(text="hello", reasoning="think", tool_calls=({"id": "call-1"},))

    bridge = UlearningAiBridge(
        access_factory=lambda _port: access,
        client_factory=FakeClient,
    )
    reply = bridge.complete([{"role": "user", "content": "probe"}])
    assert reply.text == "hello"
    assert reply.reasoning == "think"
    assert reply.upstream_tool_calls == ({"id": "call-1"},)


def test_bridge_keeps_upstream_operations_serial_and_five_seconds_apart():
    now = [100.0]
    sleeps = []
    access = SimpleNamespace(
        context=ChatContext("1", "2", "3"),
        create_session=lambda: object(),
    )

    class FakeClient:
        def __init__(self, _session):
            pass

        def list_models(self):
            return (AiModel(1, "Qwen"),)

        def stream_chat(self, _context, **_kwargs):
            yield ChatChunk(text="ok")

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    bridge = UlearningAiBridge(
        access_factory=lambda _port: access,
        client_factory=FakeClient,
        monotonic=lambda: now[0],
        sleep=sleep,
    )
    bridge.complete([{"role": "user", "content": "one"}])
    bridge.complete([{"role": "user", "content": "two"}])
    assert sleeps == [5.0]


def test_bridge_probe_only_discovers_browser_access():
    calls = []
    access = SimpleNamespace(create_session=lambda: object())
    client = SimpleNamespace(list_models=lambda: (AiModel(1, "Qwen"),))
    bridge = UlearningAiBridge(
        access_factory=lambda port: calls.append(port) or access,
        client_factory=lambda _session: client,
        debug_port=lambda: 9333,
    )
    assert bridge.probe() is None
    assert calls == [9333]


def test_backend_chat_command_returns_only_safe_tool_count():
    from dgutbot.app import backend_commands

    reply = SimpleNamespace(text="answer", reasoning="", upstream_tool_calls=({"arguments": "private"},))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(backend_commands.AI_BRIDGE, "complete", lambda _messages, **_kwargs: reply)
        result = backend_commands.handle("ai_chat", {"messages": [{"role": "user", "content": "hello"}]})
    assert result == {"ok": True, "answer": "answer", "reasoning": "", "upstreamToolCallCount": 1}
    assert "private" not in repr(result)


def test_gui_course_start_requires_ready_ai_when_auto_answer_is_enabled():
    from dgutbot.app import backend_commands
    from dgutbot.experimental.ulearning_ai import UlearningAiError

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(backend_commands.backend.config, "course_quiz_auto_answer", True)
        monkeypatch.setattr(backend_commands.AI_BRIDGE, "probe", lambda *_args: (_ for _ in ()).throw(UlearningAiError("private")))
        start = SimpleNamespace(called=False)
        monkeypatch.setattr(backend_commands.backend, "start_course_helper", lambda **_kwargs: setattr(start, "called", True))
        result = backend_commands.handle("start_course_helper", {})
    assert result["ok"] is False
    assert "登录缓存" in result["error"]
    assert "AI" in result["error"]
    assert "private" not in repr(result)
    assert start.called is False


def test_gui_course_start_injects_ai_provider_after_probe():
    from dgutbot.app import backend_commands

    observed = []
    controller = SimpleNamespace(emit=lambda *_args: None)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(backend_commands.backend.config, "course_quiz_auto_answer", True)
        monkeypatch.setattr(backend_commands.AI_BRIDGE, "probe", lambda *_args: observed.append("probe"))
        monkeypatch.setattr(backend_commands.backend, "_course_controller", controller)
        monkeypatch.setattr(backend_commands.backend, "save_config", lambda: observed.append("disarmed"))
        monkeypatch.setattr(backend_commands.backend, "start_course_helper", lambda **kwargs: observed.append(kwargs) or True)
        result = backend_commands.handle("start_course_helper", {})
    assert result == {"ok": True, "quizAutoAnswerArmed": False}
    assert observed[0] == "probe"
    assert observed[1]["quiz_mode"] == "ai"
    assert observed[1]["ai_provider"].bridge is backend_commands.AI_BRIDGE
    assert observed[1]["ai_provider"].model_id == backend_commands.backend.config.course_ai_model_id
    assert observed[2] == "disarmed"
    assert backend_commands.backend.config.course_quiz_auto_answer is False


def test_failed_gui_course_start_keeps_one_shot_auto_answer_armed():
    from dgutbot.app import backend_commands

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(backend_commands.backend.config, "course_quiz_auto_answer", True)
        monkeypatch.setattr(backend_commands.AI_BRIDGE, "probe", lambda *_args: None)
        monkeypatch.setattr(backend_commands.backend, "start_course_helper", lambda **_kwargs: False)
        monkeypatch.setattr(
            backend_commands.backend,
            "save_config",
            lambda: pytest.fail("failed starts must not consume the one-shot setting"),
        )
        result = backend_commands.handle("start_course_helper", {})
    assert result == {"ok": False, "quizAutoAnswerArmed": True}


def test_gui_course_start_skips_ai_probe_when_auto_answer_is_disabled():
    from dgutbot.app import backend_commands

    observed = []
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(backend_commands.backend.config, "course_quiz_auto_answer", False)
        monkeypatch.setattr(backend_commands.AI_BRIDGE, "probe", lambda *_args: observed.append("probe"))
        monkeypatch.setattr(backend_commands.backend, "start_course_helper", lambda **kwargs: observed.append(kwargs) or True)
        result = backend_commands.handle("start_course_helper", {})
    assert result == {"ok": True}
    assert observed == [{}]


def test_backend_models_command_returns_dynamic_safe_fields():
    from dgutbot.app import backend_commands

    models = (AiModel(1, "通义千问"), AiModel(4, "通义千问VL", vision=True))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(backend_commands.AI_BRIDGE, "models", lambda: models)
        monkeypatch.setattr(backend_commands.backend.config, "course_ai_model_id", 4)
        result = backend_commands.handle("ai_models", {})
    assert result == {"ok": True, "models": [
        {"id": 1, "name": "通义千问", "vision": False, "online": False, "thinking": False},
        {"id": 4, "name": "通义千问VL", "vision": True, "online": False, "thinking": False},
    ], "selectedModelId": 4}


def test_backend_ai_access_prefers_cached_login_without_browser():
    from dgutbot.app import backend_commands

    expected = object()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(backend_commands.backend, "token", "memory-only")
        monkeypatch.setattr(backend_commands.backend, "courses", [SimpleNamespace(id=7)])
        monkeypatch.setattr(backend_commands.backend, "selected_course", None)
        monkeypatch.setattr(
            backend_commands,
            "discover_cached_access",
            lambda token, courses, **_kwargs: expected if token == "memory-only" and courses == [7] else None,
        )
        monkeypatch.setattr(
            backend_commands,
            "discover_browser_access",
            lambda _port: pytest.fail("browser discovery should not run"),
        )
        assert backend_commands._resolve_ai_access(9222) is expected


def test_backend_ai_access_falls_back_to_browser_when_cache_is_unusable():
    from dgutbot.app import backend_commands

    expected = object()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(backend_commands.backend, "token", "stale")
        monkeypatch.setattr(backend_commands.backend, "courses", [SimpleNamespace(id=7)])
        monkeypatch.setattr(backend_commands.backend, "selected_course", None)
        monkeypatch.setattr(
            backend_commands,
            "discover_cached_access",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(UlearningAiError("stale")),
        )
        monkeypatch.setattr(backend_commands.backend, "load_saved_courses", lambda: False)
        monkeypatch.setattr(backend_commands, "discover_browser_access", lambda port: expected if port == 9333 else None)
        assert backend_commands._resolve_ai_access(9333) is expected
