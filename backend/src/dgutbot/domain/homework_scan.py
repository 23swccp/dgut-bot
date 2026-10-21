"""全课程作业扫描的纯解析器与有界后台服务。"""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Callable


CACHE_SECONDS = 300
HOMEWORK_STATES = {0: "未提交", 1: "未互评", 2: "互评中", 4: "已批阅"}
PENDING_STATES = frozenset({0, 1})
PEER_REVIEW_STATES = frozenset({1, 2})


class HomeworkAuthRequiredError(RuntimeError):
    pass


@dataclass(frozen=True)
class Homework:
    id: str
    homeworkId: str
    courseId: str
    courseName: str
    teacherName: str
    title: str
    state: int | None
    stateLabel: str
    pending: bool
    needsAction: bool
    archived: bool
    endTime: str
    url: str

    def mapping(self) -> dict[str, Any]:
        return asdict(self)


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000
            return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")
        normalized = str(value).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, TypeError, ValueError):
        return str(value).strip()


def _homework_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for container in (payload, payload.get("result"), payload.get("data")):
        if not isinstance(container, dict):
            continue
        for key in ("homeworkList", "list", "records"):
            items = container.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def homework_state_label(state: int | None) -> str:
    """平台已知状态使用固定文案，未知值保留原始数值，缺失值单独兜底。"""
    if state is None:
        return "未知状态"
    return HOMEWORK_STATES.get(state, f"其它状态（{state}）")


def homework_items(course: Any, payload: Any) -> list[Homework]:
    """提取课程中的全部作业，并标记待处理与互评语义。"""
    course_id = str(getattr(course, "id", "") or (course.get("id") if isinstance(course, dict) else ""))
    course_name = str(getattr(course, "name", "") or (course.get("name") if isinstance(course, dict) else "") or "未命名课程")
    teacher = str(getattr(course, "teacher_name", "") or (course.get("teacherName") if isinstance(course, dict) else "") or "未知教师")
    url = f"https://lms.dgut.edu.cn/courseweb/ulearning/index.html#/course/homework?courseId={course_id}"
    found: list[Homework] = []
    seen: set[str] = set()
    for item in _homework_list(payload):
        state = _integer(item.get("state"))
        homework_id = str(item.get("id") or item.get("homeworkId") or item.get("relationId") or "")
        title = str(item.get("homeworkTitle") or item.get("title") or item.get("name") or "未命名作业").strip()
        identity = f"{course_id}|{homework_id or title}"
        stable_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        if stable_id in seen:
            continue
        seen.add(stable_id)
        found.append(Homework(
            id=stable_id,
            homeworkId=homework_id,
            courseId=course_id,
            courseName=course_name,
            teacherName=teacher,
            title=title,
            state=state,
            stateLabel=homework_state_label(state),
            pending=state in PENDING_STATES,
            needsAction=state == 1,
            archived=False,
            endTime=_iso_time(item.get("endTime") or item.get("deadline") or item.get("dueTime")),
            url=url,
        ))
    return found


def _deadline_timestamp(value: str) -> float:
    if not value:
        return float("inf")
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except (OSError, OverflowError, TypeError, ValueError):
        return float("inf")


def homework_sort_key(item: Homework) -> tuple[int, float, str]:
    """待处理优先，其次按截止时间升序，无截止时间置后，最后按标题稳定排序。"""
    return (0 if item.pending else 1, _deadline_timestamp(item.endTime), item.title)


def sort_homework_items(items: list[Homework]) -> list[Homework]:
    return sorted(items, key=homework_sort_key)


class HomeworkScanService:
    """单实例、可取消、最多四路并发且带五分钟内存缓存的扫描服务。"""

    def __init__(
        self,
        courses: Callable[[], list[Any]],
        homeworks: Callable[[Any], Any],
        authenticated: Callable[[], bool],
        redact: Callable[[object], str],
        *,
        max_workers: int = 3,
        archives: Callable[[], tuple[set[str], set[str]]] | None = None,
    ) -> None:
        self._courses = courses
        self._homeworks = homeworks
        self._authenticated = authenticated
        self._redact = redact
        self._archives = archives or (lambda: (set(), set()))
        self._max_workers = max(1, min(4, max_workers))
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot = self._empty()
        self._items: list[Homework] = []
        self._cache_monotonic = 0.0

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "state": "idle", "loggedIn": False, "scannedCourses": 0, "totalCourses": 0,
            "totalCount": 0, "pendingCount": 0, "peerReviewCount": 0, "pendingReviewCount": 0,
            "completedCount": 0, "archivedCount": 0, "groups": [], "failures": [],
            "cachedAt": "", "fromCache": False, "error": "",
        }

    @staticmethod
    def _count(items: list[Homework]) -> dict[str, int]:
        active = [item for item in items if not item.archived]
        return {
            "totalCount": len(active),
            "pendingCount": sum(item.pending for item in active),
            "peerReviewCount": sum(item.state in PEER_REVIEW_STATES for item in active),
            "pendingReviewCount": sum(item.needsAction for item in active),
            "completedCount": sum(item.state == 4 for item in active),
            "archivedCount": sum(item.archived for item in items),
        }

    def _apply_archive(self, items: list[Homework]) -> list[Homework]:
        courses, archived_items = self._archives()
        return [
            replace(item, archived=(item.courseId in courses or item.id in archived_items))
            for item in items
        ]

    def _groups(self, items: list[Homework]) -> list[dict[str, Any]]:
        courses, _ = self._archives()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in items:
            grouped.setdefault((item.courseId, item.courseName), []).append(item.mapping())
        return [
            {"courseId": key[0], "courseName": key[1], "courseArchived": key[0] in courses, "items": value}
            for key, value in grouped.items()
        ]

    def refresh_archive(self) -> dict[str, Any]:
        """归档变化后在不重新扫描的情况下更新现有快照。"""
        with self._lock:
            if self._snapshot["state"] == "scanning" or not self._items:
                return self.snapshot()
            ordered = sort_homework_items(self._apply_archive(self._items))
            self._items = ordered
            self._snapshot.update(groups=self._groups(ordered), **self._count(ordered))
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            value = dict(self._snapshot)
            value["groups"] = [dict(group, items=[dict(item) for item in group["items"]]) for group in self._snapshot["groups"]]
            value["failures"] = [dict(item) for item in self._snapshot["failures"]]
            return value

    def item(self, item_id: str) -> dict[str, Any] | None:
        for group in self.snapshot()["groups"]:
            found = next((item for item in group["items"] if item["id"] == item_id), None)
            if found:
                return found
        return None

    def start(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.snapshot()
            if not force and self._cache_monotonic and time.monotonic() - self._cache_monotonic < CACHE_SECONDS:
                self._snapshot["fromCache"] = True
                return self.snapshot()
            if not self._authenticated():
                self._items = []
                self._snapshot = self._empty() | {"state": "auth_required", "error": "登录状态不可用，请重新登录"}
                return self.snapshot()
            self._cancel = threading.Event()
            self._items = []
            self._snapshot = self._empty() | {"state": "scanning", "loggedIn": True}
            self._thread = threading.Thread(target=self._run, name="homework-scan", daemon=True)
            self._thread.start()
            return self.snapshot()

    def cancel(self) -> dict[str, Any]:
        self._cancel.set()
        with self._lock:
            if self._snapshot["state"] == "scanning":
                self._snapshot["state"] = "cancelled"
        return self.snapshot()

    def _run(self) -> None:
        courses = list(self._courses())
        with self._lock:
            self._snapshot["totalCourses"] = len(courses)
        if not courses:
            self._finish([], [], "completed")
            return
        items: list[Homework] = []
        failures: list[dict[str, str]] = []
        pool = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="homework-list")
        aborted = False
        try:
            futures = {pool.submit(self._homeworks, course): course for course in courses}
            for future in as_completed(futures):
                course = futures[future]
                if self._cancel.is_set():
                    aborted = True
                    for pending in futures:
                        pending.cancel()
                    self._finish(items, failures, "cancelled")
                    return
                try:
                    items.extend(self._apply_archive(homework_items(course, future.result())))
                except HomeworkAuthRequiredError as error:
                    aborted = True
                    failures.append({"courseId": str(course.id), "courseName": course.name, "reason": self._redact(error)})
                    self._cancel.set()
                    for pending in futures:
                        pending.cancel()
                    self._finish(items, failures, "auth_required", "登录已失效，请重新登录")
                    return
                except Exception as error:
                    failures.append({"courseId": str(course.id), "courseName": course.name, "reason": self._redact(error)})
                with self._lock:
                    self._snapshot["scannedCourses"] += 1
                    self._snapshot.update(self._count(items))
        finally:
            pool.shutdown(wait=not aborted, cancel_futures=aborted)
        if self._cancel.is_set():
            self._finish(items, failures, "cancelled")
            return
        state = "error" if failures and len(failures) == len(courses) else "completed"
        self._finish(items, failures, state, "所有课程作业均读取失败" if state == "error" else "")

    def _finish(self, items: list[Homework], failures: list[dict[str, str]], state: str, error: str = "") -> None:
        ordered = sort_homework_items(self._apply_archive(items))
        with self._lock:
            self._items = ordered
            self._snapshot.update(
                state=state, groups=self._groups(ordered), failures=failures, **self._count(ordered),
                cachedAt=datetime.now().astimezone().isoformat(timespec="seconds"), fromCache=False,
                error=self._redact(error), loggedIn=state != "auth_required",
            )
            if state in {"completed", "error"}:
                self._cache_monotonic = time.monotonic()
