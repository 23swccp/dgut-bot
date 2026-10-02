"""未完成课件扫描的纯解析器与有界后台服务。

平台字段仍可能随租户版本变化；所有兼容字段集中在本模块，调用方不猜测
目录接口。扫描器只接收已经由 Chromium 同源请求桥取得的课程与目录数据。
"""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit


CACHE_SECONDS = 300
TREE_KEYS = ("chapters", "chapterList", "sections", "sectionList", "pages", "pageList", "children", "nodes", "items", "list", "result", "data", "content", "records")
TITLE_KEYS = ("name", "title", "pageName", "nodeName", "resourceName", "coursewareName", "currentUnit", "currentActivity")
URL_KEYS = ("url", "href", "learnUrl", "learningUrl", "coursewareUrl", "pageUrl")
ID_KEYS = ("pageId", "nodeId", "resourceId", "coursewareId", "id")


class AuthRequiredError(RuntimeError):
    pass


class DirectorySampleRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class LessonItem:
    id: str
    courseId: str
    ocId: str
    classId: str
    courseName: str
    teacherName: str
    chapterPath: list[str]
    title: str
    type: str
    completionStatus: str
    completionPercent: float | None
    nodeId: str
    chapterId: str
    pageId: str
    url: str
    canAutoOpen: bool
    unavailableReason: str

    def mapping(self) -> dict[str, Any]:
        return asdict(self)


def _value(data: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "completed", "complete", "finished"}:
            return True
        if lowered in {"false", "0", "no", "unfinished", "incomplete"}:
            return False
    return None


def completion_state(data: dict[str, Any]) -> tuple[str, float | None]:
    """集中识别完成状态；未知状态保守地作为未开始保留。"""
    percent: float | None = None
    for key in ("completionPercent", "progress", "progressRate", "percent", "studyProgress"):
        raw = data.get(key)
        try:
            if isinstance(raw, str):
                raw = raw.strip().rstrip("%")
            candidate = float(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= candidate <= 1 and key != "percent":
            candidate *= 100
        percent = max(0.0, min(100.0, candidate))
        break
    for key in ("completed", "isCompleted", "finished", "isFinished", "recordComplete"):
        parsed = _bool(data.get(key))
        if parsed is True:
            return "completed", 100.0 if percent is None else percent
        if parsed is False:
            return ("in_progress" if percent and percent > 0 else "not_started"), percent
    status = str(_value(data, ("completionStatus", "studyStatus", "learnStatus", "recordStatus", "status")) or "").strip().lower()
    if status in {"2", "completed", "complete", "finished", "已完成"} or percent == 100:
        return "completed", 100.0 if percent is None else percent
    if status in {"1", "learning", "studying", "in_progress", "进行中"} or (percent is not None and percent > 0):
        return "in_progress", percent
    return "not_started", percent


def _hidden_or_closed(data: dict[str, Any]) -> str:
    for key in ("hidden", "isHidden", "isHide", "disabled", "noPermission"):
        if _bool(data.get(key)) is True:
            return "课件已隐藏或无访问权限"
    for key in ("opened", "isOpen", "available", "published", "isPublished"):
        if key in data and _bool(data.get(key)) is False:
            return "课件尚未开放"
    return ""


def _real_learn_url(data: dict[str, Any]) -> str:
    for key in URL_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parts = urlsplit(value.strip())
        except ValueError:
            continue
        if parts.scheme == "https" and parts.hostname == "ua.dgut.edu.cn" and "/learnCourse" in parts.path:
            return value.strip()
    return ""


def flatten_lessons(course: Any, payload: Any) -> list[LessonItem]:
    """把深层章节树展平、过滤并稳定去重。"""
    course_id = str(getattr(course, "id", "") or (course.get("id") if isinstance(course, dict) else ""))
    course_name = str(getattr(course, "name", "") or (course.get("name") if isinstance(course, dict) else "") or "未命名课程")
    teacher = str(getattr(course, "teacher_name", "") or (course.get("teacherName") if isinstance(course, dict) else "") or "未知教师")
    results: list[LessonItem] = []
    seen: set[str] = set()

    def walk(value: Any, path: list[str], inherited_class_id: str = "") -> None:
        if isinstance(value, list):
            for child in value:
                walk(child, path, inherited_class_id)
            return
        if not isinstance(value, dict):
            return
        title = str(_value(value, TITLE_KEYS) or "").strip()
        class_id = str(_value(value, ("classId", "classID", "classroomId")) or inherited_class_id or "")
        children: list[Any] = []
        for key in TREE_KEYS:
            child = value.get(key)
            if isinstance(child, (list, dict)):
                children.append(child)
        item_course_id = str(_value(value, ("courseId",)) or course_id)
        node_id = str(_value(value, ("nodeId", "nodeID", "resourceId", "coursewareId")) or "")
        chapter_id = str(_value(value, ("chapterId", "currentUnitID")) or "")
        page_id = str(_value(value, ("pageId",)) or "")
        url = _real_learn_url(value)
        kind = str(_value(value, ("typeName", "resourceType", "contentType", "pageType", "type")) or "未知")
        looks_like_lesson = bool(url or node_id or chapter_id or value.get("pageId") is not None or value.get("resourceType") is not None or value.get("contentType") is not None)
        unavailable = _hidden_or_closed(value)
        if unavailable:
            return
        if looks_like_lesson and title and not unavailable:
            state, percent = completion_state(value)
            if state != "completed":
                strong_identity = "|".join((class_id, node_id, chapter_id, page_id, url)).strip("|")
                identity = "|".join((item_course_id, strong_identity or f"{title}|{'/'.join(path)}"))
                stable_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                if stable_id not in seen:
                    seen.add(stable_id)
                    results.append(LessonItem(
                        id=stable_id, courseId=item_course_id, ocId=course_id, classId=class_id,
                        courseName=course_name, teacherName=teacher, chapterPath=path,
                        title=title, type=kind, completionStatus=state,
                        completionPercent=percent, nodeId=node_id, pageId=page_id,
                        chapterId=chapter_id, url=url, canAutoOpen=bool(url),
                        unavailableReason="" if url else "平台响应未提供已验证的真实课件地址",
                    ))
        next_path = path + [title] if title and (children or not looks_like_lesson) else path
        for child in children:
            walk(child, next_path, class_id)

    walk(payload, [])
    return results


class CourseScanService:
    """单实例、可取消、最多四路并发且带五分钟内存缓存的扫描服务。"""

    def __init__(
        self,
        courses: Callable[[], list[Any]],
        directory: Callable[[Any], Any],
        authenticated: Callable[[], bool],
        redact: Callable[[object], str],
        *,
        max_workers: int = 3,
    ) -> None:
        self._courses = courses
        self._directory = directory
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
        return {"state": "idle", "loggedIn": False, "scannedCourses": 0, "totalCourses": 0,
                "unfinishedCount": 0, "groups": [], "failures": [], "cachedAt": "", "fromCache": False,
                "error": "", "openPhase": "idle", "openError": "", "selectedId": ""}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            value = dict(self._snapshot)
            value["groups"] = [dict(group, items=[dict(item) for item in group["items"]]) for group in self._snapshot["groups"]]
            value["failures"] = [dict(item) for item in self._snapshot["failures"]]
            return value

    def set_open_state(self, phase: str, *, selected_id: str = "", error: str = "") -> None:
        with self._lock:
            self._snapshot.update(openPhase=phase, selectedId=selected_id or self._snapshot.get("selectedId", ""), openError=self._redact(error))

    def reset_open_state(self) -> None:
        """退出当前刷课流程，并清除课件选择和打开错误。"""
        with self._lock:
            self._snapshot.update(openPhase="idle", selectedId="", openError="")

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
            self._thread = threading.Thread(target=self._run, name="unfinished-course-scan", daemon=True)
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
        items: list[LessonItem] = []
        failures: list[dict[str, str]] = []
        auth_failed = False
        pool = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="course-directory")
        aborted = False
        try:
            futures = {pool.submit(self._directory, course): course for course in courses}
            for future in as_completed(futures):
                course = futures[future]
                if self._cancel.is_set():
                    aborted = True
                    for pending in futures:
                        pending.cancel()
                    self._finish(items, failures, "cancelled")
                    return
                try:
                    payload = future.result()
                    items.extend(flatten_lessons(course, payload))
                except AuthRequiredError as error:
                    auth_failed = True
                    failures.append({"courseId": str(course.id), "courseName": course.name, "reason": self._redact(error)})
                except Exception as error:  # 单门课程失败不终止其余课程
                    failures.append({"courseId": str(course.id), "courseName": course.name, "reason": self._redact(error)})
                with self._lock:
                    self._snapshot["scannedCourses"] += 1
                    self._snapshot["unfinishedCount"] = len(items)
                if auth_failed:
                    aborted = True
                    self._cancel.set()
                    for pending in futures:
                        pending.cancel()
                    self._finish(items, failures, "auth_required", "登录已失效，请重新登录")
                    return
        finally:
            pool.shutdown(wait=not aborted, cancel_futures=aborted)
        if self._cancel.is_set():
            self._finish(items, failures, "cancelled")
            return
        state = "error" if failures and len(failures) == len(courses) else "completed"
        self._finish(items, failures, state, "所有课程目录均读取失败" if state == "error" else "")

    def _finish(self, items: list[LessonItem], failures: list[dict[str, str]], state: str, error: str = "") -> None:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in items:
            groups.setdefault((item.courseId, item.courseName), []).append(item.mapping())
        mapped = [{"courseId": key[0], "courseName": key[1], "items": value} for key, value in groups.items()]
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            self._snapshot.update(state=state, groups=mapped, failures=failures,
                                  unfinishedCount=len(items), cachedAt=now, fromCache=False,
                                  error=self._redact(error), loggedIn=state != "auth_required")
            if state in {"completed", "error"}:
                self._cache_monotonic = time.monotonic()
