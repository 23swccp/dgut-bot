import threading
import time
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from dgutbot.domain.course_scan import AuthRequiredError, CourseScanService, completion_state, flatten_lessons
from dgutbot.domain.yxy_backend import Course
from dgutbot.domain.yxy_backend import SignBackend


URL = "https://ua.dgut.edu.cn/learnCourse?courseId=1&pageId=p1"


def lesson(**values):
    return {"pageId": "p1", "name": "导论", "contentType": "video", "url": URL, **values}


def wait_done(service):
    deadline = time.monotonic() + 2
    while service.snapshot()["state"] == "scanning" and time.monotonic() < deadline:
        time.sleep(.01)
    return service.snapshot()


def test_flattens_deep_tree_and_keeps_path_and_real_url():
    payload = {"chapters": [{"name": "第一章", "sections": [{"title": "第一节", "pages": [lesson()]}]}]}
    result = flatten_lessons(Course(1, "系统工程", "教师"), payload)
    assert len(result) == 1
    assert result[0].chapterPath == ["第一章", "第一节"]
    assert result[0].url == URL and result[0].canAutoOpen


def test_completion_recognizes_completed_partial_and_unstarted():
    assert completion_state({"completed": True}) == ("completed", 100.0)
    assert completion_state({"progress": 0.4}) == ("in_progress", 40.0)
    assert completion_state({}) == ("not_started", None)


def test_filters_completed_hidden_and_unopened_but_keeps_partial():
    payload = {"pages": [lesson(pageId="done", completed=True), lesson(pageId="hidden", hidden=True),
                         lesson(pageId="closed", isOpen=False), lesson(pageId="partial", progress="25%") ]}
    result = flatten_lessons(Course(1, "课"), payload)
    assert [item.pageId for item in result] == ["partial"]
    assert result[0].completionPercent == 25


def test_missing_fields_degrades_to_non_openable_and_duplicates_are_stable():
    raw = {"pageId": "p", "name": "无地址", "contentType": "document"}
    result = flatten_lessons(Course(1, "课"), {"pages": [raw, dict(raw)]})
    assert len(result) == 1
    assert not result[0].canAutoOpen
    assert "真实课件地址" in result[0].unavailableReason


def test_unverified_or_constructed_urls_are_rejected():
    result = flatten_lessons(Course(1, "课"), {"pages": [lesson(url="https://example.invalid/learnCourse")]})
    assert len(result) == 1 and not result[0].url and not result[0].canAutoOpen


def test_one_course_failure_continues_and_error_is_redacted():
    courses = [Course(1, "正常"), Course(2, "失败")]
    def directory(course):
        if course.id == 2:
            raise RuntimeError("Authorization: secret")
        return {"pages": [lesson()]}
    service = CourseScanService(lambda: courses, directory, lambda: True, lambda value: str(value).replace("secret", "[已隐藏]"))
    service.start()
    result = wait_done(service)
    assert result["state"] == "completed" and result["unfinishedCount"] == 1
    assert result["failures"][0]["reason"] == "Authorization: [已隐藏]"


def test_auth_failure_stops_with_explicit_state():
    service = CourseScanService(lambda: [Course(1, "课")], lambda _course: (_ for _ in ()).throw(AuthRequiredError("登录已失效")), lambda: True, str)
    service.start()
    assert wait_done(service)["state"] == "auth_required"


def test_cache_and_force_refresh():
    calls = []
    service = CourseScanService(lambda: [Course(1, "课")], lambda _course: calls.append(1) or {"pages": [lesson()]}, lambda: True, str)
    wait_done_after = lambda force=False: (service.start(force=force), wait_done(service))[1]
    wait_done_after()
    assert wait_done_after()["fromCache"] and len(calls) == 1
    wait_done_after(True)
    assert len(calls) == 2


def test_cancel_scan_does_not_publish_late_success():
    release = threading.Event()
    service = CourseScanService(lambda: [Course(1, "课")], lambda _course: release.wait(1) or {}, lambda: True, str)
    service.start()
    assert service.cancel()["state"] == "cancelled"
    release.set()
    time.sleep(.05)
    assert service.snapshot()["state"] == "cancelled"


def test_missing_login_does_not_start_worker():
    service = CourseScanService(lambda: [], lambda _course: {}, lambda: False, str)
    assert service.start()["state"] == "auth_required"


def test_verified_lms_directory_endpoints_build_the_observed_ua_url():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        responses = []
        for value in (
            [{"courseId": 900, "name": "教材"}],
            {"classId": 300},
            {"textbook": {"courseId": 900}, "list": [
                {"currentUnit": "完成", "currentUnitID": 10, "nodeID": 100, "progress": 1.0, "hide": 0, "planState": {"studyState": 1}},
                {"currentUnit": "待学习", "currentUnitID": 11, "nodeID": 101, "progress": 0.25, "hide": 0, "planState": {"studyState": 1}},
                {"currentUnit": "未开放", "currentUnitID": 12, "nodeID": 102, "progress": 0, "hide": 2, "planState": {"studyState": 3}},
            ]},
        ):
            response = Mock()
            response.json.return_value = value
            responses.append(response)
        backend.api.request = Mock(side_effect=responses)
        payload = backend._course_directory_payload(Course(42, "课程", "教师"))
        result = flatten_lessons(Course(42, "课程", "教师"), payload)
        assert len(result) == 1
        assert result[0].courseId == "900" and result[0].ocId == "42"
        assert result[0].classId == "300" and result[0].chapterId == "11"
        assert result[0].completionPercent == 25
        assert "courseId=900" in result[0].url and "chapterId=11" in result[0].url and "classId=300" in result[0].url
        calls = backend.api.request.call_args_list
        assert calls[0].args[1].endswith("/textbook/student/42/list")
        assert calls[1].args[1].endswith("/classes/information/student/42")
        assert calls[2].kwargs["params"]["currentPlatformType"] == 1


def test_directory_abort_is_retried_once_with_shorter_timeout():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        textbook = Mock()
        textbook.json.return_value = []
        class_info = Mock()
        class_info.json.return_value = {"classId": 300}
        backend.api.request = Mock(side_effect=[RuntimeError("浏览器请求失败：AbortError"), textbook, class_info])

        assert backend._course_directory_payload(Course(42, "课程")) == {"pages": []}
        assert backend.api.request.call_args_list[0].kwargs["timeout"] == 12
        assert backend.api.request.call_args_list[1].kwargs["timeout"] == 8


def backend_with_scanned_item():
    temporary = tempfile.TemporaryDirectory()
    backend = SignBackend(lambda *_: None, root=Path(temporary.name))
    item = flatten_lessons(Course(1, "课"), {"pages": [lesson()]})[0].mapping()
    backend.course_scan._snapshot.update(groups=[{"courseId": "1", "courseName": "课", "items": [item]}], unfinishedCount=1)
    controller = Mock()
    controller._running = False
    controller.preferred_ws_url = None
    controller.preferred_page_id = ""
    backend._course_controller = controller
    return temporary, backend, item, controller


def test_auto_open_targets_selected_page_and_starts_once():
    temporary, backend, item, controller = backend_with_scanned_item()
    try:
        starts = []
        with patch.object(backend, "_navigate_course_target", return_value="ws://selected") as navigate:
            assert backend.auto_open_course(item["id"], lambda: starts.append(1) or True)
        navigate.assert_called_once()
        assert starts == [1]
        assert controller.preferred_ws_url == "ws://selected"
        assert controller.preferred_page_id == "p1"
        assert backend.course_scan_status()["openPhase"] == "started"
    finally:
        temporary.cleanup()


def test_existing_task_is_never_silently_replaced():
    temporary, backend, item, controller = backend_with_scanned_item()
    try:
        controller._running = True
        with patch.object(backend, "_navigate_course_target") as navigate:
            assert not backend.auto_open_course(item["id"], lambda: True)
        navigate.assert_not_called()
        assert "已有刷课任务" in backend.course_scan_status()["openError"]
    finally:
        temporary.cleanup()


def test_repeated_open_callback_cannot_start_twice_after_controller_runs():
    temporary, backend, item, controller = backend_with_scanned_item()
    try:
        calls = []
        def start():
            calls.append(1)
            controller._running = True
            return True
        with patch.object(backend, "_navigate_course_target", return_value="ws://selected"):
            assert backend.auto_open_course(item["id"], start)
            assert not backend.auto_open_course(item["id"], start)
        assert calls == [1]
    finally:
        temporary.cleanup()


def test_page_mismatch_never_starts_controller():
    temporary, backend, item, _controller = backend_with_scanned_item()
    class FakeWs:
        def __init__(self): self.last = {}
        def settimeout(self, _timeout): pass
        def send(self, raw):
            import json
            self.last = json.loads(raw)
        def recv(self):
            import json
            value = {"ready": True, "url": "https://ua.dgut.edu.cn/learnCourse?courseId=999&pageId=wrong", "courseId": "999", "pageId": "wrong"}
            result = {"result": {"result": {"value": value}}} if self.last["method"] == "Runtime.evaluate" else {"result": {}}
            return json.dumps({"id": self.last["id"], **result})
        def close(self): pass
    try:
        backend.api._list_targets = Mock(return_value=[{"type": "page", "url": URL, "webSocketDebuggerUrl": "ws://selected"}])
        with patch("dgutbot.domain.yxy_backend.create_connection", return_value=FakeWs()):
            assert not backend.auto_open_course(item["id"], lambda: (_ for _ in ()).throw(AssertionError("must not start")))
        assert "不匹配" in backend.course_scan_status()["openError"]
    finally:
        temporary.cleanup()


def test_auto_open_restores_javascript_readable_learn_course_token_before_navigation():
    temporary, backend, item, _controller = backend_with_scanned_item()
    calls = []
    class FakeWs:
        def __init__(self): self.last = {}
        def settimeout(self, _timeout): pass
        def send(self, raw):
            import json
            self.last = json.loads(raw)
            calls.append(self.last)
        def recv(self):
            import json
            state = {"ready": True, "url": item["url"], "courseId": item["courseId"], "classId": item["classId"], "pageId": item["pageId"]}
            value = {"result": {"value": state}} if self.last["method"] == "Runtime.evaluate" else {}
            return json.dumps({"id": self.last["id"], "result": value})
        def close(self): pass
    try:
        backend.token = "test-token"
        backend.api._list_targets = Mock(return_value=[{"type": "page", "url": item["url"], "webSocketDebuggerUrl": "ws://selected"}])
        with patch("dgutbot.domain.yxy_backend.create_connection", return_value=FakeWs()):
            assert backend._navigate_course_target(item, backend._auto_course_generation) == "ws://selected"
        cookie_calls = [call for call in calls if call["method"] == "Network.setCookie"]
        assert [call["params"]["name"] for call in cookie_calls] == ["AUTHORIZATION", "token"]
        assert cookie_calls[0]["params"]["httpOnly"] is True
        assert cookie_calls[1]["params"]["httpOnly"] is False
        methods = [call["method"] for call in calls]
        assert max(index for index, method in enumerate(methods) if method == "Network.setCookie") < methods.index("Page.navigate")
    finally:
        temporary.cleanup()


def test_timeout_can_retry_selected_item():
    temporary, backend, item, _controller = backend_with_scanned_item()
    try:
        starts = []
        with patch.object(backend, "_navigate_course_target", side_effect=[RuntimeError("课件页面加载超时"), "ws://selected"]):
            assert not backend.auto_open_course(item["id"], lambda: starts.append(1) or True)
            assert backend.course_scan_status()["openPhase"] == "timeout"
            assert backend.auto_open_course(item["id"], lambda: starts.append(1) or True)
        assert starts == [1]
    finally:
        temporary.cleanup()


def test_stop_during_open_prevents_late_automatic_start():
    temporary, backend, item, _controller = backend_with_scanned_item()
    try:
        starts = []
        def navigate(_item, _generation):
            backend.stop_course_helper()
            return "ws://selected"
        with patch.object(backend, "_navigate_course_target", side_effect=navigate):
            assert not backend.auto_open_course(item["id"], lambda: starts.append(1) or True)
        assert starts == []
        assert backend.course_scan_status()["openPhase"] == "idle"
        assert backend.course_scan_status()["selectedId"] == ""
        assert backend.course_scan_status()["openError"] == ""
    finally:
        temporary.cleanup()
