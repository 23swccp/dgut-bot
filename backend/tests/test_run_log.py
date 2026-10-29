"""运行日志只保留内存；手动导出、旧配置兼容与后台输出的离线回归。"""

import io
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from dgutbot import logs


def test_event_line_keeps_existing_compact_format_and_redaction():
    line = logs.format_event_line({
        "time": "2026-10-21T19:42:11+08:00", "level": "warning", "category": "sign",
        "code": "SIGN_POLL_FAILED", "message": "轮询出错", "data": {"round": 3},
    })
    assert line == "2026-10-21 19:42:11 · warning · sign · 轮询出错 · round=3\n"
    secret_line = logs.format_event_line({
        "message": "Authorization: secret-token / password=hunter2", "data": {"cookie": "SID=abc"},
    })
    assert all(secret not in secret_line for secret in ("secret-token", "hunter2", "abc"))
    assert "[已隐藏]" in secret_line


def test_old_config_cannot_enable_automatic_logging_or_move_old_files(tmp_path):
    from dgutbot.domain.yxy_backend import SignBackend

    legacy = tmp_path / "签到记录.md"
    existing = tmp_path / "data.md"
    legacy.write_text("旧签到记录", encoding="utf-8")
    existing.write_text("旧运行日志", encoding="utf-8")
    (tmp_path / "config.json").write_text(json.dumps({
        "save_log": True, "log_path": str(tmp_path / "custom" / "data.md"),
    }), encoding="utf-8")
    backend = SignBackend(Mock(), root=tmp_path)
    backend._log("普通运行事件", "info")
    backend.update_settings(save_log=True, log_path="new-log.md")
    assert "save_log" not in backend.config.to_mapping()
    assert "log_path" not in backend.config.to_mapping()
    assert legacy.read_text(encoding="utf-8") == "旧签到记录"
    assert existing.read_text(encoding="utf-8") == "旧运行日志"
    assert not (tmp_path / "custom").exists()
    assert not (tmp_path / "new-log.md").exists()


def test_open_log_requires_existing_explicit_file_without_creating_it(tmp_path):
    from dgutbot.domain.yxy_backend import SignBackend

    backend = SignBackend(Mock(), root=tmp_path)
    with pytest.raises(ValueError, match="请先"):
        backend.open_log_file()
    with pytest.raises(OSError, match="不存在"):
        backend.open_log_file("missing/data.md")
    assert not (tmp_path / "missing").exists()
    assert not (tmp_path / "data.md").exists()


def test_event_stream_is_bounded_splits_lines_and_flushes_partial_output():
    seen = []
    with logs.EventLogStream(seen.append) as stream:
        stream.MAX_LINE_CHARS = 32
        assert stream.write("第一") == 2
        stream.write("行\r\npassword=hunter2\n")
        stream.write("x" * 80)
        stream.flush()
        stream.write("退出前尾行")
    assert seen[:2] == ["第一行", "password=[已隐藏]"]
    assert "".join(seen[2:-1]) == "x" * 80
    assert all(len(line) <= 32 for line in seen)
    assert seen[-1] == "退出前尾行"


def test_background_diagnostics_enter_memory_without_opening_log_files():
    from dgutbot.app import browser_launcher, commands

    buffer = commands.EventBuffer()
    with patch.object(commands, "EVENT_BUFFER", buffer), patch.object(browser_launcher, "_run_background_service") as run:
        def output(*_):
            print("后台服务启动")
            import sys
            sys.stderr.write("模拟服务异常\n")
            return 0
        run.side_effect = output
        with patch.object(Path, "open", side_effect=AssertionError("不得打开日志文件")):
            assert browser_launcher.run_background_service(8765, True) == 0
            process = Mock(stdout=io.StringIO("Vite 输出\n最后半行"))
            browser_launcher.collect_frontend_output(process)
            with patch.object(browser_launcher, "service_command", return_value=["service"]), patch.object(browser_launcher.subprocess, "Popen") as popen, patch.object(browser_launcher.threading, "Thread"):
                popen.return_value.stdout = None
                browser_launcher.start_background_service(8765, True)
                assert popen.call_args.kwargs["stdout"] == browser_launcher.subprocess.PIPE
    events = buffer.get_events()["events"]
    assert [event["message"] for event in events] == ["后台服务启动", "模拟服务异常", "Vite 输出", "最后半行"]
    assert events[1]["level"] == "error"


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


def test_commands_stay_in_memory_skip_polls_and_keep_key_names_only(tmp_path, monkeypatch):
    from unittest.mock import patch

    from dgutbot.app import commands
    from dgutbot.domain.yxy_backend import SignBackend
    from unittest.mock import Mock

    backend = SignBackend(Mock(), root=tmp_path)
    buffer = commands.EventBuffer()
    monkeypatch.setattr(commands, "EVENT_BUFFER", buffer)
    with patch.object(commands, "backend", backend):
        assert commands.handle("clear_selected_course", {})["ok"]
        assert commands.handle("get_events", {})["ok"]
        commands.handle("update_account_login", {"username": "student", "password": "hunter2"})
    assert not (tmp_path / "data.md").exists()
    content = "\n".join(logs.format_event_line(event) for event in buffer.get_events()["events"])
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


def test_manual_export_includes_retained_events_and_creates_distinct_files(tmp_path, monkeypatch):
    from dgutbot.app import commands
    from dgutbot.domain.yxy_backend import SignBackend

    monkeypatch.setattr(commands, "backend", SignBackend(Mock(), root=tmp_path))
    buffer = commands.EventBuffer()
    monkeypatch.setattr(commands, "EVENT_BUFFER", buffer)
    for index in range(4100):
        commands.emit_event("TEST", "info", "app", f"事件-{index:04}", module="app")
    assert not list(tmp_path.rglob("*.md"))
    first = commands.handle("export_log_report", {})
    assert first["ok"] and first["report"]["count"] == 4000
    path = Path(first["report"]["path"])
    content = path.read_text(encoding="utf-8")
    assert "事件-0000" not in content
    assert "事件-0100" in content and "事件-4099" in content
    second = commands.handle("export_log_report", {})
    assert second["ok"] and second["report"]["path"] != str(path)
    assert path.read_text(encoding="utf-8") == content
    assert not (tmp_path / "data.md").exists()


def test_manual_export_failure_returns_original_error(tmp_path, monkeypatch):
    from dgutbot.app import commands
    from dgutbot.domain.yxy_backend import SignBackend

    monkeypatch.setattr(commands, "backend", SignBackend(Mock(), root=tmp_path))
    monkeypatch.setattr(commands, "EVENT_BUFFER", commands.EventBuffer())
    with patch.object(Path, "open", side_effect=OSError("disk full")):
        result = commands.handle("export_log_report", {})
    assert not result["ok"]
    assert "disk full" in result["error"]
    assert "report" not in result
