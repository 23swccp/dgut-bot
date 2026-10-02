"""全课程互评作业扫描的纯解析器与有界后台服务。"""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable


CACHE_SECONDS = 300
PEER_REVIEW_STATES = {1: "未互评", 2: "互评中"}


class HomeworkAuthRequiredError(RuntimeError):
    pass


@dataclass(frozen=True)
class PeerReviewHomework:
    id: str
    homeworkId: str
    courseId: str
    courseName: str
    teacherName: str
    title: str
    state: int
    stateLabel: str
    needsAction: bool
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


def peer_review_homeworks(course: Any, payload: Any) -> list[PeerReviewHomework]:
    """提取平台明确标为“未互评”或“互评中”的作业。"""
    course_id = str(getattr(course, "id", "") or (course.get("id") if isinstance(course, dict) else ""))
    course_name = str(getattr(course, "name", "") or (course.get("name") if isinstance(course, dict) else "") or "未命名课程")
    teacher = str(getattr(course, "teacher_name", "") or (course.get("teacherName") if isinstance(course, dict) else "") or "未知教师")
    url = f"https://lms.dgut.edu.cn/courseweb/ulearning/index.html#/course/homework?courseId={course_id}"
    found: list[PeerReviewHomework] = []
    seen: set[str] = set()
    for item in _homework_list(payload):
        state = _integer(item.get("state"))
        if state not in PEER_REVIEW_STATES:
            continue
        homework_id = str(item.get("id") or item.get("homeworkId") or item.get("relationId") or "")
        title = str(item.get("homeworkTitle") or item.get("title") or item.get("name") or "未命名作业").strip()
        identity = f"{course_id}|{homework_id or title}"
        stable_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        if stable_id in seen:
            continue
        seen.add(stable_id)
        found.append(PeerReviewHomework(
            id=stable_id,
            homeworkId=homework_id,
            courseId=course_id,
            courseName=course_name,
            teacherName=teacher,
            title=title,
            state=state,
            stateLabel=PEER_REVIEW_STATES[state],
            needsAction=state == 1,
            endTime=_iso_time(item.get("endTime") or item.get("deadline") or item.get("dueTime")),
            url=url,
        ))
    return found


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
    ) -> None:
        self._courses = courses
        self._homeworks = homeworks
        self._authenticated = authenticated
        self._redact = redact
        self._max_workers = max(1, min(4, max_workers))
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot = self._empty()
        self._cache_monotonic = 0.0

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "state": "idle", "loggedIn": False, "scannedCourses": 0, "totalCourses": 0,
            "peerReviewCount": 0, "pendingReviewCount": 0, "groups": [], "failures": [],
            "cachedAt": "", "fromCache": False, "error": "",
        }

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
                self._snapshot = self._empty() | {"state": "auth_required", "error": "登录状态不可用，请重新登录"}
                return self.snapshot()
            self._cancel = threading.Event()
            self._snapshot = self._empty() | {"state": "scanning", "loggedIn": True}
            self._thread = threading.Thread(target=self._run, name="homework-peer-review-scan", daemon=True)
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
        items: list[PeerReviewHomework] = []
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
                    items.extend(peer_review_homeworks(course, future.result()))
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
                    self._snapshot["peerReviewCount"] = len(items)
                    self._snapshot["pendingReviewCount"] = sum(item.needsAction for item in items)
        finally:
            pool.shutdown(wait=not aborted, cancel_futures=aborted)
        if self._cancel.is_set():
            self._finish(items, failures, "cancelled")
            return
        state = "error" if failures and len(failures) == len(courses) else "completed"
        self._finish(items, failures, state, "所有课程作业均读取失败" if state == "error" else "")

    def _finish(self, items: list[PeerReviewHomework], failures: list[dict[str, str]], state: str, error: str = "") -> None:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in items:
            groups.setdefault((item.courseId, item.courseName), []).append(item.mapping())
        mapped = [{"courseId": key[0], "courseName": key[1], "items": value} for key, value in groups.items()]
        with self._lock:
            self._snapshot.update(
                state=state, groups=mapped, failures=failures, peerReviewCount=len(items),
                pendingReviewCount=sum(item.needsAction for item in items),
                cachedAt=datetime.now().astimezone().isoformat(timespec="seconds"), fromCache=False,
                error=self._redact(error), loggedIn=state != "auth_required",
            )
            if state in {"completed", "error"}:
                self._cache_monotonic = time.monotonic()
