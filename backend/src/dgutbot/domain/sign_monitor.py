"""课堂签到查询、提交与轮询生命周期。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

import requests

from dgutbot.domain.course_membership import CourseMembershipMonitor


LMS_BASE = "https://lms.dgut.edu.cn/courseapi"
APP_BASE = "https://application.dgut.edu.cn/classroomapi"


class CourseLike(Protocol):
    id: int
    name: str


class SignSettings(Protocol):
    poll_interval: int
    lat: float
    lng: float
    address: str


@dataclass(frozen=True)
class Classroom:
    id: int
    title: str
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_api(cls, data: dict) -> "Classroom":
        return cls(id=int(data["id"]), title=data.get("title", "未命名课堂"), raw=data)


@dataclass(frozen=True)
class Activity:
    relation_id: int
    title: str
    relation_type: int | None
    score_type: int | None
    state: int | None
    status: int | None
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_api(cls, data: dict) -> "Activity":
        return cls(
            relation_id=int(data["relationId"]),
            title=data.get("title", "未命名活动"),
            relation_type=data.get("relationType"),
            score_type=data.get("scoreType"),
            state=data.get("state"),
            status=data.get("status"),
            raw=data,
        )


class MonitorState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"


class SignMonitor:
    """只依赖注入的会话与配置，独立管理签到轮询。"""

    def __init__(
        self,
        *,
        request: Callable[..., Any],
        selected_course: Callable[[], CourseLike | None],
        settings: Callable[[], SignSettings],
        token: Callable[[], str],
        user_id: Callable[[], int | None],
        log: Callable[[str, str], None],
        recovery_path: Path | None = None,
    ) -> None:
        self._request = request
        self._selected_course = selected_course
        self._settings = settings
        self._token = token
        self._user_id = user_id
        self._log = log
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None
        self.state = MonitorState.IDLE
        self.round = 0
        self.interval = max(2, int(settings().poll_interval))
        self.started_at = ""
        self.last_check = ""
        self.last_result = "等待首次检查"
        self._last_sign_succeeded = False
        self._classroom_scope = self._scope()
        self._classroom_choices: list[Classroom] = []
        self._classroom: Classroom | None = None
        self._classroom_selection_required = False
        self.membership = (CourseMembershipMonitor(
            request=self._membership_request, user_id=user_id, log=log, path=recovery_path,
        ) if recovery_path is not None else None)

    def _membership_request(self, method: str, url: str, **kwargs):
        token = self._token().strip()
        if not token:
            raise RuntimeError("缺少登录信息，请登录原学生账号后恢复课程")
        headers = {"Authorization": token, "Accept": "application/json, text/plain, */*",
                   "Origin": "https://lms.dgut.edu.cn", "Referer": "https://lms.dgut.edu.cn/"}
        if method == "PUT":
            headers["Content-Type"] = "text/plain"
        try:
            return requests.request(method, url, headers=headers, timeout=12, allow_redirects=False, **kwargs)
        except requests.RequestException:
            # 第三方异常可能包含 URL 或请求头，面向界面只返回固定文案。
            raise RuntimeError("课程接口连接失败，保留恢复记录；请检查网络和登录状态") from None

    def classrooms(self, course_id: int) -> list[Classroom]:
        found: dict[int, Classroom] = {}
        for page in range(1, 101):
            data = self._request(
                "GET", f"{LMS_BASE}/wisdomClassroom/student/getClassroomList",
                params={"ocId": course_id, "status": "", "pageNum": page,
                        "pageSize": 100, "order": 0, "lang": "zh"},
            ).json()
            result = data.get("result") if isinstance(data, dict) and data.get("code") == 1 else None
            if not isinstance(result, dict) or not isinstance(result.get("list"), list):
                raise RuntimeError("课堂列表读取失败，请检查登录状态后重试")
            rows = result["list"]
            previous_count = len(found)
            for row in rows:
                classroom = Classroom.from_api(row)
                found[classroom.id] = classroom
            total = result.get("total")
            if not rows or (isinstance(total, int) and len(found) >= total):
                return list(found.values())
            if len(found) == previous_count:
                raise RuntimeError("课堂列表分页未前进，请稍后重试")
            if len(rows) < 100 and not isinstance(total, int):
                return list(found.values())
        raise RuntimeError("课堂列表过多，未能完整读取，请稍后重试")

    def _scope(self) -> tuple[int | None, int | None]:
        course = self._selected_course()
        return self._user_id(), course.id if course else None

    def _sync_classroom_selection(self) -> None:
        """持锁调用；课堂选择只能在原账号、原课程中使用。"""
        scope = self._scope()
        if scope != self._classroom_scope:
            self._classroom_scope = scope
            self._classroom_choices = []
            self._classroom = None
            self._classroom_selection_required = False
            if self.thread and self.thread.is_alive():
                self.stop_event.set()

    @staticmethod
    def _classroom_ended(classroom: Classroom) -> bool:
        """只按课堂自身状态判断，不看日期：长期不关的旧课堂仍是进行中。"""
        return str(getattr(classroom, "raw", {}).get("status")) == "2"

    def _update_classrooms(self, scope: tuple, choices: list[Classroom]) -> bool:
        with self.lock:
            self._sync_classroom_selection()
            if self._classroom_scope != scope:
                raise RuntimeError("账号或课程已变化，请重新读取课堂")
            old = self._classroom
            self._classroom_choices = choices
            self._classroom = next((item for item in choices if old and item.id == old.id), None)
            if old is None and len(choices) == 1 and not self._classroom_ended(choices[0]):
                self._classroom = choices[0]
            lost = old is not None and self._classroom is None
            self._classroom_selection_required = lost or bool(choices and self._classroom is None)
            return lost

    def prepare_classrooms(self) -> dict[str, Any]:
        with self.lock:
            self._sync_classroom_selection()
            if (self.thread and self.thread.is_alive()) or (self.membership and self.membership.pending):
                raise RuntimeError("请先停止监测并完成课程恢复，再重新选择课堂")
            scope = self._classroom_scope
            course = self._selected_course()
        if course is None:
            raise ValueError("尚未选择课程")
        choices = self.classrooms(course.id)
        self._update_classrooms(scope, choices)
        return self.status()

    def select_classroom(self, classroom_id: object) -> dict[str, Any]:
        with self.lock:
            self._sync_classroom_selection()
            if (self.thread and self.thread.is_alive()) or (self.membership and self.membership.pending):
                raise RuntimeError("请先停止监测并完成课程恢复，再重新选择课堂")
            classroom = next((item for item in self._classroom_choices if str(item.id) == str(classroom_id)), None)
            if classroom is None:
                raise ValueError("课堂已失效，请重新读取并选择")
            if self._classroom_ended(classroom):
                raise ValueError("该课堂已结束，请选择进行中的课堂")
            self._classroom = classroom
            self._classroom_selection_required = False
            return self.status()

    @classmethod
    def _classroom_mapping(cls, classroom: Classroom) -> dict[str, Any]:
        begin = ""
        raw = getattr(classroom, "raw", {})
        try:
            begin = datetime.fromtimestamp(float(raw.get("beginTime")) / 1000).astimezone().isoformat(timespec="minutes")
        except (OSError, OverflowError, TypeError, ValueError):
            pass
        return {"id": classroom.id, "title": classroom.title, "beginTime": begin,
                "statusLabel": {"0": "进行中", "2": "已结束"}.get(str(raw.get("status")), "状态未知"),
                "ended": cls._classroom_ended(classroom)}

    def activities(self, classroom_id: int) -> list[Activity]:
        response = self._request(
            "GET",
            f"{APP_BASE}/wisdomClassroom/student/classroomActivitys",
            params={"classroomId": classroom_id, "pageNum": 1, "pageSize": 999},
        )
        data = response.json()
        raw_activities = data.get("result", {}).get("list", []) if data.get("code") == 1 else []
        return [Activity.from_api(activity) for activity in raw_activities]

    @staticmethod
    def kind(score_type: int | None) -> str:
        return {0: "位置签到", 1: "二维码签到", 2: "数字码签到", 3: "一键签到"}.get(score_type, "未知签到")

    @staticmethod
    def attendance_code(activity: Activity) -> str:
        return next((
            str(activity.raw[key])
            for key in ("attendanceCode", "code", "codeStr", "signCode", "checkInCode")
            if activity.raw.get(key)
        ), "")

    def direct_sign_request(self, payload: dict) -> requests.Response:
        """使用已验证的认证信息复刻官方应用的签到提交。"""
        token = self._token().strip()
        if not token:
            raise RuntimeError("缺少 Authorization，无法直接提交签到")
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 5 Build/TP1A.221005.002; wv) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
            "Chrome/150.0.7871.46 Mobile Safari/537.36 umoocApp umoocApp -language-zh",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN",
            "Content-Type": "application/json;charset=UTF-8",
            "sec-ch-ua-platform": '"Android"',
            "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Android WebView";v="150"',
            "sec-ch-ua-mobile": "?1",
            "Origin": "https://lms.dgut.edu.cn",
            "Referer": "https://lms.dgut.edu.cn/",
            "X-Requested-With": "cn.ulearning.yxy",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Authorization": token,
        }
        return requests.post(
            f"{APP_BASE}/newAttendance/signByStu",
            json=payload,
            headers=headers,
            timeout=15,
        )

    def sign(self, course: CourseLike, classroom_id: int, activity: Activity) -> bool:
        self._last_sign_succeeded = False
        score_type = activity.score_type
        kind = self.kind(score_type)
        code = self.attendance_code(activity) if score_type == 1 else ""
        if score_type not in (0, 1, 2, 3):
            self._log(f"[{course.name}] {kind}：当前类型不支持自动处理，已跳过", "warn")
            return True
        settings = self._settings()
        payload = {
            "attendanceID": activity.relation_id,
            "classID": classroom_id,
            "userID": self._user_id(),
            "location": f"{settings.lat},{settings.lng}",
            "address": settings.address,
            "enterWay": 1,
            "attendanceCode": code,
        }
        http_status: int | str = "exception"
        raw_response = ""
        try:
            response = self.direct_sign_request(payload)
            http_status = response.status_code
            raw_response = response.text
            try:
                result = response.json()
            except (requests.JSONDecodeError, ValueError):
                result = {}
            status = result.get("status")
            if isinstance(status, str) and status.isdigit():
                status = int(status)
            message = result.get("msg", result.get("message", raw_response or f"HTTP {http_status}"))
        except Exception as error:
            status, message = "exception", str(error)
            raw_response = str(error)
        if status == 200:
            self._last_sign_succeeded = True
            self._log(f"✓ [{course.name}] {kind}：签到成功", "success")
        elif status in (201, 209):
            self._log(f"• [{course.name}] {kind}：已签到过", "muted")
        else:
            self._log(f"× [{course.name}] {kind}：{message}", "warn")
        return status in (200, 201, 209)

    def poll_once(self, checked: set[str]) -> str:
        if self.membership and (self.membership.pending or self.membership.context):
            if (self.stop_event.is_set() or not getattr(self._settings(), "selection_auto_leave", False)):
                self.membership.stop()
            result = self.membership.tick()
            if not self.membership.pending and not self.membership.context:
                self.stop_event.set()
            return result
        course = self._selected_course()
        if course is None:
            self._log("尚未选择课程，轮询已跳过。", "warn")
            return "尚未选择课程"
        with self.lock:
            self._sync_classroom_selection()
            scope = self._classroom_scope
        choices = self.classrooms(course.id)
        lost = self._update_classrooms(scope, choices)
        with self.lock:
            classroom = self._classroom
        if lost or (classroom is None and choices):
            self.stop_event.set()
            self._log(f"[{course.name}] 本轮完成：请选择需要监测的课堂。", "warn")
            return "所选课堂不可用，请重新选择" if lost else "请选择课堂后开始监测"
        if classroom is None:
            self._log(f"[{course.name}] 本轮完成：暂无课堂，请稍后刷新。", "muted")
            return "暂无课堂"
        active_count = 0
        new_count = 0
        for activity in self.activities(classroom.id):
            if self._scope() != scope:
                self.stop_event.set()
                return "账号或课程已变化，请重新选择课堂"
            if (self.membership and getattr(self._settings(), "selection_auto_leave", False)
                    and str(classroom.raw.get("status")) == "0" and activity.relation_type == 1
                    and activity.state in (1, 2) and not self.stop_event.is_set()):
                if self.membership.arm(course, classroom):
                    self._log(f"[{course.name}] 已确认签到完成，开始监测选人活动。", "success")
                    return self.membership.tick()
            if activity.relation_type != 1 or activity.state not in (0, 1) or activity.status != 0:
                continue
            active_count += 1
            key = f"{activity.relation_id}_{classroom.id}"
            if key in checked:
                continue
            new_count += 1
            self._log(f"[{course.name}] 发现 {self.kind(activity.score_type)}，正在处理…", "info")
            if self.sign(course, classroom.id, activity):
                checked.add(key)
            if self._last_sign_succeeded:
                if (not self.stop_event.is_set() and self.membership
                        and getattr(self._settings(), "selection_auto_leave", False)):
                    try:
                        if self.membership.arm(course, classroom):
                            self._log(f"[{course.name}] 签到成功，继续监测选人活动。", "success")
                            return self.membership.tick()
                    except Exception:
                        if self.membership.pending:
                            return "签到已成功，选课变更结果待核对；恢复记录已保留"
                        self._log("签到已成功，但活动监测初始化失败；未自动退课，请检查登录和班级信息。", "warn")
                self.stop()
                self._log("已自动停止签到监测。", "success")
                return "签到成功，已停止监测"
        if active_count == 0:
            self._log(f"[{course.name}] 本轮完成：未发现进行中的签到。", "muted")
            return "未发现进行中的签到"
        if new_count == 0:
            self._log(f"[{course.name}] 本轮完成：签到活动已处理，继续等待。", "muted")
            return "签到活动已处理"
        return f"已处理 {new_count} 个签到活动"

    def status(self) -> dict[str, Any]:
        membership = self.membership.status() if self.membership else {}
        with self.lock:
            self._sync_classroom_selection()
            running = self.thread is not None and self.thread.is_alive()
            course = self._selected_course()
            return {
                "running": running,
                "state": self.state.value,
                "courseName": membership.get("recoveryCourseName") or (course.name if course else ""),
                "intervalSeconds": self.interval,
                "round": self.round,
                "startedAt": self.started_at,
                "lastCheck": self.last_check,
                "lastResult": self.last_result,
                "classroomId": self._classroom.id if self._classroom else None,
                "classroomName": self._classroom.title if self._classroom else "",
                "classrooms": [self._classroom_mapping(item) for item in self._classroom_choices],
                "classroomSelectionRequired": self._classroom_selection_required,
                **membership,
            }

    def start(self) -> bool:
        with self.lock:
            self._sync_classroom_selection()
            if self.thread is not None and self.thread.is_alive():
                self._log("签到监测已在运行，无需重复启动。", "muted")
                return False
            self.stop_event.clear()
            if self.membership:
                self.membership.reset()
            self.state = MonitorState.RUNNING
            self.round = 0
            self.interval = max(2, int(self._settings().poll_interval))
            self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.last_check = ""
            self.last_result = "等待首次检查"
            self.thread = threading.Thread(target=self._monitor, name="sign-monitor", daemon=True)
            self.thread.start()
            return True

    def stop(self) -> None:
        self.stop_event.set()
        if self.membership:
            self.membership.stop()
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                self.state = MonitorState.STOPPED

    def _monitor(self) -> None:
        try:
            checked: set[str] = set()
            interval = self.interval
            self._log(f"开始轮询，每 {interval} 秒检查一次。", "success")
            round_number = 0
            while not self.stop_event.is_set() or (self.membership and self.membership.pending):
                round_number += 1
                try:
                    course = self._selected_course()
                    course_name = course.name if course else "未选择课程"
                    self._log(f"第 {round_number} 轮：正在检查《{course_name}》…", "info")
                    result = self.poll_once(checked)
                    with self.lock:
                        self.round = round_number
                        self.last_check = datetime.now().astimezone().isoformat(timespec="seconds")
                        self.last_result = result
                except Exception as error:
                    with self.lock:
                        self.round = round_number
                        self.last_check = datetime.now().astimezone().isoformat(timespec="seconds")
                        self.last_result = "检查失败"
                    self._log(f"轮询出错：{error}", "warn")
                if self.stop_event.is_set() and self.membership and self.membership.pending:
                    # 停止操作仍需继续恢复，避免 set 的 Event 导致忙循环。
                    time.sleep(interval)
                else:
                    self.stop_event.wait(interval)
        finally:
            with self.lock:
                self.state = MonitorState.IDLE
                if self.thread is threading.current_thread():
                    self.thread = None


__all__ = ["Activity", "APP_BASE", "Classroom", "LMS_BASE", "MonitorState", "SignMonitor"]
