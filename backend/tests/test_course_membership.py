"""全部使用本机假平台，不向学校接口提交任何请求。"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dgutbot.domain.course_membership import CourseMembershipMonitor
from dgutbot.domain.sign_monitor import Activity, Classroom
from dgutbot.domain.yxy_backend import Course, SignBackend


class Platform:
    def __init__(self):
        self.uid = 7
        self.role = 9
        self.class_id = 30
        self.enrolled = True
        self.classroom_status = 0
        self.items = [{"relationId": 50, "relationType": 4, "state": 1, "status": 0}]
        self.calls = []
        self.join_code = 1
        self.join_applies = True
        self.quit_timeout = False
        self.activities_error = False
        self.activities_invalid = False
        self.path = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        data = {}
        if url.endswith("getUserInfo"):
            data = {"code": 1, "result": {"userId": self.uid, "roleId": self.role}}
        elif "/classes/information/student/" in url:
            data = {"classId": self.class_id, "code": "sample-code"} if self.enrolled else {}
        elif url.endswith("getClassroomList"):
            data = {"code": 1, "result": {"list": [
                {"id": 20, "status": self.classroom_status},
            ] if self.enrolled else []}}
        elif url.endswith("classroomActivitys"):
            if self.activities_error:
                raise RuntimeError("network failed")
            data = {"code": 0, "result": None} if self.activities_invalid else {
                "code": 1, "result": {"ocId": 10, "list": self.items}}
        elif method == "PUT" and url.endswith("quitCourse"):
            # 在真实变更发生之前，恢复日志必须已落盘。
            assert json.loads(self.path.read_text())["phase"] == "quit_pending"
            self.enrolled = False
            if self.quit_timeout:
                raise RuntimeError("request timed out after server applied it")
        elif method == "POST" and "/user/addClass" in url:
            assert kwargs == {"params": {"ocId": 10}, "json": [7]}
            assert url.endswith("/classes/30/user/addClass")
            data = {"code": self.join_code}
            if self.join_code == 1 and self.join_applies:
                self.enrolled = True
        else:
            raise AssertionError((method, url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: data)

    def count(self, suffix):
        return sum(url.endswith(suffix) for _, url, _ in self.calls)


@pytest.fixture
def setup(tmp_path):
    platform = Platform()
    path = tmp_path / "course-recovery.json"
    platform.path = path
    logs = Mock()
    monitor = CourseMembershipMonitor(request=platform.request, user_id=lambda: 7, log=logs, path=path)
    monitor.arm(Course(10, "测试课程"), Classroom(20, "测试课堂"))
    return monitor, platform, path, logs


@pytest.mark.parametrize("relation_type", [4, 10])
def test_leave_once_and_restore_original_class_after_selection_ends(setup, relation_type):
    monitor, platform, path, _ = setup
    platform.items[0]["relationType"] = relation_type
    monitor.tick()
    assert not platform.enrolled and path.exists()
    for _ in range(3):
        monitor.tick()
    assert platform.count("quitCourse") == 1
    platform.items[0]["status"] = 2
    assert "已恢复原班级" in monitor.tick()
    assert platform.enrolled and not path.exists() and not monitor.pending
    assert platform.count("addClass") == 1
    monitor.tick()
    assert platform.count("quitCourse") == 1


@pytest.mark.parametrize("relation_type,status", [(1, 0), (3, 0), (10, 2), (4, 1), (4, None)])
def test_only_ongoing_selection_triggers_leave(setup, relation_type, status):
    monitor, platform, path, _ = setup
    platform.items[0].update(relationType=relation_type, status=status)
    monitor.tick()
    assert platform.enrolled and not path.exists()
    assert platform.count("quitCourse") == 0


def test_overlapping_selections_wait_for_all_to_finish(setup):
    monitor, platform, _, _ = setup
    monitor.tick()
    platform.items.append({"relationId": 51, "relationType": 10, "status": 0})
    platform.items[0]["status"] = 2
    monitor.tick()
    assert not platform.enrolled
    platform.items[1]["status"] = 2
    monitor.tick()
    assert platform.enrolled


def test_classroom_end_signal_restores_and_finishes_monitoring(setup):
    monitor, platform, _, _ = setup
    monitor.tick()
    # 实页验证：下课同时将原选人活动标为已结束。
    platform.items[0]["status"] = 2
    platform.classroom_status = 2
    monitor.tick()
    assert platform.enrolled and monitor.context is None


@pytest.mark.parametrize("failure", ["activities_error", "activities_invalid"])
def test_activity_failures_never_mean_ended(setup, failure):
    monitor, platform, path, _ = setup
    monitor.tick()
    setattr(platform, failure, True)
    with pytest.raises(RuntimeError):
        monitor.tick()
    assert monitor.pending and path.exists() and not platform.enrolled
    assert platform.count("addClass") == 0


def test_empty_activity_list_does_not_mean_ended(setup):
    monitor, platform, _, _ = setup
    monitor.tick()
    platform.items = []
    monitor.tick()
    assert not platform.enrolled and monitor.pending


def test_stop_recovers_without_needing_activity_queries(setup):
    monitor, platform, path, _ = setup
    monitor.tick()
    platform.activities_error = True
    monitor.stop()
    monitor.tick()
    assert platform.enrolled and not path.exists() and monitor.context is None


def test_restart_prioritizes_recovery_even_with_feature_disabled(setup):
    monitor, platform, path, _ = setup
    monitor.tick()
    platform.activities_error = True
    recovered = CourseMembershipMonitor(request=platform.request, user_id=lambda: 7, log=Mock(), path=path)
    assert recovered.pending["phase"] == "restore_pending"
    recovered.tick()
    assert platform.enrolled and not path.exists()
    assert platform.count("quitCourse") == 1


def test_request_timeout_after_leave_does_not_repeat_leave(setup):
    monitor, platform, path, _ = setup
    platform.quit_timeout = True
    with pytest.raises(RuntimeError):
        monitor.tick()
    assert not platform.enrolled and path.exists()
    monitor.tick()
    assert platform.enrolled and not monitor.pending
    monitor.tick()
    assert platform.count("quitCourse") == 1


def test_cannot_leave_without_durable_recovery_record(setup):
    monitor, platform, _, _ = setup
    with patch.object(monitor, "_save", side_effect=OSError("disk full")):
        with pytest.raises(RuntimeError, match="未自动退课"):
            monitor.tick()
    assert platform.enrolled and platform.count("quitCourse") == 0


def test_brief_windows_file_lock_still_persists_record_before_leaving(setup):
    monitor, platform, path, _ = setup
    original = Path.replace
    blocked = True

    def replace(source, target):
        nonlocal blocked
        if blocked:
            blocked = False
            raise PermissionError("file is briefly locked")
        return original(source, target)

    with patch.object(Path, "replace", new=replace), patch("dgutbot.domain.course_membership.time.sleep"):
        monitor.tick()
    assert not platform.enrolled
    assert json.loads(path.read_text())["phase"] == "withdrawn"
    assert platform.count("quitCourse") == 1


@pytest.mark.parametrize("change", ["cached_user", "actual_user", "teacher"])
def test_recovery_cannot_write_using_another_identity(setup, change):
    monitor, platform, path, _ = setup
    monitor.tick()
    monitor.stop()
    if change == "cached_user":
        monitor.user_id = lambda: 8
    elif change == "actual_user":
        platform.uid = 8
    else:
        platform.role = 81
    with pytest.raises(RuntimeError):
        monitor.tick()
    assert path.exists() and platform.count("addClass") == 0


@pytest.mark.parametrize("join_code", [1, 2])
def test_success_or_pending_join_response_requires_membership_verification(setup, join_code):
    monitor, platform, path, _ = setup
    monitor.tick()
    platform.join_code = join_code
    platform.join_applies = False
    monitor.stop()
    for _ in range(3):
        monitor.tick()
    assert monitor.pending and path.exists() and not platform.enrolled
    assert platform.count("addClass") == 1
    platform.enrolled = True
    monitor.tick()
    assert not path.exists() and monitor.pending is None


def test_different_class_never_counts_as_restored(setup):
    monitor, platform, path, _ = setup
    monitor.tick()
    platform.class_id = 31
    platform.enrolled = True
    monitor.stop()
    with pytest.raises(RuntimeError, match="其他班级"):
        monitor.tick()
    assert path.exists() and platform.count("addClass") == 0


def test_invalid_recovery_record_blocks_new_automatic_leaves(tmp_path):
    path = tmp_path / "course-recovery.json"
    path.write_text('{"schema":1,"courseId":true}')
    monitor = CourseMembershipMonitor(request=Mock(), user_id=lambda: 7, log=Mock(), path=path)
    assert monitor.status()["recoveryError"]
    assert not monitor.arm(Course(10, "测试"), Classroom(20, "课堂"))
    assert path.exists()


@pytest.mark.parametrize("already_signed", [False, True])
def test_sign_success_transitions_to_activity_monitoring(tmp_path, already_signed):
    backend = SignBackend(Mock(), root=tmp_path)
    backend.config.selection_auto_leave = True
    backend.user_id = 7
    backend.selected_course = Course(10, "测试课程")
    platform = Platform()
    platform.items = []
    platform.path = backend.sign_monitor.membership.path
    backend.sign_monitor.membership.request = platform.request
    classroom = Classroom.from_api({"id": 20, "title": "课堂", "status": 0})
    activity = Activity.from_api({"relationId": 1, "relationType": 1, "state": 1 if already_signed else 0,
                                  "scoreType": 3, "status": 0})
    response = SimpleNamespace(status_code=200, text='{"status":200}', json=lambda: {"status": 200})
    with patch.object(backend.sign_monitor, "classrooms", return_value=[classroom]), patch.object(
        backend.sign_monitor, "activities", return_value=[activity]), patch.object(
        backend.sign_monitor, "direct_sign_request", return_value=response,
    ) as sign:
        assert "监测选人活动" in backend.sign_monitor.poll_once(set())
    assert not backend.stop_event.is_set() and backend.sign_monitor.membership.context
    assert sign.call_count == (0 if already_signed else 1)


def test_membership_transport_uses_current_student_token_and_json(tmp_path):
    backend = SignBackend(Mock(), root=tmp_path)
    backend.token = "student-example"
    backend.headers["Authorization"] = "current-student-example"
    with patch("dgutbot.domain.sign_monitor.requests.request") as request:
        backend.sign_monitor._membership_request("POST", "https://lms.dgut.edu.cn/courseapi/classes/30/user/addClass", json=[7])
    assert request.call_args.kwargs["headers"]["Authorization"] == "current-student-example"
    assert request.call_args.kwargs["json"] == [7]
    assert request.call_args.kwargs["allow_redirects"] is False


def test_config_default_off_and_boolean_validation(tmp_path):
    from dgutbot.domain.yxy_backend import AppConfig
    assert not AppConfig.from_mapping({}, tmp_path).selection_auto_leave
    assert AppConfig.from_mapping({"selection_auto_leave": "true"}, tmp_path).selection_auto_leave
    assert not AppConfig.from_mapping({"selection_auto_leave": "false"}, tmp_path).selection_auto_leave


def test_pending_approval_survives_restart_without_duplicate_join(setup):
    monitor, platform, path, _ = setup
    monitor.tick()
    platform.join_code = 2
    monitor.stop()
    monitor.tick()
    restarted = CourseMembershipMonitor(request=platform.request, user_id=lambda: 7, log=Mock(), path=path)
    restarted.tick()
    assert restarted.pending and platform.count("addClass") == 1


def test_backend_restart_recovers_with_no_selected_course_or_feature_flag(setup):
    monitor, platform, path, _ = setup
    monitor.tick()
    (path.parent / "auth.json").write_text(json.dumps({"token": "example", "user_id": 7}))

    def transport(method, url, **kwargs):
        return platform.request(method, url, **{k: v for k, v in kwargs.items() if k in ("json", "params")})

    with patch("dgutbot.domain.sign_monitor.requests.request", side_effect=transport):
        backend = SignBackend(Mock(), root=path.parent)
        worker = backend.monitor_thread
        if worker is not None:
            worker.join(2)
            assert not worker.is_alive()
    assert backend.selected_course is None and not backend.config.selection_auto_leave
    assert platform.enrolled and not path.exists()
