import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

from dgutbot.domain.homework_scan import (
    HomeworkAuthRequiredError,
    HomeworkScanService,
    homework_items,
    homework_state_label,
    sort_homework_items,
)
from dgutbot.domain.yxy_backend import Course, SignBackend


def wait_done(service):
    deadline = time.monotonic() + 2
    while service.snapshot()["state"] == "scanning" and time.monotonic() < deadline:
        time.sleep(.01)
    return service.snapshot()


def test_extracts_all_homework_states_with_pending_and_peer_flags():
    payload = {"homeworkList": [
        {"id": 10, "homeworkTitle": "普通未提交", "state": 0, "endTime": "2026-01-01T10:00:00+08:00"},
        {"id": 11, "homeworkTitle": "等待我互评", "state": 1, "endTime": 1_700_000_000_000},
        {"id": 12, "homeworkTitle": "正在互评", "state": "2"},
        {"id": 13, "homeworkTitle": "已批阅", "state": 4},
        {"id": 14, "homeworkTitle": "未知状态", "state": 3},
        {"id": 15, "homeworkTitle": "缺状态"},
    ]}
    result = homework_items(Course(42, "系统工程", "张老师"), payload)
    assert [item.homeworkId for item in result] == ["10", "11", "12", "13", "14", "15"]
    assert [item.stateLabel for item in result] == ["未提交", "未互评", "互评中", "已批阅", "其它状态（3）", "未知状态"]
    assert [item.pending for item in result] == [True, True, False, False, False, False]
    assert [item.needsAction for item in result] == [False, True, False, False, False, False]
    assert result[0].courseName == "系统工程" and result[0].teacherName == "张老师"
    assert "courseId=42" in result[0].url
    assert result[0].endTime.startswith("2026-01-01")
    assert result[1].endTime.startswith("2023-")


def test_state_label_falls_back_to_raw_value_and_unknown():
    assert homework_state_label(0) == "未提交"
    assert homework_state_label(9) == "其它状态（9）"
    assert homework_state_label(None) == "未知状态"


def test_supports_nested_homework_list_and_stable_deduplication():
    item = {"homeworkId": "hw-1", "title": "课程论文", "state": 0}
    result = homework_items(Course(1, "课"), {"result": {"homeworkList": [item, dict(item)]}})
    assert len(result) == 1


def test_sorts_pending_first_then_deadline_and_missing_deadline_last():
    payload = {"homeworkList": [
        {"id": 1, "homeworkTitle": "已批阅早截止", "state": 4, "endTime": "2026-01-01T00:00:00+08:00"},
        {"id": 2, "homeworkTitle": "待互评晚截止", "state": 1, "endTime": "2026-01-03T00:00:00+08:00"},
        {"id": 3, "homeworkTitle": "未提交早截止", "state": 0, "endTime": "2026-01-02T00:00:00+08:00"},
        {"id": 4, "homeworkTitle": "未知无截止", "state": 3},
    ]}
    ordered = sort_homework_items(homework_items(Course(1, "课"), payload))
    assert [item.homeworkId for item in ordered] == ["3", "2", "1", "4"]


def test_archived_items_are_hidden_from_counts_and_course_archive_refreshes():
    payload = {"homeworkList": [
        {"id": 1, "homeworkTitle": "未提交", "state": 0},
        {"id": 2, "homeworkTitle": "待互评", "state": 1},
        {"id": 3, "homeworkTitle": "已批阅", "state": 4},
    ]}
    parsed = homework_items(Course(1, "课"), payload)
    archived_item = parsed[1].id
    archive_sets = [(set(), {archived_item})]
    service = HomeworkScanService(
        lambda: [Course(1, "课")], lambda _course: payload, lambda: True, str,
        archives=lambda: archive_sets[0],
    )
    service.start()
    result = wait_done(service)
    assert result["totalCount"] == 2 and result["archivedCount"] == 1
    assert result["pendingCount"] == 1 and result["pendingReviewCount"] == 0
    assert result["peerReviewCount"] == 0 and result["completedCount"] == 1
    assert result["groups"][0]["courseArchived"] is False
    archived_by_id = {item["homeworkId"]: item["archived"] for item in result["groups"][0]["items"]}
    assert archived_by_id == {"1": False, "2": True, "3": False}

    archive_sets[0] = ({"1"}, set())
    refreshed = service.refresh_archive()
    assert refreshed["totalCount"] == 0 and refreshed["archivedCount"] == 3
    assert refreshed["groups"][0]["courseArchived"] is True
    assert all(item["archived"] for item in refreshed["groups"][0]["items"])

    archive_sets[0] = (set(), set())
    restored = service.refresh_archive()
    assert restored["totalCount"] == 3 and restored["archivedCount"] == 0
    assert restored["groups"][0]["courseArchived"] is False
    assert not any(item["archived"] for item in restored["groups"][0]["items"])


def test_scan_continues_after_one_course_failure_and_counts_all_states():
    courses = [Course(1, "正常"), Course(2, "失败")]

    def fetch(course):
        if course.id == 2:
            raise RuntimeError("Authorization: secret")
        return {"homeworkList": [
            {"id": 1, "homeworkTitle": "待处理", "state": 1},
            {"id": 2, "homeworkTitle": "进行中", "state": 2},
            {"id": 3, "homeworkTitle": "未提交", "state": 0},
            {"id": 4, "homeworkTitle": "已批阅", "state": 4},
        ]}

    service = HomeworkScanService(
        lambda: courses, fetch, lambda: True,
        lambda value: str(value).replace("secret", "[已隐藏]"),
    )
    service.start()
    result = wait_done(service)
    assert result["state"] == "completed"
    assert result["totalCount"] == 4
    assert result["pendingCount"] == 2
    assert result["peerReviewCount"] == 2
    assert result["pendingReviewCount"] == 1
    assert result["completedCount"] == 1
    assert result["failures"][0]["reason"] == "Authorization: [已隐藏]"


def test_scan_reports_auth_failure_and_uses_cache():
    failing = HomeworkScanService(
        lambda: [Course(1, "课")],
        lambda _course: (_ for _ in ()).throw(HomeworkAuthRequiredError("登录已失效")),
        lambda: True, str,
    )
    failing.start()
    assert wait_done(failing)["state"] == "auth_required"

    calls = []
    cached = HomeworkScanService(
        lambda: [Course(1, "课")], lambda _course: calls.append(1) or {"homeworkList": []},
        lambda: True, str,
    )
    cached.start()
    wait_done(cached)
    assert cached.start()["fromCache"] and calls == [1]


def test_backend_uses_verified_homework_endpoint_and_closes_worker_client():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        response = Mock()
        response.json.return_value = {"homeworkList": []}
        client = Mock()
        client.request.return_value = response
        with patch("dgutbot.domain.yxy_backend.BrowserApiClient", return_value=client):
            assert backend._scan_course_homeworks(Course(42, "课程")) == {"homeworkList": []}
        call = client.request.call_args
        assert call.args[0] == "GET" and call.args[1].endswith("/homeworks/student/v2")
        assert call.kwargs["params"] == {"ocId": 42, "pn": 1, "ps": 999, "lang": "zh"}
        client.close.assert_called_once()
