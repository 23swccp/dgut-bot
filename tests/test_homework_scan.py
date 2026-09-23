import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

from dgutbot.domain.homework_scan import (
    HomeworkAuthRequiredError,
    HomeworkScanService,
    peer_review_homeworks,
)
from dgutbot.domain.yxy_backend import Course, SignBackend


def wait_done(service):
    deadline = time.monotonic() + 2
    while service.snapshot()["state"] == "scanning" and time.monotonic() < deadline:
        time.sleep(.01)
    return service.snapshot()


def test_extracts_pending_and_active_peer_reviews_only():
    payload = {"homeworkList": [
        {"id": 10, "homeworkTitle": "普通未提交", "state": 0},
        {"id": 11, "homeworkTitle": "等待我互评", "state": 1, "endTime": 1_700_000_000_000},
        {"id": 12, "homeworkTitle": "正在互评", "state": "2"},
        {"id": 13, "homeworkTitle": "已批阅", "state": 4},
    ]}
    result = peer_review_homeworks(Course(42, "系统工程", "张老师"), payload)
    assert [item.homeworkId for item in result] == ["11", "12"]
    assert [item.stateLabel for item in result] == ["未互评", "互评中"]
    assert [item.needsAction for item in result] == [True, False]
    assert result[0].courseName == "系统工程" and result[0].teacherName == "张老师"
    assert "courseId=42" in result[0].url
    assert result[0].endTime.startswith("2023-")


def test_supports_nested_homework_list_and_stable_deduplication():
    item = {"homeworkId": "hw-1", "title": "课程论文", "state": 1}
    result = peer_review_homeworks(Course(1, "课"), {"result": {"homeworkList": [item, dict(item)]}})
    assert len(result) == 1


def test_scan_continues_after_one_course_failure_and_counts_pending():
    courses = [Course(1, "正常"), Course(2, "失败")]

    def fetch(course):
        if course.id == 2:
            raise RuntimeError("Authorization: secret")
        return {"homeworkList": [
            {"id": 1, "homeworkTitle": "待处理", "state": 1},
            {"id": 2, "homeworkTitle": "进行中", "state": 2},
        ]}

    service = HomeworkScanService(
        lambda: courses, fetch, lambda: True,
        lambda value: str(value).replace("secret", "[已隐藏]"),
    )
    service.start()
    result = wait_done(service)
    assert result["state"] == "completed"
    assert result["peerReviewCount"] == 2 and result["pendingReviewCount"] == 1
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
