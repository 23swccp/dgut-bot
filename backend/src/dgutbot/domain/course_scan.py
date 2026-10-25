"""未完成课件扫描的纯解析器与有界后台服务。

平台字段仍可能随租户版本变化；所有兼容字段集中在本模块，调用方不猜测
目录接口。扫描器只接收已经由 Chromium 同源请求桥取得的课程与目录数据。
"""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime
from queue import Empty, Queue
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


class ScanCancelled(RuntimeError):
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
        directory_session: Callable[[threading.Event], AbstractContextManager[Callable[[Any], Any]]] | None = None,
        cache_context: Callable[[], object] = lambda: None,
        log: Callable[[str, str], None] = lambda *_: None,
    ) -> None:
        self._courses = courses
        self._directory = directory
        self._authenticated = authenticated
        self._redact = redact
        self._max_workers = max(1, min(4, max_workers))
        self._directory_session = directory_session
        self._cache_context = cache_context
        self._context = cache_context()
        self._log = log
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot = self._empty()
        self._cache: dict[str, tuple[float, list[LessonItem]]] = {}
        self._results: dict[str, list[LessonItem]] = {}
        self._course_order: list[str] = []

    def _check_context(self) -> None:
        """缓存和迟到结果只能用于产生它们的登录会话。调用方持有锁。"""
        context = self._cache_context()
        if context != self._context:
            self._cancel.set()
            self._context = context
            self._cache.clear()
            self._results.clear()
            self._snapshot = self._empty() | {"state": "auth_required"}

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"state": "idle", "loggedIn": False, "scannedCourses": 0, "totalCourses": 0,
                "unfinishedCount": 0, "groups": [], "failures": [], "cachedAt": "", "fromCache": False,
                "error": "", "openPhase": "idle", "openError": "", "selectedId": ""}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._check_context()
            value = dict(self._snapshot)
            value["stopping"] = bool(self._thread and self._thread.is_alive() and self._cancel.is_set())
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
            self._check_context()
            if self._thread and self._thread.is_alive():
                return self.snapshot()
            if not self._authenticated():
                self._cache.clear()
                self._results.clear()
                self._snapshot = self._empty() | {"state": "auth_required", "error": "登录状态不可用，请重新登录"}
                return self.snapshot()
            courses = list(self._courses())
            self._course_order = list(dict.fromkeys(str(course.id) for course in courses))
            self._cache = {key: value for key, value in self._cache.items() if key in self._course_order}
            # 刷新时先保留上次成功的列表，随后逐门替换，避免整个界面被清空。
            self._results = {key: value[1] for key, value in self._cache.items()}
            now = time.monotonic()
            pending = [course for course in courses if force or str(course.id) not in self._cache
                       or now - self._cache[str(course.id)][0] >= CACHE_SECONDS]
            cached_count = len(courses) - len(pending)
            self._cancel = threading.Event()
            open_state = {key: self._snapshot[key] for key in ("openPhase", "selectedId", "openError")}
            self._snapshot = self._empty() | open_state | {
                "state": "scanning" if pending else "completed", "loggedIn": True,
                "totalCourses": len(courses), "scannedCourses": cached_count,
                "fromCache": bool(cached_count),
            }
            self._publish()
            if not pending:
                self._snapshot["cachedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
                return self.snapshot()
            self._thread = threading.Thread(target=self._run, args=(pending, self._cancel, self._context),
                                            name="unfinished-course-scan", daemon=True)
            self._thread.start()
            return self.snapshot()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            self._cancel.set()
            if self._snapshot["state"] == "scanning":
                self._snapshot["state"] = "cancelled"
        return self.snapshot()

    def _run(self, courses: list[Any], cancel: threading.Event, context: object) -> None:
        started = time.monotonic()
        jobs: Queue[Any] = Queue()
        for course in courses:
            jobs.put(course)
        try:
            # 每个 worker 领取下一门课程，整个批次只创建最多 max_workers 个连接。
            with ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="course-directory") as pool:
                futures = [pool.submit(self._worker, jobs, cancel, context)
                           for _ in range(min(self._max_workers, len(courses)))]
                for future in futures:
                    future.result()
        except Exception as error:
            with self._lock:
                if not cancel.is_set() and context == self._cache_context():
                    self._snapshot.update(state="error", error=self._redact(error))
            self._log(self._redact(error), "warn")
            return
        with self._lock:
            if context != self._cache_context():
                self._check_context()
                return
            if cancel.is_set():
                return
            failed = len(self._snapshot["failures"])
            state = "error" if failed == self._snapshot["totalCourses"] else "completed"
            self._snapshot.update(state=state, cachedAt=datetime.now().astimezone().isoformat(timespec="seconds"),
                                  error="所有课程目录均读取失败" if state == "error" else "")
            count = self._snapshot["unfinishedCount"]
        self._log(f"课件目录扫描完成：发现 {count} 个未完成课件，{failed} 门课程失败，用时 {time.monotonic() - started:.1f} 秒", "info")

    def _worker(self, jobs: Queue[Any], cancel: threading.Event, context: object) -> None:
        session = self._directory_session(cancel) if self._directory_session else nullcontext(self._directory)
        with session as directory:
            while not cancel.is_set() and context == self._cache_context():
                try:
                    course = jobs.get_nowait()
                except Empty:
                    return
                items = None
                failure = None
                auth_failed = False
                try:
                    items = flatten_lessons(course, directory(course))
                except ScanCancelled:
                    return
                except Exception as error:  # 单门课程失败不终止其余课程。
                    auth_failed = isinstance(error, AuthRequiredError)
                    failure = {"courseId": str(course.id), "courseName": course.name, "reason": self._redact(error)}
                with self._lock:
                    if cancel.is_set() or context != self._cache_context():
                        return
                    key = str(course.id)
                    if items is not None:
                        self._results[key] = items
                        self._cache[key] = (time.monotonic(), items)
                    else:
                        self._results.pop(key, None)
                        self._cache.pop(key, None)
                        self._snapshot["failures"].append(failure)
                    self._snapshot["scannedCourses"] += 1
                    if auth_failed:
                        cancel.set()
                        self._cache.clear()
                        self._results.clear()
                        self._snapshot.update(state="auth_required", loggedIn=False, error="登录已失效，请重新登录")
                    self._publish()
                if failure:
                    self._log(f"{course.name}：{failure['reason']}", "warn")

    def _publish(self) -> None:
        """持锁发布每门课的结果，按原课程顺序排列。"""
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for key in self._course_order:
            for item in self._results.get(key, []):
                groups.setdefault((item.courseId, item.courseName), []).append(item.mapping())
        mapped = [{"courseId": key[0], "courseName": key[1], "items": value} for key, value in groups.items()]
        self._snapshot.update(groups=mapped, unfinishedCount=sum(len(group["items"]) for group in mapped))
