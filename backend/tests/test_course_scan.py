import threading
import time
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from dgutbot.domain.course_scan import AuthRequiredError, CourseScanService, ScanCancelled, completion_state, flatten_lessons
from dgutbot.domain.yxy_backend import Course
from dgutbot.domain.yxy_backend import SignBackend


URL = "https://ua.dgut.edu.cn/learnCourse?courseId=1&pageId=p1"


def lesson(**values):
    return {"pageId": "p1", "name": "导论", "contentType": "video", "url": URL, **values}


def wait_done(service):
    deadline = time.monotonic() + 2
    while service.snapshot()["state"] == "scanning" and time.monotonic() < deadline:
        time.sleep(.01)
    if service._thread and service.snapshot()["state"] != "scanning":
        service._thread.join(2)
    return service.snapshot()


def wait_progress(service, count):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = service.snapshot()
        if state["scannedCourses"] >= count:
            return state
        time.sleep(.005)
    raise AssertionError("扫描未发布预期的阶段结果")


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


def test_fast_course_is_openable_before_slow_course_finishes():
    release = threading.Event()
    def directory(course):
        if course.id == 2:
            assert release.wait(3)
        return {"pages": [lesson(pageId=str(course.id))]}
    service = CourseScanService(lambda: [Course(1, "快课"), Course(2, "慢课")], directory, lambda: True, str)
    try:
        service.start()
        state = wait_progress(service, 1)
        assert state["state"] == "scanning" and len(state["groups"]) == 1
        item = state["groups"][0]["items"][0]
        assert service.item(item["id"])["canAutoOpen"]
        service.set_open_state("started", selected_id=item["id"])
    finally:
        release.set()
        state = wait_done(service)
    assert state["scannedCourses"] == 2 and len(state["groups"]) == 2
    assert state["openPhase"] == "started" and state["selectedId"] == item["id"]


def test_only_failed_courses_are_retried_within_cache_lifetime():
    calls = []
    def directory(course):
        calls.append(course.id)
        if course.id == 2 and calls.count(2) == 1:
            raise RuntimeError("temporary failure")
        return {"pages": [lesson(pageId=str(course.id))]}
    service = CourseScanService(lambda: [Course(1, "正常"), Course(2, "失败")], directory, lambda: True, str)
    service.start()
    assert len(wait_done(service)["failures"]) == 1
    service.start()
    state = wait_done(service)
    assert sorted(calls) == [1, 2, 2]
    assert state["failures"] == [] and state["unfinishedCount"] == 2 and state["fromCache"]


def test_expiration_is_per_course_and_removed_courses_leave_cache():
    courses = [Course(1, "过期"), Course(2, "有效")]
    calls = []
    service = CourseScanService(lambda: courses, lambda course: calls.append(course.id) or {"pages": [lesson(pageId=str(course.id))]}, lambda: True, str)
    service.start()
    wait_done(service)
    service._cache["1"] = (time.monotonic() - 301, service._cache["1"][1])
    service.start()
    wait_done(service)
    assert sorted(calls) == [1, 1, 2]
    courses.pop(0)
    state = service.start()
    assert list(service._cache) == ["2"] and state["unfinishedCount"] == 1


def test_force_refresh_keeps_previous_list_until_replaced():
    release = threading.Event()
    refresh = False
    def directory(_course):
        if refresh:
            assert release.wait(3)
        return {"pages": [lesson(pageId="new" if refresh else "old")]}
    service = CourseScanService(lambda: [Course(1, "课")], directory, lambda: True, str)
    service.start()
    wait_done(service)
    refresh = True
    try:
        state = service.start(force=True)
        assert state["state"] == "scanning" and state["groups"][0]["items"][0]["pageId"] == "old"
    finally:
        release.set()
        state = wait_done(service)
    assert state["groups"][0]["items"][0]["pageId"] == "new"


def test_authentication_is_checked_before_using_cache():
    authenticated = True
    service = CourseScanService(lambda: [Course(1, "课")], lambda _: {"pages": [lesson()]}, lambda: authenticated, str)
    service.start()
    wait_done(service)
    authenticated = False
    assert service.start()["state"] == "auth_required"
    assert not service.snapshot()["groups"] and not service._cache


def test_changed_account_rejects_cached_and_inflight_results():
    context = "old"
    release = threading.Event()
    entered = threading.Event()
    def directory(_course):
        entered.set()
        assert release.wait(3)
        return {"pages": [lesson()]}
    service = CourseScanService(lambda: [Course(1, "课")], directory, lambda: True, str, cache_context=lambda: context)
    try:
        service.start()
        assert entered.wait(2)
        context = "new"
        assert service.snapshot()["groups"] == []
    finally:
        release.set()
        service._thread.join(2)
    assert service.snapshot()["state"] == "auth_required" and not service._cache
    service.start()
    assert wait_done(service)["unfinishedCount"] == 1
    context = "third"
    assert service.snapshot()["groups"] == [] and not service._cache


def test_cancel_stops_queued_courses_and_reuses_completed_cache_on_restart():
    release = threading.Event()
    entered = threading.Event()
    calls = []
    def directory(course):
        calls.append(course.id)
        if course.id == 2:
            entered.set()
            assert release.wait(3)
        return {"pages": [lesson(pageId=str(course.id))]}
    service = CourseScanService(lambda: [Course(i, str(i)) for i in range(1, 4)], directory, lambda: True, str, max_workers=1)
    try:
        service.start()
        assert entered.wait(2)
        cancelled = service.cancel()
        assert cancelled["unfinishedCount"] == 1 and cancelled["stopping"]
        # 在旧请求结束前重复启动不创建新的线程池。
        assert service.start()["state"] == "cancelled"
    finally:
        release.set()
        service._thread.join(2)
    assert calls == [1, 2] and service.snapshot()["unfinishedCount"] == 1
    assert not service.snapshot()["stopping"]
    service.start()
    assert wait_done(service)["unfinishedCount"] == 3
    assert calls == [1, 2, 2, 3]


def test_worker_sessions_are_reused_bounded_and_closed():
    barrier = threading.Barrier(3)
    active = set()
    lock = threading.Lock()
    counts = []
    closed = []
    @contextmanager
    def session(_cancel):
        with lock:
            worker = len(counts)
            counts.append(0)
        def directory(course):
            with lock:
                active.add(worker)
                assert len(active) <= 3
                counts[worker] += 1
            if course.id < 3:
                barrier.wait(2)
            with lock:
                active.remove(worker)
            return {"pages": []}
        try:
            yield directory
        finally:
            closed.append(worker)
    service = CourseScanService(lambda: [Course(i, str(i)) for i in range(9)], lambda _: None, lambda: True, str, directory_session=session)
    service.start()
    state = wait_done(service)
    assert state["state"] == "completed" and not state["failures"]
    assert len(counts) == 3 and sum(counts) == 9 and sorted(closed) == [0, 1, 2]


def test_empty_textbook_list_skips_class_lookup():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        backend.api.request = Mock()
        backend.api.request.return_value.json.return_value = []
        assert backend._course_directory_payload(Course(1, "空课")) == {"pages": []}
        assert backend.api.request.call_count == 1


def test_duplicate_textbooks_are_requested_once():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        responses = [Mock(), Mock(), Mock()]
        for response, value in zip(responses, ([{"courseId": 9}, {"courseId": 9}], {"classId": 3}, {"list": []})):
            response.json.return_value = value
        backend.api.request = Mock(side_effect=responses)
        assert backend._course_directory_payload(Course(1, "课")) == {"pages": []}
        assert backend.api.request.call_count == 3


def test_cancel_during_request_does_not_fetch_class_or_retry():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        cancel = threading.Event()
        def request(*_args, **_kwargs):
            cancel.set()
            raise RuntimeError("浏览器请求失败：AbortError")
        backend.api.request = Mock(side_effect=request)
        with pytest.raises(ScanCancelled):
            backend._course_directory_payload(Course(1, "课"), cancel=cancel)
        assert backend.api.request.call_count == 1


def test_course_budget_limits_request_and_prevents_followup():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        backend.api.request = Mock()
        backend.api.request.return_value.json.return_value = [{"courseId": 9}]
        with patch("dgutbot.domain.yxy_backend.time.monotonic", side_effect=[100, 106]):
            with pytest.raises(TimeoutError, match="30 秒预算"):
                backend._course_directory_payload(Course(1, "课"), deadline=105)
        assert backend.api.request.call_count == 1
        assert backend.api.request.call_args.kwargs["timeout"] == 5


def test_backend_scan_session_reuses_one_client_and_closes_on_failure():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        client = Mock()
        with patch("dgutbot.domain.yxy_backend.BrowserApiClient", return_value=client) as factory, patch.object(
            backend, "_course_directory_payload", side_effect=[{}, RuntimeError("read failed")],
        ) as read:
            with pytest.raises(RuntimeError, match="read failed"):
                with backend._course_scan_session(threading.Event()) as scan:
                    scan(Course(1, "一"))
                    scan(Course(2, "二"))
            assert factory.call_count == 1 and read.call_count == 2
            assert all(call.kwargs["api"] is client for call in read.call_args_list)
            client.close.assert_called_once()


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
