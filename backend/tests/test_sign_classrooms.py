from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from dgutbot.domain.sign_monitor import Classroom
from dgutbot.domain.yxy_backend import Course, SignBackend
from dgutbot.app import commands


@pytest.fixture
def backend(tmp_path):
    value = SignBackend(Mock(), root=tmp_path)
    value.user_id = 7
    value.selected_course = Course(42, "示例课程")
    return value


def rooms():
    return [Classroom.from_api({"id": 11, "title": "旧课堂", "status": 0,
                               "beginTime": int((datetime.now() - timedelta(days=7)).timestamp() * 1000)}),
            Classroom.from_api({"id": 22, "title": "新课堂", "status": 2})]


def test_multiple_classrooms_require_explicit_selection_and_poll_only_selected(backend):
    monitor = backend.sign_monitor
    with patch.object(monitor, "classrooms", return_value=rooms()), patch.object(monitor, "activities", return_value=[]) as activities:
        status = monitor.prepare_classrooms()
        assert status["classroomId"] is None and status["classroomSelectionRequired"]
        assert [item["id"] for item in status["classrooms"]] == [11, 22]
        assert status["classrooms"][0]["beginTime"]
        assert status["classrooms"][0]["statusLabel"] == "进行中"
        assert status["classrooms"][1]["statusLabel"] == "已结束"
        assert monitor.poll_once(set()) == "请选择课堂后开始监测"
        activities.assert_not_called()
        monitor.select_classroom(11)
        monitor.stop_event.clear()
        assert monitor.poll_once(set()) == "未发现进行中的签到"
        activities.assert_called_once_with(11)


def test_single_old_classroom_is_auto_selected_without_date_filter(backend):
    monitor = backend.sign_monitor
    with patch.object(monitor, "classrooms", return_value=rooms()[:1]), patch.object(monitor, "activities", return_value=[]) as activities:
        assert monitor.prepare_classrooms()["classroomId"] == 11
        monitor.poll_once(set())
        activities.assert_called_once_with(11)


@pytest.mark.parametrize("switch_account", [False, True])
def test_selection_is_cleared_when_course_or_account_changes(backend, switch_account):
    monitor = backend.sign_monitor
    with patch.object(monitor, "classrooms", return_value=rooms()):
        monitor.prepare_classrooms()
        monitor.select_classroom(11)
    if switch_account:
        backend.user_id = 8
    else:
        backend.selected_course = Course(43, "另一课程")
    assert monitor.status()["classroomId"] is None
    assert monitor.status()["classrooms"] == []
    with pytest.raises(ValueError, match="失效"):
        monitor.select_classroom(11)


def test_late_classroom_response_cannot_populate_another_account(backend):
    def switch(_course_id):
        backend.user_id = 8
        return rooms()
    with patch.object(backend.sign_monitor, "classrooms", side_effect=switch):
        with pytest.raises(RuntimeError, match="变化"):
            backend.sign_monitor.prepare_classrooms()
    assert backend.sign_monitor.status()["classrooms"] == []


def test_missing_selected_room_stops_instead_of_switching_to_other_room(backend):
    monitor = backend.sign_monitor
    with patch.object(monitor, "classrooms", return_value=rooms()):
        monitor.prepare_classrooms()
        monitor.select_classroom(11)
    with patch.object(monitor, "classrooms", return_value=rooms()[1:]), patch.object(monitor, "activities") as activities:
        assert monitor.poll_once(set()) == "所选课堂不可用，请重新选择"
    assert monitor.stop_event.is_set() and monitor.status()["classroomSelectionRequired"]
    activities.assert_not_called()


def test_empty_list_can_be_refreshed_and_failed_response_is_not_empty(backend):
    monitor = backend.sign_monitor
    with patch.object(monitor, "classrooms", side_effect=[[], rooms()[:1]]):
        assert monitor.prepare_classrooms()["classrooms"] == []
        assert monitor.prepare_classrooms()["classroomId"] == 11
    with patch.object(monitor, "_request", return_value=Mock(json=lambda: {"code": 0, "msg": "failure"})):
        with pytest.raises(RuntimeError, match="读取失败"):
            monitor.prepare_classrooms()


def test_classroom_list_reads_all_pages_and_deduplicates(backend):
    request = Mock(side_effect=[Mock(json=lambda: {"code": 1, "result": {
        "total": 101, "list": [{"id": i, "title": f"课堂{i}"} for i in range(100)]}}),
        Mock(json=lambda: {"code": 1, "result": {"total": 101, "list": [{"id": 100}]}})])
    with patch.object(backend.sign_monitor, "_request", request):
        found = backend.sign_monitor.classrooms(42)
    assert len(found) == 101 and found[-1].id == 100
    assert [call.kwargs["params"]["pageNum"] for call in request.call_args_list] == [1, 2]


def test_classroom_pagination_failure_does_not_return_a_partial_list(backend):
    request = Mock(return_value=Mock(json=lambda: {"code": 1, "result": {"total": 101, "list": [{"id": 11}]}}))
    with patch.object(backend.sign_monitor, "_request", request):
        with pytest.raises(RuntimeError, match="未前进"):
            backend.sign_monitor.classrooms(42)
    assert request.call_count == 2


def test_selection_commands_return_only_display_fields_and_validate_ids(backend):
    with patch.object(commands, "backend", backend), patch.object(backend.sign_monitor, "classrooms", return_value=rooms()):
        result = commands.handle("get_sign_classrooms", {})
        assert result["ok"]
        assert set(result["signStatus"]["classrooms"][0]) == {"id", "title", "beginTime", "statusLabel", "ended"}
        assert result["signStatus"]["classrooms"][1]["ended"] is True
        assert not commands.handle("select_sign_classroom", {"classroomId": "999"})["ok"]
        assert commands.handle("select_sign_classroom", {"classroomId": 11})["signStatus"]["classroomId"] == 11


def test_ended_classroom_cannot_be_selected(backend):
    monitor = backend.sign_monitor
    with patch.object(monitor, "classrooms", return_value=rooms()):
        monitor.prepare_classrooms()
        with pytest.raises(ValueError, match="已结束"):
            monitor.select_classroom(22)
    assert monitor.status()["classroomId"] is None
    with patch.object(commands, "backend", backend):
        rejected = commands.handle("select_sign_classroom", {"classroomId": 22})
    assert rejected["ok"] is False and "已结束" in rejected["error"]


def test_single_ended_classroom_is_not_auto_selected(backend):
    monitor = backend.sign_monitor
    with patch.object(monitor, "classrooms", return_value=rooms()[1:]), patch.object(monitor, "activities") as activities:
        status = monitor.prepare_classrooms()
        assert status["classroomId"] is None and status["classroomSelectionRequired"]
        assert monitor.poll_once(set()) == "请选择课堂后开始监测"
        activities.assert_not_called()


def test_open_classroom_with_old_begin_date_is_still_selectable(backend):
    """Issue #3 的情况：课堂一直不关，beginTime 是旧日期但 status=0，必须仍可选。"""
    old_open = Classroom.from_api({"id": 55, "title": "马原课堂", "status": 0, "beginTime": 1_600_000_000_000})
    monitor = backend.sign_monitor
    with patch.object(monitor, "classrooms", return_value=[old_open]), patch.object(monitor, "activities", return_value=[]) as activities:
        assert monitor.prepare_classrooms()["classroomId"] == 55
        monitor.poll_once(set())
        activities.assert_called_once_with(55)


def test_cannot_change_classroom_or_course_while_monitor_is_running(backend):
    monitor = backend.sign_monitor
    with patch.object(monitor, "classrooms", return_value=rooms()):
        monitor.prepare_classrooms()
        monitor.select_classroom(11)
    monitor.thread = Mock(is_alive=lambda: True)
    with pytest.raises(RuntimeError, match="停止监测"):
        monitor.select_classroom(22)
    assert backend.select_course_id("43") is None
    backend.clear_selected_course()
    assert backend.selected_course.id == 42
