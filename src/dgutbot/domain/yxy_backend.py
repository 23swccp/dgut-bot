"""桌面 UI 的本地后端：浏览器登录、课程读取和课堂活动轮询。"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlencode, urlsplit

import requests
from websocket import create_connection

from dgutbot.app.browser_paths import (
    BROWSER_INSTALLATIONS, browser_scan_roots, extra_browser_candidates,
    normalize_browser_path, resolve_browser_path, scan_browser_directory,
)


LMS_BASE = "https://lms.dgut.edu.cn/courseapi"
APP_BASE = "https://application.dgut.edu.cn/classroomapi"


@dataclass
class AppConfig:
    browser_path: str = ""
    browser_name: str = ""
    debug_port: int = 9222
    poll_interval: int = 5
    save_log: bool = True
    log_path: str = ""
    lat: float = 23.0432
    lng: float = 113.3993
    address: str = "东莞理工学院"
    # 课件学习辅助：播放、文档阅读、章节衔接与测验自动作答（实验性，占位选项）。
    course_playback_rate: float = 8.0
    course_auto_dismiss_dialog: bool = True
    course_document_scroll_enabled: bool = True
    course_document_scroll_interval: float = 3.0
    course_document_scroll_speed: float = 3.0
    course_quiz_auto_answer: bool = False
    course_quiz_choice_enabled: bool = True
    course_quiz_judgment_enabled: bool = True
    course_quiz_blank_enabled: bool = True
    course_ai_model_id: int = 1

    @staticmethod
    def _number(value, default: float, minimum: float, maximum: float, *, integer: bool = False):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        if not math.isfinite(number):
            number = default
        number = min(max(number, minimum), maximum)
        return int(number) if integer else number

    @staticmethod
    def _boolean(value, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        return default

    @classmethod
    def from_mapping(cls, values: dict, root: Path) -> "AppConfig":
        defaults = cls(log_path=str(root / "签到记录.md"))
        if not isinstance(values, dict):
            values = {}
        merged = asdict(defaults) | {
            key: value for key, value in values.items() if key in defaults.__dataclass_fields__
        }
        merged.update(
            debug_port=cls._number(merged["debug_port"], defaults.debug_port, 1024, 65535, integer=True),
            poll_interval=cls._number(merged["poll_interval"], defaults.poll_interval, 2, 3600, integer=True),
            save_log=cls._boolean(merged["save_log"], defaults.save_log),
            lat=cls._number(merged["lat"], defaults.lat, -90, 90),
            lng=cls._number(merged["lng"], defaults.lng, -180, 180),
            course_playback_rate=cls._number(merged["course_playback_rate"], defaults.course_playback_rate, 1, 16),
            course_auto_dismiss_dialog=cls._boolean(merged["course_auto_dismiss_dialog"], defaults.course_auto_dismiss_dialog),
            course_document_scroll_enabled=cls._boolean(merged["course_document_scroll_enabled"], defaults.course_document_scroll_enabled),
            course_document_scroll_interval=cls._number(merged["course_document_scroll_interval"], defaults.course_document_scroll_interval, 0.5, 60),
            course_document_scroll_speed=cls._number(merged["course_document_scroll_speed"], defaults.course_document_scroll_speed, 1, 3),
            course_quiz_auto_answer=cls._boolean(merged["course_quiz_auto_answer"], defaults.course_quiz_auto_answer),
            course_quiz_choice_enabled=cls._boolean(merged["course_quiz_choice_enabled"], defaults.course_quiz_choice_enabled),
            course_quiz_judgment_enabled=cls._boolean(merged["course_quiz_judgment_enabled"], defaults.course_quiz_judgment_enabled),
            course_quiz_blank_enabled=cls._boolean(merged["course_quiz_blank_enabled"], defaults.course_quiz_blank_enabled),
            course_ai_model_id=cls._number(merged["course_ai_model_id"], defaults.course_ai_model_id, 1, 1_000_000, integer=True),
        )
        if not any((
            merged["course_quiz_choice_enabled"],
            merged["course_quiz_judgment_enabled"],
            merged["course_quiz_blank_enabled"],
        )):
            merged["course_quiz_auto_answer"] = False
        for key in ("browser_path", "browser_name", "log_path", "address"):
            merged[key] = str(merged[key] if merged[key] is not None else getattr(defaults, key))
        merged["browser_path"] = normalize_browser_path(merged["browser_path"])
        return cls(**merged)

    def to_mapping(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Course:
    id: int
    name: str
    teacher_name: str = "未知教师"
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_api(cls, data: dict) -> "Course":
        return cls(id=int(data["id"]), name=data.get("name", "未命名课程"), teacher_name=data.get("teacherName", "未知教师"), raw=data)


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
        return cls(relation_id=int(data["relationId"]), title=data.get("title", "未命名活动"), relation_type=data.get("relationType"), score_type=data.get("scoreType"), state=data.get("state"), status=data.get("status"), raw=data)


class MonitorState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass(frozen=True)
class BrowserResponse:
    """浏览器 fetch 响应的最小兼容对象。"""

    status_code: int
    text: str
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"浏览器请求返回 HTTP {self.status_code}")

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("服务返回了无法解析的数据") from error


class BrowserApiClient:
    """在与目标接口同源的页面内执行 fetch，复用 Chromium 网络栈与会话。"""

    LMS_ORIGIN = "https://lms.dgut.edu.cn"
    ALLOWED_ORIGINS = frozenset({LMS_ORIGIN, "https://application.dgut.edu.cn"})
    _FETCH_FUNCTION = r"""
async function(request) {
  const controller = new AbortController();
  const timer = setTimeout(function() { controller.abort(); }, request.timeoutMs);
  try {
    const options = {
      method: request.method,
      credentials: 'include',
      headers: request.headers,
      redirect: 'error',
      signal: controller.signal
    };
    if (request.body !== null) options.body = request.body;
    const response = await fetch(request.url, options);
    const text = (await response.text()).slice(0, request.maxResponseChars);
    return {
      status: response.status,
      text: text,
      contentType: response.headers.get('content-type') || ''
    };
  } catch (error) {
    return {transportError: String(error && error.name || 'FetchError')};
  } finally {
    clearTimeout(timer);
  }
}
"""

    def __init__(
        self,
        debug_port: Callable[[], int],
        headers: dict[str, str],
        *,
        auto_create: bool = True,
    ) -> None:
        self._debug_port = debug_port
        self._headers = headers
        self._auto_create = auto_create
        self._ws = None
        self._target_id = ""
        self._owned_target_ids: set[str] = set()
        self._message_id = 0
        self._lock = threading.RLock()

    @staticmethod
    def _is_origin_page(target: dict, origin: str) -> bool:
        if target.get("type") != "page":
            return False
        try:
            parts = urlsplit(str(target.get("url") or ""))
        except ValueError:
            return False
        return f"{parts.scheme}://{parts.netloc}" == origin

    def _list_targets(self) -> list[dict]:
        try:
            response = requests.get(f"http://127.0.0.1:{self._debug_port()}/json", timeout=2)
            response.raise_for_status()
            targets = response.json()
        except (requests.RequestException, ValueError, TypeError) as error:
            raise RuntimeError("无法连接调试浏览器") from error
        if not isinstance(targets, list):
            raise RuntimeError("调试浏览器返回了无效的页面列表")
        return [item for item in targets if isinstance(item, dict)]

    def _target_for_origin(self, targets: list[dict], origin: str) -> dict | None:
        return next((
            item for item in targets
            if self._is_origin_page(item, origin)
        ), None)

    def _create_origin_target(self, origin: str) -> None:
        try:
            response = requests.get(
                f"http://127.0.0.1:{self._debug_port()}/json/version", timeout=2,
            )
            response.raise_for_status()
            browser_ws_url = str(response.json().get("webSocketDebuggerUrl") or "")
        except (requests.RequestException, ValueError, TypeError, AttributeError) as error:
            raise RuntimeError("无法读取调试浏览器控制端点") from error
        if not browser_ws_url:
            raise RuntimeError("调试浏览器未提供控制端点")
        try:
            ws = create_connection(browser_ws_url, timeout=10, enable_multithread=True)
            try:
                message_id = 0

                def browser_call(method: str, params: dict) -> dict:
                    nonlocal message_id
                    message_id += 1
                    ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
                    while True:
                        message = json.loads(ws.recv())
                        if message.get("id") != message_id:
                            continue
                        if message.get("error"):
                            raise RuntimeError("浏览器拒绝恢复登录缓存或创建后台页面")
                        return message.get("result") or {}

                authorization = str(self._headers.get("Authorization") or "")
                if authorization:
                    browser_call("Storage.setCookies", {"cookies": [{
                        "name": "AUTHORIZATION",
                        "value": authorization,
                        "domain": ".dgut.edu.cn",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }]})
                result = browser_call(
                    "Target.createTarget",
                    {"url": f"{origin}/favicon.ico", "background": True},
                )
                target_id = str(result.get("targetId") or "")
                if not target_id:
                    raise RuntimeError("浏览器未返回新页面标识")
                self._owned_target_ids.add(target_id)
                return
            finally:
                ws.close()
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError("创建后台学校页面失败") from error

    def _discover_target(self, origin: str) -> dict:
        targets = self._list_targets()
        target = self._target_for_origin(targets, origin)
        if target is None and self._auto_create:
            self._create_origin_target(origin)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                targets = self._list_targets()
                target = self._target_for_origin(targets, origin)
                if target is not None:
                    break
                time.sleep(0.1)
        if not target or not target.get("webSocketDebuggerUrl"):
            host = urlsplit(origin).netloc
            raise RuntimeError(f"未找到已打开的学校页面：{host}")
        return target

    def _disconnect(self) -> None:
        ws, self._ws = self._ws, None
        self._target_id = ""
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._disconnect()
            target_ids, self._owned_target_ids = self._owned_target_ids, set()
            for target_id in target_ids:
                try:
                    requests.get(
                        f"http://127.0.0.1:{self._debug_port()}/json/close/{quote(target_id, safe='')}",
                        timeout=2,
                    )
                except requests.RequestException:
                    pass

    def _connect(self, target: dict) -> None:
        target_id = str(target.get("id") or "")
        if self._ws is not None and target_id and target_id == self._target_id:
            return
        self._disconnect()
        try:
            self._ws = create_connection(
                str(target["webSocketDebuggerUrl"]), timeout=15, enable_multithread=True,
            )
        except Exception as error:
            self._disconnect()
            raise RuntimeError("连接 LMS 页面失败") from error
        self._target_id = target_id

    def _call(self, method: str, params: dict, timeout: float = 15.0) -> dict:
        if self._ws is None:
            raise RuntimeError("浏览器请求桥尚未连接")
        self._message_id += 1
        message_id = self._message_id
        try:
            self._ws.settimeout(timeout)
            self._ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
            while True:
                message = json.loads(self._ws.recv())
                if message.get("id") != message_id:
                    continue
                if message.get("error"):
                    raise RuntimeError("浏览器拒绝执行请求")
                return message.get("result") or {}
        except RuntimeError:
            raise
        except Exception as error:
            self._disconnect()
            raise RuntimeError("浏览器请求桥连接已中断") from error

    def request(self, method: str, url: str, **kwargs) -> BrowserResponse:
        method = str(method or "GET").upper()
        if method not in {"GET", "POST"}:
            raise ValueError(f"浏览器请求桥不支持 {method}")
        try:
            parts = urlsplit(str(url))
        except ValueError as error:
            raise ValueError("浏览器请求地址无效") from error
        if f"{parts.scheme}://{parts.netloc}" not in self.ALLOWED_ORIGINS:
            raise ValueError("浏览器请求地址不在允许的学校域名内")
        params = kwargs.pop("params", None)
        payload = kwargs.pop("json", None)
        timeout = kwargs.pop("timeout", 15)
        if kwargs:
            raise TypeError(f"不支持的浏览器请求参数：{', '.join(sorted(kwargs))}")
        if params:
            query = urlencode(params, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        headers = {"Accept": "application/json, text/plain, */*"}
        authorization = str(self._headers.get("Authorization") or "")
        if authorization:
            headers["Authorization"] = authorization
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json;charset=UTF-8"
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        request_data = {
            "url": url,
            "method": method,
            "headers": headers,
            "body": body,
            "timeoutMs": max(1000, min(60000, int(float(timeout) * 1000))),
            "maxResponseChars": 1_048_576,
        }
        with self._lock:
            origin = f"{parts.scheme}://{parts.netloc}"
            target = self._discover_target(origin)
            self._connect(target)
            global_result = self._call(
                "Runtime.evaluate", {"expression": "globalThis", "returnByValue": False}, timeout=5,
            )
            object_id = (global_result.get("result") or {}).get("objectId")
            if not object_id:
                raise RuntimeError("无法取得学校页面执行环境")
            result = self._call(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": self._FETCH_FUNCTION,
                    "arguments": [{"value": request_data}],
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                timeout=max(5.0, float(timeout) + 2.0),
            )
        if result.get("exceptionDetails"):
            raise RuntimeError("学校页面执行浏览器请求失败")
        value = (result.get("result") or {}).get("value")
        if not isinstance(value, dict):
            raise RuntimeError("浏览器返回了无效的请求结果")
        if value.get("transportError"):
            raise RuntimeError(f"浏览器请求失败：{value['transportError']}")
        response = BrowserResponse(
            int(value.get("status") or 0), str(value.get("text") or ""), str(value.get("contentType") or ""),
        )
        response.raise_for_status()
        return response

    def json(self, method: str, url: str, **kwargs) -> dict:
        value = self.request(method, url, **kwargs).json()
        if not isinstance(value, dict):
            raise RuntimeError("服务返回的数据结构不符合预期")
        return value

# 开源源码不保存任何个人登录信息。发布版会在当前用户的 AppData 中保存本地缓存。
# 开发时如需临时令牌，请通过环境变量 YXY_TOKEN 提供，切勿提交到仓库。
TOKEN = os.environ.get("YXY_TOKEN", "")
USER_ID = None


class SignBackend:
    def __init__(
        self,
        emit: Callable[[str, str], None],
        root: Path | None = None,
        emit_event: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.emit = emit
        self.emit_event = emit_event
        self.root = root or Path(__file__).resolve().parents[3]
        self.config_path = self.root / "config.json"
        self.config: AppConfig = self._load_config()
        # 自动答题是一次性运行授权，不能从上一次程序会话继承。
        self.config.course_quiz_auto_answer = False
        credentials = self._load_credentials()
        self.token = credentials.get("token") or TOKEN
        cached_user_id = credentials.get("user_id")
        self.user_id: int | None = cached_user_id if isinstance(cached_user_id, int) else USER_ID
        self.courses: list[Course] = []
        self.selected_course: Course | None = None
        self.stop_event = threading.Event()
        self.browser_start_lock = threading.Lock()
        self.monitor_lock = threading.Lock()
        self.monitor_thread: threading.Thread | None = None
        self.monitor_state = MonitorState.IDLE
        self.monitor_round = 0
        self.monitor_interval = max(2, int(self.config.poll_interval))
        self.monitor_started_at = ""
        self.monitor_last_check = ""
        self.monitor_last_result = "等待首次检查"
        # 远端课程/签到请求只从这里读取 Authorization；其余网络请求头均由
        # Chromium 根据 LMS 页面上下文生成。User-Agent 仅供登录恢复/AI 兼容层使用。
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if self.token:
            self.headers["Authorization"] = self.token
        self.api = BrowserApiClient(lambda: int(self.config.debug_port), self.headers)
        self._course_controller = None
        self._course_operation_lock = threading.RLock()
        self._course_reservation_lock = threading.Lock()
        self._course_reservation = None
        self._course_starting = False

    def _load_config(self) -> AppConfig:
        values: dict = {}
        try:
            values = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        return AppConfig.from_mapping(values, self.root)

    def _load_credentials(self) -> dict:
        """读取当前用户本地保存的登录缓存，不从源码读取任何个人凭据。"""
        try:
            values = json.loads((self.root / "auth.json").read_text(encoding="utf-8"))
            return values if isinstance(values, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _account_path(self) -> Path:
        """账号密码仅作为可选的本机重新登录凭据，不写进源码或 config.json。"""
        return self.root / "account.json"

    def _load_account(self) -> dict:
        try:
            values = json.loads(self._account_path().read_text(encoding="utf-8"))
            return values if isinstance(values, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def account_login_status(self) -> dict:
        """只向前端返回是否已保存密码，绝不返回密码本身。"""
        account = self._load_account()
        return {
            "enabled": bool(account.get("enabled")),
            "username": str(account.get("username", "")),
            "has_password": bool(account.get("password")),
        }

    def update_account_login(self, username: str, password: str, enabled: bool) -> bool:
        """保存或清除可选的账号密码自动重新登录设置。"""
        path = self._account_path()
        if not enabled:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                self._log(f"清除账号登录设置失败：{error}", "warn")
                return False
            self._log("已关闭账号密码自动重新登录，并清除本机保存的账号信息。", "success")
            return True

        previous = self._load_account()
        username = username.strip() or str(previous.get("username", "")).strip()
        password = password or str(previous.get("password", ""))
        if not username or not password:
            self._log("启用账号密码自动重新登录前，请填写学号和密码。", "warn")
            return False
        try:
            self._write_json_atomic(
                path,
                {"enabled": True, "username": username, "password": password},
            )
        except OSError as error:
            self._log(f"保存账号登录设置失败：{error}", "warn")
            return False
        self._log("已保存账号密码自动重新登录设置；仅会在登录缓存失效时尝试一次。", "success")
        return True

    def save_config(self) -> None:
        values = self.config.to_mapping()
        # 允许当前进程临时开启，但磁盘上的下一次启动状态始终关闭。
        values["course_quiz_auto_answer"] = False
        self._write_json_atomic(self.config_path, values)

    def update_settings(self, **values) -> None:
        if "browser_path" in values:
            path = normalize_browser_path(str(values["browser_path"] or ""))
            if path:
                resolved = resolve_browser_path(path)
                if not resolved:
                    raise ValueError(f"浏览器路径无效：{path}。请选择 msedge.exe、chrome.exe 等浏览器程序，或其所在文件夹。")
                values["browser_path"] = resolved
            else:
                values["browser_path"] = ""
        self.config = AppConfig.from_mapping(self.config.to_mapping() | values, self.root)
        self.save_config()

    @staticmethod
    def _write_json_atomic(path: Path, values: dict) -> None:
        """先写同目录临时文件再替换，避免进程中断留下半个 JSON。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _resolve_log_path(self, value: str = "") -> Path:
        path = Path(value.strip() or self.config.log_path).expanduser()
        if not path.is_absolute():
            path = self.root / path
        return path.resolve()

    def open_log_file(self, value: str = "") -> Path:
        """创建并用系统默认程序打开当前日志文件。"""
        path = self._resolve_log_path(value)
        if path.exists() and path.is_dir():
            raise OSError(f"日志路径指向文件夹：{path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return path

    def _fetch_courses(self) -> list[Course]:
        data = self.api.json("GET", f"{LMS_BASE}/courses/students", params={"keyword": "", "publishStatus": 1, "type": 1, "pn": 1, "ps": 50})
        raw_courses = data.get("courseList") or data.get("result", {}).get("courseList", [])
        return [Course.from_api(course) for course in raw_courses]

    def _log(self, text: str, kind: str = "muted") -> None:
        self.emit(text, kind)

    @property
    def course_controller(self):
        """延迟创建课程页控制器，避免普通签到流程依赖浏览器课件页。"""
        if self._course_controller is None:
            from dgutbot.course.yxy_course import CourseController
            # 携带模块通道，前端据此将刷课输出与签到输出隔离。
            self._course_controller = CourseController(
                lambda text, kind: self.emit(text, f"course:{kind}"),
                emit_event=self.emit_event,
            )
        return self._course_controller

    def reserve_course_start(self, task_id: str) -> bool:
        with self._course_reservation_lock:
            if self._course_reservation or self._course_starting or (self._course_controller and self._course_controller._running):
                return False
            self._course_reservation = task_id
            return True

    def release_course_start(self, task_id: str) -> None:
        with self._course_reservation_lock:
            if self._course_reservation == task_id:
                self._course_reservation = None

    def start_course_helper(self, *, rate=None, quiz_mode=None, agent_provider=None, ai_provider=None, task_id=None) -> bool:
        with self._course_operation_lock:
            with self._course_reservation_lock:
                if self._course_reservation and self._course_reservation != task_id:
                    return False
                self._course_starting = True
            try:
                return self._start_course_helper(rate=rate, quiz_mode=quiz_mode, agent_provider=agent_provider, ai_provider=ai_provider)
            finally:
                with self._course_reservation_lock:
                    self._course_starting = False

    def _start_course_helper(self, *, rate=None, quiz_mode=None, agent_provider=None, ai_provider=None) -> bool:
        """启动课件学习辅助；需用户先在调试浏览器打开课件学习页。"""
        from dgutbot.course.yxy_course import CourseConfig
        config = CourseConfig(
            playback_rate=float(self.config.course_playback_rate if rate is None else rate),
            auto_dismiss_dialog=bool(self.config.course_auto_dismiss_dialog),
            document_scroll_enabled=bool(self.config.course_document_scroll_enabled),
            document_scroll_interval=float(self.config.course_document_scroll_interval),
            document_scroll_speed=float(self.config.course_document_scroll_speed),
            quiz_auto_answer=bool(self.config.course_quiz_auto_answer) if quiz_mode is None else quiz_mode != "disabled",
            quiz_mode=quiz_mode or ("ai" if ai_provider is not None else "fixed"),
            quiz_choice_enabled=bool(self.config.course_quiz_choice_enabled),
            quiz_judgment_enabled=bool(self.config.course_quiz_judgment_enabled),
            quiz_blank_enabled=bool(self.config.course_quiz_blank_enabled),
        )
        controller = self.course_controller
        if controller._running:
            return False
        controller._agent_answer_provider = agent_provider
        controller._ai_answer_provider = ai_provider
        return controller.start(config)

    def stop_course_helper(self) -> None:
        """停止课程页控制器，不关闭用户的浏览器标签页。"""
        with self._course_operation_lock:
            self.course_controller.stop()

    def set_course_speed(self, rate: float) -> None:
        """在运行中调整视频播放倍速。"""
        self.course_controller.set_speed(float(rate))

    def course_helper_status(self) -> dict:
        """读取课件页实时状态；页面断连或尚未启动时也返回明确状态。"""
        controller = self.course_controller
        snapshot = controller.status_snapshot()
        if not snapshot.get("running"):
            snapshot["playbackRate"] = float(self.config.course_playback_rate)
            snapshot.setdefault("video", {})["rate"] = float(self.config.course_playback_rate)
        return snapshot

    def _persist_credentials(self) -> None:
        """把最近一次有效凭据保存到本地 auth.json，绝不修改或污染源码。"""
        try:
            self._write_json_atomic(self.root / "auth.json", {"token": self.token, "user_id": self.user_id})
            self._log("已更新本地应用登录缓存。", "success")
        except OSError as error:
            self._log(f"保存本地登录缓存失败：{error}", "warn")

    def _apply_token(self, token: str, user_id: int | None) -> None:
        self.token = token
        self.user_id = user_id
        self.headers["Authorization"] = token
        self._persist_credentials()

    @staticmethod
    def _cookie_value(response: requests.Response, name: str) -> str:
        value = response.cookies.get(name)
        if value:
            return value
        cookies = SimpleCookie()
        cookies.load(response.headers.get("Set-Cookie", ""))
        item = cookies.get(name)
        return item.value if item else ""

    @staticmethod
    def _browser_cookie(cookies: list[dict], name: str) -> str:
        expected = name.casefold()
        return next((
            str(item.get("value") or "") for item in cookies
            if str(item.get("name") or "").casefold() == expected
        ), "")

    @classmethod
    def _browser_user_id(cls, cookies: list[dict]) -> int | None:
        direct = cls._browser_cookie(cookies, "userid")
        if direct.isdigit():
            return int(direct)
        raw_user = cls._browser_cookie(cookies, "USERINFO")
        try:
            value = json.loads(unquote(raw_user)).get("userId") if raw_user else None
            return int(value) if value is not None else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _relogin_with_account(self) -> bool:
        """使用用户明确保存的账号密码重新获取 Token；每次调用只尝试一次。"""
        account = self._load_account()
        if not (account.get("enabled") and account.get("username") and account.get("password")):
            return False
        self._log("登录缓存已失效，正在尝试可选的账号密码重新登录…", "info")
        try:
            response = requests.post(
                "https://application.dgut.edu.cn/appapi/user/login/app",
                data={"loginName": account["username"], "password": account["password"], "alias": "application"},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": self.headers["User-Agent"],
                },
                allow_redirects=False,
                timeout=10,
            )
            if response.status_code not in (200, 302):
                self._log(f"账号密码重新登录失败：服务器返回 {response.status_code}。", "warn")
                return False
            token = self._cookie_value(response, "AUTHORIZATION")
            raw_user = self._cookie_value(response, "USERINFO")
            if not token:
                self._log("账号密码重新登录失败：未收到登录凭据。", "warn")
                return False
            try:
                user_id = json.loads(unquote(raw_user)).get("userId") if raw_user else None
                user_id = int(user_id) if user_id is not None else None
            except (TypeError, ValueError, json.JSONDecodeError):
                user_id = None
            self._apply_token(token, user_id)
            self._log("账号密码重新登录成功，已更新本地登录缓存。", "success")
            return True
        except requests.RequestException as error:
            self._log(f"账号密码重新登录失败：{error}", "warn")
            return False

    @staticmethod
    def _is_unauthorized(error: Exception) -> bool:
        return "401" in str(error)

    def load_saved_courses(self) -> bool:
        """使用本地 Token，并通过已打开的 LMS 页面读取课程。"""
        if not self.token:
            self._log("本地没有可用的登录缓存。", "muted")
            if not self._relogin_with_account():
                return False
        self._log("正在通过浏览器使用本地登录信息读取课程列表…", "info")
        try:
            self.courses = self._fetch_courses()
        except Exception as error:
            self._log(f"浏览器课程请求失败：{error}", "warn")
            if not self._is_unauthorized(error) or not self._relogin_with_account():
                return False
            try:
                self.courses = self._fetch_courses()
            except Exception as retry_error:
                self._log(f"重新登录后仍无法读取课程：{retry_error}", "warn")
                return False
        if not self.courses:
            self._log("本地登录信息已失效或未读取到课程，需要重新登录。", "warn")
            return False
        self._log(f"已读取 {len(self.courses)} 门课程，请在下方选择。", "success")
        return True

    def browser_candidates(self) -> list[tuple[str, list[str]]]:
        """返回按推荐顺序排列的 Chromium 浏览器常见安装位置。"""
        return list(extra_browser_candidates().items())

    def _browser_report(self, message: str, progress: Callable[[str], None] | None = None) -> None:
        try:
            with (self.root / "browser-detection.log").open("a", encoding="utf-8") as handle:
                handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
        except OSError:
            pass
        if progress:
            progress(message)

    def _scan_browsers(self, timeout_seconds: float, progress: Callable[[str], None] | None = None):
        """终端与网页共用：快速路径检查后，在浏览器安装目录内有限扫描。"""
        report = lambda message: self._browser_report(message, progress)
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        self._browser_report(f"开始浏览器检测；时间预算 {timeout_seconds:g} 秒。")
        candidates = self.browser_candidates()
        checked: set[str] = set()
        found: set[str] = set()
        for name, paths in candidates:
            for path in paths:
                if time.monotonic() >= deadline:
                    report("检测超时：尚未检查完候选路径，可手动填写浏览器路径。")
                    return
                key = os.path.normcase(os.path.normpath(path))
                if key in checked:
                    continue
                checked.add(key)
                report(path)
                try:
                    if Path(path).is_file():
                        found.add(name)
                        report(f"找到 {name}：{path}")
                        yield path, name
                        break
                except (OSError, ValueError) as error:
                    report(f"无法检查文件：{path}（{type(error).__name__}：{error}）")
        report("开始扫描浏览器安装目录（最多向下 3 层）。")
        for name, paths in candidates:
            if name in found or name not in BROWSER_INSTALLATIONS:
                continue
            executables = BROWSER_INSTALLATIONS[name][1]
            for root in browser_scan_roots(paths):
                if time.monotonic() >= deadline:
                    report("检测超时：尚未检查完安装目录，可手动填写浏览器路径。")
                    return
                path = next(scan_browser_directory(root, executables, deadline, report), "")
                if path:
                    found.add(name)
                    report(f"找到 {name}：{path}")
                    yield path, name
                    break
        if time.monotonic() >= deadline:
            report("检测超时：尚未检查完安装目录，可手动填写浏览器路径。")
        else:
            report(f"检测结束：找到 {len(found)} 类浏览器；已检查候选路径和限定深度的安装目录。")

    def detect_browsers(self, timeout_seconds: float = 10.0) -> list[dict[str, str]]:
        """扫描并返回全部已安装的 Chromium 浏览器，而不是只取第一个。"""
        return [{"name": name, "path": str(Path(path).resolve())}
                for path, name in self._scan_browsers(timeout_seconds)]

    def find_browser(
        self,
        progress: Callable[[str], None] | None = None,
        timeout_seconds: float = 10.0,
    ) -> tuple[str | None, str | None]:
        """按 Edge、Chrome、其他 Chromium 的顺序检查常见安装位置。"""
        report = lambda message: self._browser_report(message, progress)
        manual = resolve_browser_path(self.config.browser_path)
        # 网页设置已将浏览器名称与精确路径成对保存；选中的路径优先。
        if manual:
            report(str(Path(manual)))
            return manual, self.config.browser_name or Path(manual).stem
        if self.config.browser_path:
            report(f"已保存的浏览器路径无效：{self.config.browser_path}；正在重新检测。")
        return next(self._scan_browsers(timeout_seconds, progress), (None, None))

    def start_browser(self, url: str = "") -> bool:
        if not self.browser_start_lock.acquire(blocking=False):
            self._log("浏览器登录正在启动，请稍候。", "muted")
            return True
        try:
            return self._start_browser_impl(url)
        finally:
            self.browser_start_lock.release()

    def _start_browser_impl(self, url: str = "") -> bool:
        url = url.strip()
        if url and self._open_debug_tab(url):
            return True
        path, name = self.find_browser()
        if not path:
            self._log("未找到可用浏览器。请打开设置并手动选择浏览器程序。", "warn")
            return False
        port = int(self.config.debug_port)
        self._log(f"正在以远程调试模式启动 {name}…", "info")
        debug_profile = self.root / "browser_profile"
        command = [path, f"--remote-debugging-port={port}", "--remote-allow-origins=*", f"--user-data-dir={debug_profile}", "--no-first-run", "--no-default-browser-check"]
        if url:
            command.append(url)
        try:
            # 浏览器无需继承启动器的终端句柄，明确丢弃输入输出。
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            self._log(f"浏览器启动失败：{error}", "warn")
            return False
        self.config.browser_path, self.config.browser_name = path, name
        self.save_config()
        self._log(f"已启动 {name}。请自行进入优学院并完成登录后回到本程序。", "success")
        return True

    def _open_debug_tab(self, url: str) -> bool:
        """调试浏览器已运行时，在同一配置中新增标签页而不是关闭所有窗口。"""
        try:
            port = int(self.config.debug_port)
            response = requests.put(f"http://127.0.0.1:{port}/json/new?{quote(url, safe='')}", timeout=2)
            if response.ok:
                self._log("已在调试浏览器中新开页面。", "success")
                return True
        except requests.RequestException:
            pass
        return False

    def _get_ws_url(self) -> str | None:
        try:
            targets = requests.get(f"http://127.0.0.1:{self.config.debug_port}/json", timeout=2).json()
            page = next((item for item in targets if item.get("type") == "page"), None)
            return page.get("webSocketDebuggerUrl") if page else None
        except (requests.RequestException, ValueError):
            return None

    def _cookies(self, ws_url: str) -> list[dict]:
        ws = create_connection(ws_url, timeout=10)
        try:
            ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
            ws.recv()
            ws.send(json.dumps({"id": 2, "method": "Network.getAllCookies"}))
            while True:
                response = json.loads(ws.recv())
                if response.get("id") == 2:
                    return response.get("result", {}).get("cookies", [])
        finally:
            ws.close()

    def load_session_and_courses(self, wait_seconds: int = 20, automatic: bool = False) -> bool:
        if not automatic:
            self._log("正在连接浏览器远程调试端口…", "info")
        ws_url = None
        attempts = max(1, wait_seconds)
        for attempt in range(attempts):
            ws_url = self._get_ws_url()
            if ws_url:
                break
            if attempt + 1 < attempts:
                time.sleep(1)
        if not ws_url:
            if not automatic:
                self._log("连接浏览器失败：未发现远程调试端口。", "warn")
            return False
        try:
            cookies = self._cookies(ws_url)
        except Exception as error:
            if not automatic:
                self._log(f"读取浏览器登录信息失败：{error}", "warn")
            return False
        dgut = [item for item in cookies if "dgut.edu.cn" in item.get("domain", "")]
        self.token = self._browser_cookie(dgut, "AUTHORIZATION")
        if not self.token:
            if not automatic:
                self._log("未检测到有效登录状态，请在浏览器中完成优学院登录。", "warn")
            return False
        self.headers["Authorization"] = self.token
        self.user_id = self._browser_user_id(dgut)
        self._persist_credentials()
        self._log("登录信息读取成功，正在获取课程列表…", "success")
        try:
            self.courses = self._fetch_courses()
        except Exception as error:
            self._log(f"获取课程列表失败：{error}", "warn")
            return False
        if not self.courses:
            self._log("没有读取到课程，请确认登录账号与网络状态。", "warn")
            return False
        self._log(f"已读取 {len(self.courses)} 门课程，请在下方选择。", "success")
        return True

    def select_course(self, query: str) -> Course | None:
        """只允许选择一门课程；模糊匹配有歧义时要求用户细化输入。"""
        keyword = query.strip()
        if not keyword or "," in keyword or "，" in keyword:
            return None
        matches = [course for course in self.courses if keyword == str(course.id) or keyword.lower() in course.name.lower()]
        if len(matches) != 1:
            return None
        self.selected_course = matches[0]
        return self.selected_course

    def clear_selected_course(self) -> None:
        self.selected_course = None

    def select_course_id(self, course_id: str) -> Course | None:
        course = next((item for item in self.courses if str(item.id) == course_id), None)
        if course is not None:
            self.selected_course = course
        return course

    def _request(self, method: str, url: str, **kwargs) -> BrowserResponse:
        return self.api.request(method, url, **kwargs)

    def _classrooms(self, course_id: int) -> list[Classroom]:
        response = self._request("GET", f"{LMS_BASE}/wisdomClassroom/student/getClassroomList", params={"ocId": course_id, "status": "", "pageNum": 1, "pageSize": 10, "order": 0, "lang": "zh"})
        data = response.json()
        raw_classrooms = data.get("result", {}).get("list", []) if data.get("code") == 1 else []
        return [Classroom.from_api(classroom) for classroom in raw_classrooms]

    def _activities(self, classroom_id: int) -> list[Activity]:
        response = self._request("GET", f"{APP_BASE}/wisdomClassroom/student/classroomActivitys", params={"classroomId": classroom_id, "pageNum": 1, "pageSize": 999})
        data = response.json()
        raw_activities = data.get("result", {}).get("list", []) if data.get("code") == 1 else []
        return [Activity.from_api(activity) for activity in raw_activities]

    @staticmethod
    def _kind(score_type: int | None) -> str:
        return {0: "选人点名", 1: "二维码签到", 2: "数字码签到", 3: "一键签到"}.get(score_type, "未知签到")

    @staticmethod
    def _attendance_code(activity: Activity) -> str:
        return next((str(activity.raw[key]) for key in ("attendanceCode", "code", "codeStr", "signCode", "checkInCode") if activity.raw.get(key)), "")

    def _write_sign_log(self, course: str, kind: str, details: list[str]) -> None:
        if not self.config.save_log:
            return
        path = self._resolve_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        with path.open("a", encoding="utf-8") as file:
            file.write(f"{now:%Y-%m-%d}-{now.hour}:{now:%M} | {self._redact(course)} | {self._redact(kind)} |\n")
            for item in details:
                file.write(f"  - {self._redact(item)}\n")
            file.write("\n")

    @staticmethod
    def _redact(value: object) -> str:
        """避免服务端错误信息把常见凭据原样写进长期日志。"""
        text = str(value)
        patterns = (
            r"(?i)(authorization|token|password|cookie)(\s*[:=]\s*)([^\s,;}&]+)",
            r"(?i)(bearer\s+)([A-Za-z0-9._~+\-/]+=*)",
        )
        for pattern in patterns:
            text = re.sub(pattern, lambda match: f"{match.group(1)}{match.group(2) if match.lastindex and match.lastindex >= 3 else ''}[已隐藏]", text)
        return text

    def _sign(self, course: Course, classroom_id: int, activity: Activity) -> bool:
        score_type = activity.score_type
        kind = self._kind(score_type)
        activity_id = activity.relation_id
        code = self._attendance_code(activity) if score_type == 1 else ""
        if score_type == 1 and not code:
            message = "未找到二维码签到码，已跳过"
            self._log(f"[{course.name}] {kind}：{message}", "warn")
            self._write_sign_log(course.name, kind, [f"attendanceID: {activity_id}", "result: skipped", f"reason: {message}"])
            return True
        if score_type not in (1, 2, 3):
            self._log(f"[{course.name}] {kind}：当前类型不支持自动处理，已跳过", "warn")
            self._write_sign_log(course.name, kind, [f"attendanceID: {activity_id}", "result: skipped", "reason: unsupported scoreType"])
            return True
        payload = {"attendanceID": activity_id, "classID": classroom_id, "userID": self.user_id, "location": f"{self.config.lat},{self.config.lng}", "address": self.config.address, "enterWay": 1, "attendanceCode": code}
        try:
            response = self._request("POST", f"{APP_BASE}/newAttendance/signByStu", json=payload)
            result = response.json()
            status, message = result.get("status"), result.get("msg", result)
        except Exception as error:
            status, message = "exception", str(error)
        if status == 200:
            self._log(f"✓ [{course.name}] {kind}：签到成功", "success")
        elif status == 201:
            self._log(f"• [{course.name}] {kind}：已签到过", "muted")
        else:
            self._log(f"× [{course.name}] {kind}：{message}", "warn")
        self._write_sign_log(course.name, kind, [f"attendanceID: {activity_id}", f"HTTP/status: {status}", f"response: {message}"])
        return status in (200, 201)

    def _poll_once(self, checked: set[str]) -> str:
        today = datetime.now().strftime("%m-%d")
        course = self.selected_course
        if course is None:
            self._log("尚未选择课程，轮询已跳过。", "warn")
            return "尚未选择课程"
        classrooms = [item for item in self._classrooms(course.id) if today in item.title]
        if not classrooms:
            self._log(f"[{course.name}] 本轮完成：今天没有课堂，无需签到。", "muted")
            return "今日暂无课堂"
        active_count = 0
        new_count = 0
        for classroom in classrooms:
            for activity in self._activities(classroom.id):
                if activity.relation_type != 1 or activity.state != 1 or activity.status != 0:
                    continue
                active_count += 1
                key = f"{activity.relation_id}_{classroom.id}"
                if key in checked:
                    continue
                new_count += 1
                self._log(f"[{course.name}] 发现 {self._kind(activity.score_type)}，正在处理…", "info")
                if self._sign(course, classroom.id, activity):
                    checked.add(key)
        if active_count == 0:
            self._log(f"[{course.name}] 本轮完成：未发现进行中的签到。", "muted")
            return "未发现进行中的签到"
        elif new_count == 0:
            self._log(f"[{course.name}] 本轮完成：签到活动已处理，继续等待。", "muted")
            return "签到活动已处理"
        return f"已处理 {new_count} 个签到活动"

    def sign_monitor_status(self) -> dict[str, Any]:
        """返回供前端原地刷新的轻量状态，避免把每轮检查打印成日志。"""
        with self.monitor_lock:
            running = self.monitor_thread is not None and self.monitor_thread.is_alive()
            return {
                "running": running,
                "state": self.monitor_state.value,
                "courseName": self.selected_course.name if self.selected_course else "",
                "intervalSeconds": self.monitor_interval,
                "round": self.monitor_round,
                "startedAt": self.monitor_started_at,
                "lastCheck": self.monitor_last_check,
                "lastResult": self.monitor_last_result,
            }

    def start_monitor(self) -> bool:
        with self.monitor_lock:
            if self.monitor_thread is not None and self.monitor_thread.is_alive():
                self._log("签到监测已在运行，无需重复启动。", "muted")
                return False
            self.stop_event.clear()
            self.monitor_state = MonitorState.RUNNING
            self.monitor_round = 0
            self.monitor_interval = max(2, int(self.config.poll_interval))
            self.monitor_started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.monitor_last_check = ""
            self.monitor_last_result = "等待首次检查"
            self.monitor_thread = threading.Thread(
                target=self._monitor,
                name="sign-monitor",
                daemon=True,
            )
            self.monitor_thread.start()
            return True

    def stop_monitor(self) -> None:
        self.stop_event.set()
        with self.monitor_lock:
            if self.monitor_thread is not None and self.monitor_thread.is_alive():
                self.monitor_state = MonitorState.STOPPED

    def _monitor(self) -> None:
        try:
            checked: set[str] = set()
            interval = self.monitor_interval
            self._log(f"开始轮询，每 {interval} 秒检查一次。", "success")
            round_number = 0
            while not self.stop_event.is_set():
                round_number += 1
                try:
                    course_name = self.selected_course.name if self.selected_course else "未选择课程"
                    self._log(f"第 {round_number} 轮：正在检查《{course_name}》…", "info")
                    result = self._poll_once(checked)
                    with self.monitor_lock:
                        self.monitor_round = round_number
                        self.monitor_last_check = datetime.now().astimezone().isoformat(timespec="seconds")
                        self.monitor_last_result = result
                except Exception as error:
                    with self.monitor_lock:
                        self.monitor_round = round_number
                        self.monitor_last_check = datetime.now().astimezone().isoformat(timespec="seconds")
                        self.monitor_last_result = "检查失败"
                    self._log(f"轮询出错：{error}", "warn")
                self.stop_event.wait(interval)
        finally:
            with self.monitor_lock:
                self.monitor_state = MonitorState.IDLE
                if self.monitor_thread is threading.current_thread():
                    self.monitor_thread = None
