"""data.md 运行日志的离线回归：格式、脱敏、轮转与开关。"""

import json
from pathlib import Path

from dgutbot import logs


def reset_header(monkeypatch):
    monkeypatch.setitem(logs._STATE, "header", False)


def test_event_line_keeps_time_level_category_and_extra_data(monkeypatch):
    reset_header(monkeypatch)
    line = logs.RunLogSink.line({
        "time": "2026-10-21T19:42:11+08:00", "level": "warning", "category": "sign",
        "code": "SIGN_POLL_FAILED", "message": "轮询出错：浏览器拒绝执行请求",
        "data": {"round": 3},
    })
    assert line == "2026-10-21 19:42:11 · warning · sign · 轮询出错：浏览器拒绝执行请求 · round=3\n"


def test_event_line_redacts_credentials(tmp_path, monkeypatch):
    reset_header(monkeypatch)
    path = tmp_path / "data.md"
    sink = logs.RunLogSink(lambda: (True, path))
    sink.on_event({"time": "", "level": "error", "category": "sign", "code": "X",
                   "message": "Authorization: secret-token / password=hunter2", "data": {"cookie": "SID=abc"}})
    content = path.read_text(encoding="utf-8")
    assert "secret-token" not in content
    assert "hunter2" not in content
    assert "abc" not in content
    assert "[已隐藏]" in content


def test_session_header_written_once_per_process(tmp_path, monkeypatch):
    reset_header(monkeypatch)
    path = tmp_path / "data.md"
    sink = logs.RunLogSink(lambda: (True, path))
    for _ in range(3):
        sink.on_event({"time": "", "level": "info", "category": "sign", "code": "C", "message": "第 1 轮", "data": {}})
    content = path.read_text(encoding="utf-8")
    assert content.count("## 运行开始") == 1
    assert content.count("第 1 轮") == 3


def test_disabled_log_switch_writes_nothing(tmp_path, monkeypatch):
    reset_header(monkeypatch)
    path = tmp_path / "data.md"
    logs.RunLogSink(lambda: (False, path)).on_event(
        {"time": "", "level": "info", "category": "sign", "code": "C", "message": "不应出现", "data": {}},
    )
    assert not path.exists()


def test_rotation_keeps_one_previous_file(tmp_path, monkeypatch):
    reset_header(monkeypatch)
    path = tmp_path / "data.md"
    path.write_text("x" * (logs.MAX_BYTES + 1), encoding="utf-8")
    logs.append_block(path, ["新一行\n"])
    assert (tmp_path / "data.1.md").is_file()
    assert path.read_text(encoding="utf-8") == "新一行\n"


def test_missing_log_directory_is_created(tmp_path, monkeypatch):
    reset_header(monkeypatch)
    path = tmp_path / "logs" / "deep" / "data.md"
    logs.append_block(path, ["一行\n"])
    assert path.read_text(encoding="utf-8") == "一行\n"


def test_default_log_name_is_data_md(tmp_path):
    from dgutbot.domain.yxy_backend import AppConfig

    assert AppConfig.from_mapping({}, tmp_path).log_path == str(tmp_path / "data.md")


def test_legacy_log_name_migrates_but_custom_paths_are_kept(tmp_path):
    from dgutbot.domain.yxy_backend import AppConfig

    legacy = AppConfig.from_mapping({"log_path": str(tmp_path / "签到记录.md")}, tmp_path)
    assert legacy.log_path == str(tmp_path / "data.md")
    custom = AppConfig.from_mapping({"log_path": str(tmp_path / "my-log.md")}, tmp_path)
    assert custom.log_path == str(tmp_path / "my-log.md")


def test_legacy_log_file_is_renamed_to_data_md(tmp_path):
    from unittest.mock import Mock

    from dgutbot.domain.yxy_backend import SignBackend

    legacy = tmp_path / "签到记录.md"
    legacy.write_text("2026-10-01-10:00 | 旧课程 | 一键签到 |\n", encoding="utf-8")
    backend = SignBackend(Mock(), root=tmp_path)
    backend.update_settings(log_path=str(tmp_path / "data.md"))
    backend._write_sign_log("新课程", "一键签到", ["HTTP/status: 200"])
    content = (tmp_path / "data.md").read_text(encoding="utf-8")
    assert "旧课程" in content and "新课程" in content
    assert not legacy.exists()


def test_legacy_log_migration_keeps_an_existing_data_md(tmp_path):
    from unittest.mock import Mock

    from dgutbot.domain.yxy_backend import SignBackend

    (tmp_path / "签到记录.md").write_text("旧内容", encoding="utf-8")
    (tmp_path / "data.md").write_text("新内容", encoding="utf-8")
    SignBackend(Mock(), root=tmp_path)
    assert (tmp_path / "data.md").read_text(encoding="utf-8") == "新内容"
    assert (tmp_path / "签到记录.md").is_file()


def test_events_are_tagged_with_their_owning_module(tmp_path):
    """签到轮次归签到，登录/浏览器/设置归程序——由发出方声明，不靠猜。"""
    from dgutbot.domain.yxy_backend import SignBackend

    backend = SignBackend(lambda *_: None, root=tmp_path)
    captured: list[str] = []
    backend.emit = lambda text, kind, module="": captured.append(module)
    backend.sign_monitor._log("第 1 轮：正在检查《示例课程》…", "info")
    assert captured == ["sign"]
    backend._log("账号密码重新登录失败：未收到登录凭据。", "warn")
    assert captured[-1] == "app"


def test_emit_keeps_the_declared_module_on_the_event(tmp_path, monkeypatch):
    from dgutbot.app import commands

    buffer = commands.EventBuffer()
    monkeypatch.setattr(commands, "EVENT_BUFFER", buffer)
    commands.emit("第 1 轮：正在检查《示例》…", "info", "sign")
    commands.emit("本次登录会话已更新。", "success")
    commands.emit_event("UPDATE_HANDOFF", "success", "update", "正在退出应用")
    assert [commands.log_module(event) for event in buffer.get_events()["events"]] == ["sign", "app", "update"]


def test_log_page_filters_by_module_level_and_keyword():
    from dgutbot.app.commands import EventBuffer

    buffer = EventBuffer(maxlen=50)
    buffer.emit_event("LEGACY_LOG", "info", "general", "第 1 轮：正在检查《示例》")
    buffer.emit_event("DEBUG_LOG", "info", "debug", "[刷课] 播放视频")
    buffer.emit_event("COMMAND", "warning", "command", "start_monitor · 失败 · 3ms")
    # 未声明归属的通用事件按“程序”归类，不被静默算成签到。
    assert [event["module"] for event in buffer.log_page()["events"]] == ["app", "course", "app"]
    assert [event["message"] for event in buffer.log_page(module="course")["events"]] == ["[刷课] 播放视频"]
    assert [event["code"] for event in buffer.log_page(level="warning")["events"]] == ["COMMAND"]
    assert [event["code"] for event in buffer.log_page(query="播放")["events"]] == ["DEBUG_LOG"]


def test_log_page_after_seq_returns_only_the_increment():
    from dgutbot.app.commands import EventBuffer

    buffer = EventBuffer()
    for index in range(5):
        buffer.emit_event("LEGACY_LOG", "info", "general", f"第 {index} 步")
    page = buffer.log_page(limit=3)
    assert [event["message"] for event in page["events"]] == ["第 2 步", "第 3 步", "第 4 步"]
    assert page["hasMore"] and page["bufferSize"] == 5
    assert [event["message"] for event in buffer.log_page(after_seq=3, limit=10)["events"]] == ["第 3 步", "第 4 步"]


def test_export_log_report_writes_only_the_filtered_module(tmp_path, monkeypatch):
    from unittest.mock import Mock, patch

    from dgutbot.app import commands
    from dgutbot.domain.yxy_backend import SignBackend

    backend = SignBackend(Mock(), root=tmp_path)
    backend.update_settings(log_path=str(tmp_path / "data.md"))
    buffer = commands.EventBuffer()
    monkeypatch.setattr(commands, "EVENT_BUFFER", buffer)
    buffer.emit_event("LEGACY_LOG", "info", "general", "签到第 1 轮")
    buffer.emit_event("DEBUG_LOG", "info", "debug", "[刷课] 播放视频")
    with patch.object(commands, "backend", backend):
        result = commands.handle("export_log_report", {"module": "course"})
    assert result["ok"] and result["report"]["count"] == 1
    content = Path(result["report"]["path"]).read_text(encoding="utf-8")
    assert "刷课" in content and "播放视频" in content
    assert "签到第 1 轮" not in content
    assert Path(result["report"]["path"]).name.startswith("日志-")
    assert Path(result["report"]["path"]).parent.name == "日志导出"


def test_command_recording_skips_polls_and_keeps_key_names_only(tmp_path, monkeypatch):
    from unittest.mock import patch

    from dgutbot.app import commands
    from dgutbot.domain.yxy_backend import SignBackend
    from unittest.mock import Mock

    backend = SignBackend(Mock(), root=tmp_path)
    backend.update_settings(log_path=str(tmp_path / "data.md"), save_log=True)
    monkeypatch.setitem(logs._STATE, "header", False)
    with patch.object(commands, "backend", backend):
        assert commands.handle("clear_selected_course", {})["ok"]
        assert commands.handle("get_events", {})["ok"]
        commands.handle("update_account_login", {"username": "student", "password": "hunter2"})
    content = (tmp_path / "data.md").read_text(encoding="utf-8")
    assert "· command · clear_selected_course · 成功" in content
    assert "get_events" not in content
    assert "hunter2" not in content
    assert "payload: password,username" in content


def test_courseware_commands_and_results_have_a_separate_log_module(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from dgutbot.app import commands
    from dgutbot.domain.yxy_backend import SignBackend

    backend = SignBackend(commands.emit, root=tmp_path)
    buffer = commands.EventBuffer()
    monkeypatch.setattr(commands, "EVENT_BUFFER", buffer)
    monkeypatch.setattr(commands, "backend", backend)
    assert commands.handle("get_courseware_export_status", {})["ok"]
    assert buffer.get_events()["events"] == []
    backend.start_courseware_exports = Mock(side_effect=ValueError("课程不在当前列表中，请先刷新课程"))
    result = commands.handle("start_courseware_export", {"courseIds": ["101"]})
    assert not result["ok"] and result["error"] == "课程不在当前列表中，请先刷新课程"
    backend.courseware_export._log("课件导出已取消，已保存内容保留", "warn")
    events = buffer.log_page(module="courseware")["events"]
    assert len(events) == 2
    assert all(event["module"] == "courseware" for event in events)
    assert {event["level"] for event in events} == {"warning"}
