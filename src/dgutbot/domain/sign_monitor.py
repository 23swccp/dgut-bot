"""课堂签到查询、提交与轮询生命周期。"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Protocol

import requests


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
        write_log: Callable[[str, str, list[str]], None],
    ) -> None:
        self._request = request
        self._selected_course = selected_course
        self._settings = settings
        self._token = token
        self._user_id = user_id
        self._log = log
        self._write_log = write_log
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.state = MonitorState.IDLE
        self.round = 0
        self.interval = max(2, int(settings().poll_interval))
        self.started_at = ""
        self.last_check = ""
        self.last_result = "等待首次检查"

    def classrooms(self, course_id: int) -> list[Classroom]:
        response = self._request(
            "GET",
            f"{LMS_BASE}/wisdomClassroom/student/getClassroomList",
            params={
                "ocId": course_id,
                "status": "",
                "pageNum": 1,
                "pageSize": 10,
                "order": 0,
                "lang": "zh",
            },
        )
        data = response.json()
        raw_classrooms = data.get("result", {}).get("list", []) if data.get("code") == 1 else []
        return [Classroom.from_api(classroom) for classroom in raw_classrooms]

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
    def classroom_is_today(classroom: Classroom, now: datetime | None = None) -> bool:
        """优先使用课堂开始时间；旧接口缺少时间时才兼容标题日期。"""
        current = now or datetime.now()
        classroom_raw = getattr(classroom, "raw", {})
        raw = classroom_raw if isinstance(classroom_raw, dict) else {}
        begin_time = raw.get("beginTime")
        try:
            milliseconds = float(begin_time)
            if math.isfinite(milliseconds):
                return datetime.fromtimestamp(milliseconds / 1000).date() == current.date()
        except (OSError, OverflowError, TypeError, ValueError):
            pass
        return current.strftime("%m-%d") in classroom.title

    @staticmethod
    def kind(score_type: int | None) -> str:
        return {0: "选人点名", 1: "二维码签到", 2: "数字码签到", 3: "一键签到"}.get(score_type, "未知签到")

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
        score_type = activity.score_type
        kind = self.kind(score_type)
        activity_id = activity.relation_id
        code = self.attendance_code(activity) if score_type == 1 else ""
        if score_type not in (1, 2, 3):
            self._log(f"[{course.name}] {kind}：当前类型不支持自动处理，已跳过", "warn")
            self._write_log(course.name, kind, [
                f"attendanceID: {activity_id}",
                "result: skipped",
                "reason: unsupported scoreType",
            ])
            return True
        settings = self._settings()
        payload = {
            "attendanceID": activity_id,
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
            self._log(f"✓ [{course.name}] {kind}：签到成功", "success")
        elif status in (201, 209):
            self._log(f"• [{course.name}] {kind}：已签到过", "muted")
        else:
            self._log(f"× [{course.name}] {kind}：{message}", "warn")
        self._write_log(course.name, kind, [
            "transport: direct-python",
            f"endpoint: {APP_BASE}/newAttendance/signByStu",
            f"request: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}",
            f"HTTP: {http_status}",
            f"serviceStatus: {status}",
            f"rawResponse: {raw_response}",
        ])
        return status in (200, 201, 209)

    def poll_once(self, checked: set[str]) -> str:
        course = self._selected_course()
        if course is None:
            self._log("尚未选择课程，轮询已跳过。", "warn")
            return "尚未选择课程"
        now = datetime.now()
        classrooms = [item for item in self.classrooms(course.id) if self.classroom_is_today(item, now)]
        if not classrooms:
            self._log(f"[{course.name}] 本轮完成：今天没有课堂，无需签到。", "muted")
            return "今日暂无课堂"
        active_count = 0
        new_count = 0
        for classroom in classrooms:
            for activity in self.activities(classroom.id):
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
        if active_count == 0:
            self._log(f"[{course.name}] 本轮完成：未发现进行中的签到。", "muted")
            return "未发现进行中的签到"
        if new_count == 0:
            self._log(f"[{course.name}] 本轮完成：签到活动已处理，继续等待。", "muted")
            return "签到活动已处理"
        return f"已处理 {new_count} 个签到活动"

    def status(self) -> dict[str, Any]:
        with self.lock:
            running = self.thread is not None and self.thread.is_alive()
            course = self._selected_course()
            return {
                "running": running,
                "state": self.state.value,
                "courseName": course.name if course else "",
                "intervalSeconds": self.interval,
                "round": self.round,
                "startedAt": self.started_at,
                "lastCheck": self.last_check,
                "lastResult": self.last_result,
            }

    def start(self) -> bool:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                self._log("签到监测已在运行，无需重复启动。", "muted")
                return False
            self.stop_event.clear()
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
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                self.state = MonitorState.STOPPED

    def _monitor(self) -> None:
        try:
            checked: set[str] = set()
            interval = self.interval
            self._log(f"开始轮询，每 {interval} 秒检查一次。", "success")
            round_number = 0
            while not self.stop_event.is_set():
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
                self.stop_event.wait(interval)
        finally:
            with self.lock:
                self.state = MonitorState.IDLE
                if self.thread is threading.current_thread():
                    self.thread = None


__all__ = ["Activity", "APP_BASE", "Classroom", "LMS_BASE", "MonitorState", "SignMonitor"]
