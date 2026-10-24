import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

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


def test_cache_is_reused_only_for_the_same_user_and_cleared_on_logout():
    owner = [7]
    courses = [Course(42, "A-course")]
    fetch = Mock(return_value={"homeworkList": [{"id": 99, "state": 1}]})
    service = HomeworkScanService(lambda: courses, fetch, lambda: True, str, user_id=lambda: owner[0])
    service.start()
    first = wait_done(service)
    old_id = first["groups"][0]["items"][0]["id"]
    assert service.start()["fromCache"]
    assert fetch.call_count == 1

    owner[0] = 8
    courses[:] = [Course(88, "B-course")]
    assert service.snapshot()["groups"] == []
    assert service.item(old_id) is None
    service.start()
    second = wait_done(service)
    assert not second["fromCache"]
    assert second["groups"][0]["courseName"] == "B-course"
    assert fetch.call_count == 2

    owner[0] = None
    assert service.snapshot()["groups"] == []
    assert service.start()["state"] == "auth_required"
    assert fetch.call_count == 2


def test_unknown_user_cannot_scan_or_reuse_anonymous_cache():
    fetch = Mock()
    service = HomeworkScanService(lambda: [Course(42, "课")], fetch, lambda: True, str, user_id=lambda: None)
    assert service.start()["state"] == "auth_required"
    fetch.assert_not_called()


def test_invalid_login_cannot_reuse_a_completed_cache():
    authenticated = [True]
    service = HomeworkScanService(lambda: [Course(42, "课")], lambda _: {"homeworkList": [{"id": 99, "state": 1}]},
                                  lambda: authenticated[0], str, user_id=lambda: 7)
    service.start()
    assert wait_done(service)["groups"]
    authenticated[0] = False
    result = service.start()
    assert result["state"] == "auth_required" and not result["groups"]


@pytest.mark.parametrize("old_login_rejected", [False, True])
def test_late_scan_from_old_account_cannot_overwrite_or_cancel_new_account(old_login_rejected):
    owner = [7]
    courses = [Course(42, "A-course")]
    entered = threading.Event()
    release = threading.Event()

    def fetch(course):
        if course.id == 42:
            entered.set()
            assert release.wait(3)
            if old_login_rejected:
                raise HomeworkAuthRequiredError("old account rejected")
        return {"homeworkList": [{"id": 99, "state": 1}]}

    service = HomeworkScanService(lambda: courses, fetch, lambda: True, str, user_id=lambda: owner[0])
    service.start()
    old_thread = service._thread
    try:
        assert entered.wait(2)
        owner[0] = 8
        courses[:] = [Course(88, "B-course")]
        service.start()
        current = wait_done(service)
        assert current["state"] == "completed"
        assert current["groups"][0]["courseName"] == "B-course"
    finally:
        release.set()
        old_thread.join(3)
    assert not old_thread.is_alive()
    assert service.snapshot() == current


def test_browser_account_switch_invalidates_scan_and_rejects_old_archive_target():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        backend.user_id = 7
        backend.token = "fixture-A"
        backend.courses = [Course(42, "A-course")]
        with patch.object(backend.homework_scan, "_homeworks", return_value={"homeworkList": [{"id": 99, "state": 1}]}):
            backend.start_homework_scan()
            old = wait_done(backend.homework_scan)
        old_item_id = old["groups"][0]["items"][0]["id"]
        cookies = [{"name": "AUTHORIZATION", "value": "fixture-B", "domain": "lms.dgut.edu.cn"}]
        with (
            patch.object(backend, "_get_ws_url", return_value="ws://fixture"),
            patch.object(backend, "_cookies", return_value=cookies),
            patch.object(backend, "_fetch_courses", return_value=[Course(88, "B-course")]),
            patch.object(backend, "_browser_identity", return_value=(8, "", "")),
            patch.object(backend, "resume_course_recovery"),
        ):
            assert backend.load_session_and_courses(wait_seconds=1)
        assert backend.homework_scan_status()["groups"] == []
        for kind, target in (("course", "42"), ("item", old_item_id)):
            with pytest.raises(ValueError, match="已失效"):
                backend.set_homework_archive(kind, target, True)
        assert backend.homework_archive.sets(8) == (set(), set())
        with patch.object(backend.homework_scan, "_homeworks", return_value={"homeworkList": [{"id": 100, "state": 0}]}):
            backend.start_homework_scan()
            fresh = wait_done(backend.homework_scan)
        assert not fresh["fromCache"]
        assert fresh["groups"][0]["courseName"] == "B-course"
        backend.set_homework_archive("course", "88", True)
        assert backend.homework_archive.sets(8) == ({"88"}, set())
        assert backend.homework_archive.sets(7) == (set(), set())
