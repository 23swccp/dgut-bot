"""签到后的课堂活动监测，以及可恢复的临时退课事务。"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable


LMS_BASE = "https://lms.dgut.edu.cn/courseapi"
APP_BASE = "https://application.dgut.edu.cn/classroomapi"


class CourseMembershipMonitor:
    """只管理当前学生的一门课；退课前先持久化原班级和课堂。"""

    def __init__(self, *, request: Callable, user_id: Callable, log: Callable,
                 path: Path) -> None:
        self.request = request
        self.user_id = user_id
        self.log = log
        self.path = path
        self.lock = threading.RLock()
        self.context: dict[str, Any] | None = None
        self.pending: dict[str, Any] | None = None
        self.error = ""
        self._retry_at = 0.0
        self._stopped = False
        try:
            if path.exists():
                value = json.loads(path.read_text(encoding="utf-8"))
                required = ("userId", "courseId", "classId", "classroomId", "activityIds", "phase")
                if (not isinstance(value, dict) or value.get("schema") != 1
                        or not all(key in value for key in required)
                        or any(type(value[key]) is not int or value[key] <= 0
                               for key in ("userId", "courseId", "classId", "classroomId"))
                        or not isinstance(value["activityIds"], list)
                        or not value["activityIds"]
                        or any(type(item) is not int or item <= 0 for item in value["activityIds"])
                        or value["phase"] not in ("quit_pending", "withdrawn", "restore_pending", "join_pending")):
                    raise ValueError("invalid recovery record")
                self.pending = value
                # 重启后优先恢复原班级，不依赖原活动仍可查询。
                if self.pending["phase"] != "join_pending":
                    self.pending["phase"] = "restore_pending"
        except (OSError, ValueError, TypeError):
            self.error = "课程恢复记录无法读取，请检查本机记录并手动确认选课状态"

    def status(self) -> dict[str, Any]:
        with self.lock:
            value = self.pending or self.context or {}
            return {"activityMonitoring": bool(self.context), "recoveryPending": bool(self.pending),
                    "membershipPhase": value.get("phase", "watching" if self.context else "idle"),
                    "recoveryCourseName": str(value.get("courseName", "")),
                    "recoveryError": self.error}

    def _json(self, method: str, url: str, **kwargs) -> Any:
        response = self.request(method, url, **kwargs)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"课程接口返回 HTTP {response.status_code}")
        return response.json()

    def _identity(self, expected: int | None = None) -> int:
        data = self._json("POST", f"{APP_BASE}/users/getUserInfo", json={})
        result = data.get("result", data) if isinstance(data, dict) else {}
        uid = result.get("userId", result.get("id")) if isinstance(result, dict) else None
        if (not isinstance(data, dict) or data.get("code") != 1
                or str(result.get("roleId")) != "9" or not str(uid).isdigit()
                or int(uid) != self.user_id() or (expected is not None and int(uid) != expected)):
            raise RuntimeError("请登录原学生账号后恢复课程，未发送选课变更请求")
        return int(uid)

    def _class_id(self, course_id: int) -> int | None:
        data = self._json("GET", f"{LMS_BASE}/classes/information/student/{course_id}")
        if not isinstance(data, dict):
            raise RuntimeError("无法核对原班级，未确认恢复成功")
        # 平台实测：未加入返回 {}，已加入返回 {classId, code, className}。
        if "code" in data and isinstance(data["code"], int) and data["code"] != 1:
            raise RuntimeError("班级信息接口未确认查询成功")
        value = data.get("result", data)
        if not isinstance(value, dict):
            raise RuntimeError("班级信息结构不正确")
        cid = value.get("classId")
        if cid is None and not value:
            return None
        if not str(cid).isdigit() or int(cid) <= 0:
            raise RuntimeError("无法核对班级编号")
        return int(cid)

    def _save(self) -> None:
        if self.pending is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.pending, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(4):
            try:
                temporary.replace(self.path)
                break
            except PermissionError:
                # Windows 的索引/防护程序可能短暂持有文件；保持原子替换并有限重试。
                if attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def arm(self, course, classroom) -> bool:
        with self.lock:
            if self.error or self.pending or self._stopped:
                return False
            uid = self._identity()
            cid = self._class_id(course.id)
            if cid is None:
                raise RuntimeError("未确认原班级，无法启动选人活动监测")
            self.context = {"schema": 1, "userId": uid, "courseId": course.id,
                            "courseName": course.name, "classId": cid,
                            "classroomId": classroom.id, "phase": "watching"}
            return True

    @staticmethod
    def active_selection(item: dict) -> bool:
        # status 是活动开闭状态，不能把学生参与状态 state 当作结束标志。
        return str(item.get("relationType")) in ("4", "10") and str(item.get("status")) == "0"

    def _activities(self, value: dict) -> list[dict]:
        data = self._json("GET", f"{APP_BASE}/wisdomClassroom/student/classroomActivitys",
                          params={"classroomId": value["classroomId"], "pageNum": 1, "pageSize": 999})
        result = data.get("result") if isinstance(data, dict) else None
        if (not isinstance(data, dict) or data.get("code") != 1 or not isinstance(result, dict)
                or not isinstance(result.get("list"), list)):
            raise RuntimeError("课堂活动查询未成功，保留课程恢复记录")
        if result.get("ocId") is not None and str(result["ocId"]) != str(value["courseId"]):
            raise RuntimeError("课堂与原课程不匹配")
        return result["list"]

    def _classroom_ended(self, value: dict) -> bool:
        # 退课后列表可能为空；空列表不是下课证据。下课同时结束的选人由活动状态判断。
        try:
            data = self._json("GET", f"{LMS_BASE}/wisdomClassroom/student/getClassroomList",
                              params={"ocId": value["courseId"], "status": "", "pageNum": 1,
                                      "pageSize": 999, "order": 0, "lang": "zh"})
            result = data.get("result") if isinstance(data, dict) else None
            return (isinstance(data, dict) and data.get("code") == 1 and isinstance(result, dict)
                    and isinstance(result.get("list"), list)
                    and any(str(item.get("id")) == str(value["classroomId"])
                            and str(item.get("status")) == "2" for item in result["list"] if isinstance(item, dict)))
        except (RuntimeError, ValueError, OSError):
            return False

    def tick(self) -> str:
        with self.lock:
            if self.error and self.pending is None:
                return self.error
            if self.pending:
                if self.user_id() != self.pending["userId"]:
                    raise RuntimeError("存在待恢复课程，请登录原学生账号")
                if self.pending["phase"] in ("restore_pending", "join_pending", "quit_pending"):
                    return self._restore()
                items = self._activities(self.pending)
                targets = {int(item["relationId"]): item for item in items if isinstance(item, dict)
                           and str(item.get("relationId", "")).isdigit()}
                additional = [int(item["relationId"]) for item in items if isinstance(item, dict)
                              and self.active_selection(item) and str(item.get("relationId", "")).isdigit()
                              and int(item["relationId"]) not in self.pending["activityIds"]]
                if additional:
                    self.pending["activityIds"].extend(additional)
                    self._save()
                watched = self.pending["activityIds"]
                ended = all(key in targets and str(targets[key].get("status")) == "2" for key in watched)
                if ended or self._classroom_ended(self.pending):
                    self.pending["phase"] = "restore_pending"
                    self._save()
                    return self._restore()
                return "已临时退出课程，等待选人活动或课堂结束"
            if not self.context:
                return "等待课堂活动监测"
            if self._stopped:
                self.context = None
                return "课堂活动监测已停止"
            if self._classroom_ended(self.context):
                self.context = None
                return "课堂已结束，活动监测已完成"
            selections = [item for item in self._activities(self.context)
                          if isinstance(item, dict) and self.active_selection(item)
                          and str(item.get("relationId", "")).isdigit() and int(item["relationId"]) > 0
                          and int(item["relationId"]) not in self.context.get("handledActivityIds", [])]
            if not selections:
                return "签到已完成，正在监测选人活动"
            uid = self._identity(self.context["userId"])
            if self._class_id(self.context["courseId"]) != self.context["classId"]:
                raise RuntimeError("当前选课关系已变化，未自动退课")
            if self._stopped:
                self.context = None
                return "课堂活动监测已停止"
            ids = [int(item["relationId"]) for item in selections]
            self.pending = dict(self.context, userId=uid, activityIds=ids, phase="quit_pending")
            try:
                self._save()
            except OSError:
                self.pending = None
                raise RuntimeError("无法保存课程恢复记录，未自动退课") from None
            self.context["handledActivityIds"] = self.context.get("handledActivityIds", []) + ids
            if self._stopped:
                self.pending["phase"] = "restore_pending"
                self._save()
                return self._restore()
            # 写请求超时也保留 quit_pending；下一轮只核对并恢复，绝不重复退课。
            response = self.request("PUT", f"{LMS_BASE}/courses/{self.pending['courseId']}/quitCourse")
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"退课接口返回 HTTP {response.status_code}，将核对并恢复原班级")
            cid = self._class_id(self.pending["courseId"])
            if cid is not None:
                self.pending["phase"] = "restore_pending"
                self._save()
                return self._restore()
            self.pending["phase"] = "withdrawn"
            self._save()
            self.log(f"[{self.pending['courseName']}] 发现选人活动，已临时退出课程；结束后自动恢复原班级。", "warn")
            return "已临时退出课程，等待选人活动或课堂结束"

    def _restore(self) -> str:
        value = self.pending
        if value is None:
            return "没有待恢复课程"
        self._identity(value["userId"])
        current = self._class_id(value["courseId"])
        if current == value["classId"]:
            # 原班级归属才是恢复成功，HTTP 200/申请成功均不够。
            self.path.unlink(missing_ok=True)
            self.pending = None
            self.error = ""
            self._retry_at = 0.0
            if self._stopped or self._classroom_ended(value):
                self.context = None
            self.log(f"[{value['courseName']}] 已确认恢复原班级。", "success")
            return "已恢复原班级" if not self.context else "已恢复原班级，继续监测选人活动"
        if current is not None:
            raise RuntimeError("账号已在其他班级，保留记录，请手动恢复原班级")
        if value["phase"] == "join_pending":
            return "加课申请待确认，请在平台查看审核结果；仍保留恢复记录"
        if time.monotonic() < self._retry_at:
            return "课程尚未恢复，等待重试"
        value["phase"] = "restore_pending"
        self._save()
        self._retry_at = time.monotonic() + 30
        data = self._json("POST", f"{LMS_BASE}/classes/{value['classId']}/user/addClass",
                          params={"ocId": value["courseId"]}, json=[value["userId"]])
        if not isinstance(data, dict) or data.get("code") not in (1, 2):
            raise RuntimeError("服务器未确认加入原班级，将保留记录并重试")
        value["phase"] = "join_pending"
        self._save()
        if self._class_id(value["courseId"]) == value["classId"]:
            return self._restore()
        return "加课结果待确认，仍保留课程恢复记录"

    def stop(self) -> None:
        self._stopped = True
        with self.lock:
            self.context = None
            if self.pending:
                if self.pending["phase"] != "join_pending":
                    self.pending["phase"] = "restore_pending"
                self._save()

    def reset(self) -> None:
        with self.lock:
            self._stopped = False
            self.context = None
