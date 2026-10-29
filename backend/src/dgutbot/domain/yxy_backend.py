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
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

import requests
from websocket import create_connection

from dgutbot.app.browser_paths import (
    BROWSER_INSTALLATIONS, browser_scan_roots, extra_browser_candidates,
    is_browseros_neo, normalize_browser_path, resolve_browser_path, scan_browser_directory,
)
from dgutbot.domain.course_scan import (
    AuthRequiredError, CourseScanService, DirectorySampleRequired, ScanCancelled,
)
from dgutbot.domain.courseware_export import UA_BASE, CoursewareExportService, parse_directory
from dgutbot.domain.courseware_filters import ExportOptions, export_options, source_kind
from dgutbot.domain.courseware_download import download_file
from dgutbot.domain.courseware_sources import RESOURCE_NODE, export_directory, export_resources
from dgutbot.domain.homework_archive import HomeworkArchive
from dgutbot.domain.homework_export import HOMEWORK_NODE, append_homework_directory, export_homework
from dgutbot.domain.homework_scan import HomeworkAuthRequiredError, HomeworkScanService
from dgutbot.domain.homework_review import build_peer_review_payloads, normalize_homework_review
from dgutbot.domain.school_auth import IDENTITY_URL, LOGIN_URL, login_headers
from dgutbot.logs import redact
from dgutbot.domain.sign_monitor import (
    LMS_BASE, Activity, Classroom, MonitorState, SignMonitor,
)



@dataclass
class AppConfig:
    browser_path: str = ""
    browser_name: str = ""
    debug_port: int = 9222
    poll_interval: int = 5
    selection_auto_leave: bool = False
    lat: float = 23.0432
    lng: float = 113.3993
    address: str = "东莞理工学院"
    campus_login_on_startup: bool = False
    # 课件学习辅助：播放、文档阅读、章节衔接与 AI 测验自动作答。
    course_playback_rate: float = 8.0
    course_auto_dismiss_dialog: bool = True
    course_document_scroll_enabled: bool = True
    course_document_scroll_interval: float = 3.0
    course_document_scroll_speed: float = 3.0
    course_quiz_auto_answer: bool = True
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
        defaults = cls()
        if not isinstance(values, dict):
            values = {}
        merged = asdict(defaults) | {
            key: value for key, value in values.items() if key in defaults.__dataclass_fields__
        }
        merged.update(
            debug_port=cls._number(merged["debug_port"], defaults.debug_port, 1024, 65535, integer=True),
            poll_interval=cls._number(merged["poll_interval"], defaults.poll_interval, 2, 3600, integer=True),
            selection_auto_leave=cls._boolean(merged["selection_auto_leave"], defaults.selection_auto_leave),
            campus_login_on_startup=cls._boolean(
                merged["campus_login_on_startup"], defaults.campus_login_on_startup,
            ),
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
        for key in ("browser_path", "browser_name", "address"):
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


class BrowserLoginRedirectError(RuntimeError):
    """浏览器明确报告接口发生重定向，与网络 TypeError 分开处理。"""


class BrowserApiClient:
    """在与目标接口同源的页面内执行 fetch，复用 Chromium 网络栈与会话。"""

    LMS_ORIGIN = "https://lms.dgut.edu.cn"
    ALLOWED_ORIGINS = frozenset({LMS_ORIGIN, "https://application.dgut.edu.cn", "https://ua.dgut.edu.cn"})
    _FETCH_FUNCTION = r"""
async function(request) {
  const controller = new AbortController();
  const timer = setTimeout(function() { controller.abort(); }, request.timeoutMs);
  try {
    const options = {
      method: request.method,
      credentials: 'include',
      headers: request.headers,
      redirect: 'manual',
      signal: controller.signal
    };
    if (request.body !== null) options.body = request.body;
    const response = await fetch(request.url, options);
    if (response.type === 'opaqueredirect') return {loginRedirect: true};
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
                            raise RuntimeError("浏览器拒绝设置登录信息或创建后台页面")
                        return message.get("result") or {}

                authorization = str(self._headers.get("Authorization") or "")
                if authorization:
                    browser_call("Storage.setCookies", {"cookies": [
                        {
                            "name": "AUTHORIZATION", "value": authorization,
                            "domain": ".dgut.edu.cn", "path": "/", "secure": True, "httpOnly": True,
                        },
                        {
                            # learnCourse 的旧版前端通过 document.cookie 读取 token。
                            "name": "token", "value": authorization,
                            "domain": ".dgut.edu.cn", "path": "/", "secure": True, "httpOnly": False,
                        },
                    ]})
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
                    # CDP 的 error.message 是浏览器给出的原文，原样抛出，不另做概括。
                    detail = str((message.get("error") or {}).get("message") or "").strip()
                    raise RuntimeError(detail or "浏览器拒绝执行请求")
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
        extra_headers = kwargs.pop("headers", None) or {}
        max_chars = kwargs.pop("max_chars", 1_048_576)
        if kwargs:
            raise TypeError(f"不支持的浏览器请求参数：{', '.join(sorted(kwargs))}")
        if params:
            query = urlencode(params, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        headers = {"Accept": "application/json, text/plain, */*"}
        authorization = str(self._headers.get("Authorization") or "")
        if authorization:
            headers["Authorization"] = authorization
        if not isinstance(extra_headers, dict):
            raise TypeError("浏览器请求的额外请求头必须是字典")
        headers.update({str(key): str(value) for key, value in extra_headers.items()})
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
            "maxResponseChars": max(1024, min(8_388_608, int(max_chars))),
        }
        with self._lock:
            origin = f"{parts.scheme}://{parts.netloc}"
            target = self._discover_target(origin)
            self._connect(target)
            # Edge 可冻结后台学校页；先恢复运行，否则 fetch 与超时计时器
            # 都可能停住，最终表现为 CDP 连接超时。无需切换用户当前标签。
            self._call("Page.setWebLifecycleState", {"state": "active"}, timeout=5)
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
        if value.get("loginRedirect"):
            raise BrowserLoginRedirectError("学校接口发生登录重定向，请重新登录")
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

class SignBackend:
    def __init__(
        self,
        emit: Callable[[str, str, str], None],
        root: Path | None = None,
        emit_event: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.emit = emit
        self.emit_event = emit_event
        self.root = root or Path(__file__).resolve().parents[4]
        self.config_path = self.root / "config.json"
        self.config: AppConfig = self._load_config()
        # 缓存仅在登录读取时验证后恢复，不把磁盘数据直接当作已登录状态。
        self._login_cache_path = self.root / "login-cache.json"
        self.token = ""
        self.user_id: int | None = None
        self.display_name = ""
        self.account_name = ""
        self.courses: list[Course] = []
        self.selected_course: Course | None = None
        self.browser_start_lock = threading.Lock()
        # 远端课程/签到请求只从这里读取 Authorization；其余网络请求头均由
        # Chromium 根据 LMS 页面上下文生成。User-Agent 仅供登录恢复/AI 兼容层使用。
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if self.token:
            self.headers["Authorization"] = self.token
        self._rejected_browser_token = ""
        self.api = BrowserApiClient(lambda: int(self.config.debug_port), self.headers)
        self.sign_monitor = SignMonitor(
            request=self._request,
            selected_course=lambda: self.selected_course,
            settings=lambda: self.config,
            token=lambda: str(self.headers.get("Authorization") or self.token or ""),
            user_id=lambda: self.user_id,
            log=lambda text, kind: self._log(text, kind, "sign"),
            recovery_path=self.root / "course-recovery.json",
        )
        self._course_controller = None
        self._course_operation_lock = threading.RLock()
        self._course_reservation_lock = threading.Lock()
        self._course_reservation = None
        self._course_starting = False
        self.course_scan = CourseScanService(
            lambda: list(self.courses), self._scan_course_directory,
            lambda: bool(self.token and self.courses), self._redact,
            directory_session=self._course_scan_session,
            cache_context=lambda: (self.user_id, self.token),
            log=lambda text, kind: self._log(text, kind, "course"),
        )
        self.homework_archive = HomeworkArchive(self.root)
        self.homework_scan = HomeworkScanService(
            lambda: list(self.courses), self._scan_course_homeworks,
            lambda: bool(self.token and self.courses), self._redact,
            archives=lambda: self.homework_archive.sets(self.user_id),
            user_id=lambda: self.user_id,
        )
        # 导出汇总资源区、所有教材与作业；测验在解析层跳过。
        self.courseware_export = CoursewareExportService(
            root=self.root / "courseware",
            directory=self._courseware_directory_payload,
            directory_for_options=self._courseware_directory_payload,
            chapter=self._fetch_courseware_chapter,
            download=self._download_courseware_file,
            authenticated=lambda: bool(self.token),
            redact=self._redact,
            log=lambda text, kind: self._log(text, kind, "courseware"),
            on_completed=self._open_courseware_directory,
        )
        self._auto_course_lock = threading.Lock()
        self._auto_course_generation = 0

    def resume_course_recovery(self) -> bool:
        """恢复事务不依赖课程列表：已退出的课程可能是账号唯一一门课。"""
        pending = self.sign_monitor.membership.pending if self.sign_monitor.membership else None
        if pending and self.token and self.user_id == pending["userId"]:
            if self.monitor_thread is not None and self.monitor_thread.is_alive():
                return True
            return self.sign_monitor.start()
        return False

    def _load_config(self) -> AppConfig:
        values: dict = {}
        try:
            values = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        return AppConfig.from_mapping(values, self.root)

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
        self._log("已保存账号密码自动重新登录设置；仅会在本次登录失效时尝试一次。", "success")
        return True

    def save_config(self) -> None:
        self._write_json_atomic(self.config_path, self.config.to_mapping())

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
        updated = AppConfig.from_mapping(self.config.to_mapping() | values, self.root)
        # 写入成功后才更新内存，保存失败不能让界面与重启后的配置不一致。
        self._write_json_atomic(self.config_path, updated.to_mapping())
        self.config = updated

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

    def open_log_file(self, value: str = "") -> Path:
        """只打开已有的手动导出文件，不创建或修改文件。"""
        if not value.strip():
            raise ValueError("请先在运行日志页导出日志")
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if not path.is_file():
            raise OSError(f"日志文件不存在或不是文件：{path}")
        if os.name == "nt":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return path

    def _fetch_courses(self) -> list[Course]:
        courses = []
        pn = 1
        while True:
            data = self.api.json("GET", f"{LMS_BASE}/courses/students", params={
                "keyword": "", "publishStatus": 1, "type": 1, "pn": pn, "ps": 50,
            })
            raw_courses = data.get("courseList") or data.get("result", {}).get("courseList", [])
            courses.extend(Course.from_api(course) for course in raw_courses)
            if len(raw_courses) < 50:
                return courses
            pn += 1

    @staticmethod
    def _directory_tree_present(value: Any) -> bool:
        tree_names = {"chapters", "chapterlist", "sections", "sectionlist", "pages", "pagelist", "contenttree", "directory", "catalog", "outline"}
        if isinstance(value, dict):
            return any(
                (str(key).lower() in tree_names and isinstance(child, (dict, list)))
                or (isinstance(child, (dict, list)) and SignBackend._directory_tree_present(child))
                for key, child in value.items()
            )
        if isinstance(value, list):
            return any(SignBackend._directory_tree_present(child) for child in value)
        return False

    @classmethod
    def _directory_urls(cls, value: Any) -> list[str]:
        """仅提取平台响应明确给出的目录 URL，不在客户端拼接接口路径。"""
        found: list[str] = []
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = str(key).lower()
                if isinstance(child, str) and any(word in lowered for word in ("catalog", "directory", "outline", "chapter", "content", "progress")):
                    try:
                        parts = urlsplit(child)
                    except ValueError:
                        parts = None
                    if parts and f"{parts.scheme}://{parts.netloc}" in BrowserApiClient.ALLOWED_ORIGINS:
                        found.append(child)
                elif isinstance(child, (dict, list)):
                    found.extend(cls._directory_urls(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(cls._directory_urls(child))
        return list(dict.fromkeys(found))

    def _scan_course_directory(self, course: Course) -> Any:
        """每个扫描 worker 使用独立 CDP 会话，避免共享请求桥把线程池串行化。"""
        api = BrowserApiClient(lambda: int(self.config.debug_port), self.headers)
        try:
            return self._course_directory_payload(course, api=api)
        finally:
            api.close()

    @contextmanager
    def _course_scan_session(self, cancel: threading.Event) -> Iterator[Callable[[Course], Any]]:
        """每个扫描线程复用一个连接；取消在请求边界生效，不再继续读后续教材。"""
        api = BrowserApiClient(lambda: int(self.config.debug_port), dict(self.headers))
        try:
            yield lambda course: self._course_directory_payload(
                course, api=api, cancel=cancel, deadline=time.monotonic() + 30,
            )
        finally:
            api.close()

    def _scan_course_homeworks(self, course: Course) -> Any:
        """每个扫描 worker 使用独立 CDP 会话读取一门课程的全部作业。"""
        api = BrowserApiClient(lambda: int(self.config.debug_port), self.headers)
        try:
            try:
                return api.request(
                    "GET", f"{LMS_BASE}/homeworks/student/v2",
                    params={"ocId": course.id, "pn": 1, "ps": 999, "lang": "zh"}, timeout=12,
                ).json()
            except Exception as error:
                if self._is_login_rejected(error):
                    raise HomeworkAuthRequiredError("登录已失效") from error
                raise
        finally:
            api.close()

    @staticmethod
    def _directory_json(
        api: BrowserApiClient, url: str, *, params: dict[str, Any] | None = None,
        cancel: threading.Event | None = None, deadline: float | None = None,
    ) -> Any:
        """目录请求先快速失败；仅对浏览器超时做一次更短的瞬时重试。"""
        def request(timeout: float) -> Any:
            if cancel is not None and cancel.is_set():
                raise ScanCancelled()
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining < 1:
                    raise TimeoutError("课程目录读取超时（30 秒预算已用完）")
                timeout = min(timeout, remaining)
            result = api.request("GET", url, params=params, timeout=timeout).json()
            if cancel is not None and cancel.is_set():
                raise ScanCancelled()
            return result

        try:
            return request(12)
        except RuntimeError as error:
            if "AbortError" not in str(error):
                raise
            return request(8)

    def _course_directory_payload(
        self, course: Course, *, api: BrowserApiClient | None = None,
        cancel: threading.Event | None = None, deadline: float | None = None,
    ) -> Any:
        """读取 2026-09-10 实页采样验证的教材目录，并标准化为解析器输入。"""
        request_api = api or self.api

        def read(url: str, *, params: dict[str, Any] | None = None) -> Any:
            return self._directory_json(request_api, url, params=params, cancel=cancel, deadline=deadline)

        try:
            textbook_response = read(
                f"{LMS_BASE}/textbook/student/{course.id}/list", params={"lang": "zh"},
            )
            textbooks = textbook_response if isinstance(textbook_response, list) else textbook_response.get("result", [])
            textbooks = [item for item in textbooks if isinstance(item, dict) and item.get("courseId")] if isinstance(textbooks, list) else []
            # 没有可读教材时，班级信息不会产生任何课件结果。
            if not textbooks:
                return {"pages": []}
            class_response = read(
                f"{LMS_BASE}/classes/information/student/{course.id}", params={"lang": "zh"},
            )
            class_info = class_response.get("result", class_response) if isinstance(class_response, dict) else {}
            class_id = str(class_info.get("classId") or "")
            normalized: list[dict[str, Any]] = []
            seen_textbooks: set[str] = set()
            for textbook in textbooks:
                textbook_id = str(textbook["courseId"])
                if textbook_id in seen_textbooks:
                    continue
                seen_textbooks.add(textbook_id)
                info_response = read(
                    f"{LMS_BASE}/textbook/student/information",
                    params={"currentPlatformType": 1, "lang": "zh", "ocId": course.id, "textbookId": textbook_id},
                )
                info = info_response.get("result", info_response) if isinstance(info_response, dict) else {}
                entries = info.get("list", []) if isinstance(info, dict) else []
                for entry in entries if isinstance(entries, list) else []:
                    if not isinstance(entry, dict):
                        continue
                    plan = entry.get("planState") if isinstance(entry.get("planState"), dict) else {}
                    if entry.get("hide") not in (None, 0, "0") or plan.get("unitHide") not in (None, 0, "0") or plan.get("activityAutoHide") not in (None, 0, "0"):
                        continue
                    if plan.get("studyState") not in (None, 1, "1"):
                        continue
                    chapter_id = str(entry.get("currentUnitID") or "")
                    if not chapter_id or not class_id:
                        continue
                    return_url = f"https://lms.dgut.edu.cn/courseweb/ulearning/index.html#/course/textbook?courseId={course.id}"
                    learn_url = "https://ua.dgut.edu.cn/learnCourse/learnCourse.html?" + urlencode({
                        "courseId": textbook_id, "chapterId": chapter_id,
                        "classId": class_id, "returnUrl": return_url,
                    })
                    normalized.append({
                        **entry, "courseId": textbook_id, "classId": class_id,
                        "chapterId": chapter_id, "url": learn_url,
                        "name": entry.get("currentUnit") or entry.get("currentActivity") or "未命名课件",
                        "contentType": "课件",
                    })
            return {"pages": normalized}
        except (ScanCancelled, TimeoutError):
            raise
        except Exception as error:
            if self._is_login_rejected(error):
                raise AuthRequiredError("登录已失效") from error
            verified_error = error
        if self._directory_tree_present(course.raw):
            return course.raw
        for url in self._directory_urls(course.raw):
            try:
                payload = read(url)
            except (ScanCancelled, TimeoutError):
                raise
            except Exception as error:
                if self._is_login_rejected(error):
                    raise AuthRequiredError("登录已失效") from error
                continue
            if isinstance(payload, (dict, list)):
                return payload
        raise DirectorySampleRequired(f"已验证的教材目录读取失败：{self._redact(verified_error)}")

    def start_course_scan(self, *, force: bool = False) -> dict:
        if not self.courses and self.token:
            self.refresh_courses()
        return self.course_scan.start(force=force)

    def cancel_course_scan(self) -> dict:
        return self.course_scan.cancel()

    def course_scan_status(self) -> dict:
        return self.course_scan.snapshot()

    # ------------------------------------------------------------ 课件导出
    def _ua_headers(self) -> dict[str, str]:
        """优学院网关要求 ``ua-authorization``，与 LMS 的 Authorization 同值。"""
        token = str(self.headers.get("Authorization") or self.token or "")
        return {"ua-authorization": token, "version": "1"} if token else {"version": "1"}

    def _courseware_fetch(self, api: BrowserApiClient, url: str, *, ua: bool = False, **kwargs) -> Any:
        try:
            return api.request("GET", url, headers=self._ua_headers() if ua else None,
                               timeout=30, max_chars=4_000_000, **kwargs).json()
        except Exception as error:
            if self._is_login_rejected(error):
                raise AuthRequiredError("登录已失效，请重新登录") from error
            raise

    def _courseware_directory_payload(self, course: Course, options: ExportOptions | None = None) -> Any:
        """导出任务专用的独立 CDP 会话，避免与签到轮询共享请求桥。"""
        api = BrowserApiClient(lambda: int(self.config.debug_port), self.headers)
        try:
            fetch = lambda url, **kwargs: self._courseware_fetch(api, url, **kwargs)
            filters = options or ExportOptions()
            directory = export_directory(course, fetch, include_courseware="courseware" in filters.sources)
            return append_homework_directory(directory, course, fetch) if "homework" in filters.sources else directory
        finally:
            api.close()

    def _fetch_courseware_chapter(self, node_id: str) -> Any:
        """读取资源区、作业或章节整页；UA 章节使用 ``nodeid``。"""
        api = BrowserApiClient(lambda: int(self.config.debug_port), self.headers)
        try:
            if node_id.startswith(HOMEWORK_NODE):
                return export_homework(node_id, self.user_id, lambda url, **kwargs: self._courseware_fetch(api, url, **kwargs))
            if node_id.startswith(RESOURCE_NODE):
                return export_resources(node_id[len(RESOURCE_NODE):],
                                        lambda url, **kwargs: self._courseware_fetch(api, url, **kwargs))
            return self._courseware_fetch(
                api, f"{UA_BASE}/wholepage/chapter/stu/{quote(str(node_id), safe='')}", ua=True,
            )
        finally:
            api.close()

    def _download_courseware_file(self, url: str, destination: Path) -> int:
        """附件按需下载；凭据只发往学校域名，失败时不留半截文件。"""
        return download_file(url, destination, token=str(self.headers.get("Authorization") or self.token or ""))

    def start_courseware_export(self, course_id: str, *, options: Any = None) -> dict:
        return self.start_courseware_exports([course_id], options=options)

    def _export_courses(self, course_ids: list[str]) -> list[Course]:
        if not isinstance(course_ids, list):
            raise ValueError("courseIds 应为课程标识列表")
        available = {str(item.id): item for item in self.courses}
        selected = list(dict.fromkeys(str(value) for value in course_ids))
        if not selected:
            raise ValueError("请至少选择一门课程")
        if any(value not in available for value in selected):
            raise ValueError("课程不在当前列表中，请先刷新课程")
        return [available[value] for value in selected]

    def start_courseware_exports(self, course_ids: list[str], *, options: Any = None) -> dict:
        courses = self._export_courses(course_ids)
        filters = export_options(options)
        if set(filters.chapters) - {str(course.id) for course in courses}:
            raise ValueError("目录筛选包含未选择的课程")
        return self.courseware_export.start_many(courses, options=filters)

    def courseware_export_catalog(self, course_ids: list[str]) -> list[dict[str, Any]]:
        """只读取目录供勾选，不读取章节正文或下载附件。"""
        courses = self._export_courses(course_ids)
        if not self.token:
            raise ValueError("登录状态不可用，请重新登录")
        result = []
        for course in courses:
            entry = {"courseId": str(course.id), "courseName": course.name, "chapters": [], "failures": [], "error": ""}
            try:
                payload = self._courseware_directory_payload(course)
                _, chapters = parse_directory(payload)
                entry["chapters"] = [{"id": chapter["nodeid"], "title": chapter["title"],
                                      "source": source_kind(chapter["nodeid"])} for chapter in chapters]
                entry["failures"] = [dict(item, reason=self._redact(item["reason"])) for item in payload.get("failures", [])]
            except Exception as error:
                entry["error"] = self._redact(error)
            result.append(entry)
        return result

    def courseware_export_status(self) -> dict:
        return self.courseware_export.snapshot()

    def cancel_courseware_export(self) -> dict:
        return self.courseware_export.cancel()

    def _open_courseware_directory(self, folder: Path) -> Path:
        path = folder.resolve()
        root = (self.root / "courseware").resolve()
        if not path.is_relative_to(root) or not path.is_dir():
            raise ValueError("导出文件夹不存在或不在课件目录中")
        if os.name == "nt":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return path

    def open_courseware_export_folder(self) -> Path:
        folder = self.courseware_export.output_folder()
        if folder is None:
            raise ValueError("当前没有可打开的导出结果")
        return self._open_courseware_directory(folder)

    def start_homework_scan(self, *, force: bool = False) -> dict:
        if not self.courses and self.token:
            self.refresh_courses()
        return self.homework_scan.start(force=force)

    def cancel_homework_scan(self) -> dict:
        return self.homework_scan.cancel()

    def homework_scan_status(self) -> dict:
        return self.homework_scan.snapshot()

    def set_homework_archive(self, kind: str, target_id: str, archived: bool) -> dict:
        """归档只写入本机账号级标记，并立即刷新当前扫描快照。"""
        user_id = self.user_id
        if user_id is None:
            raise HomeworkAuthRequiredError("登录信息缺少用户标识，请重新登录")
        snapshot = self.homework_scan.snapshot()
        targets = {
            group["courseId"] if kind == "course" else item["id"]
            for group in snapshot["groups"] for item in group["items"]
        }
        if kind not in {"course", "item"} or target_id not in targets or self.user_id != user_id:
            raise ValueError("归档对象已失效，请重新扫描")
        self.homework_archive.set_archived(user_id, str(kind), target_id, bool(archived))
        return self.homework_scan.refresh_archive()

    def _homework_review_payloads(self, item: dict[str, Any]) -> tuple[Any, Any, Any]:
        if self.user_id is None:
            raise HomeworkAuthRequiredError("登录信息缺少用户标识，请重新登录")
        homework_id = str(item.get("homeworkId") or "")
        course_id = str(item.get("courseId") or "")
        if not homework_id.isdigit() or not course_id.isdigit():
            raise ValueError("互评作业标识无效，请重新扫描")
        api = BrowserApiClient(lambda: int(self.config.debug_port), self.headers)
        try:
            detail = api.request(
                "GET", f"https://lms.dgut.edu.cn/homeworkapi/stuHomework/homeworkDetail/{homework_id}/{self.user_id}/{course_id}",
                timeout=12,
            ).json()
            peers = api.request(
                "GET", f"https://lms.dgut.edu.cn/homeworkapi/stuHomework/peerReviewHomeworkDatil/{homework_id}/{self.user_id}",
                timeout=12,
            ).json()
            rules = api.request(
                "GET", f"https://lms.dgut.edu.cn/homeworkapi/teaHomework/peerReviewHomeworkRule/{homework_id}",
                timeout=12,
            ).json()
            return detail, peers, rules
        except Exception as error:
            if self._is_login_rejected(error):
                raise HomeworkAuthRequiredError("登录已失效，请重新登录") from error
            raise
        finally:
            api.close()

    def homework_review(self, item_id: str) -> dict[str, Any]:
        item = self.homework_scan.item(str(item_id))
        if item is None:
            raise ValueError("未找到互评作业，请重新扫描")
        detail, peers, rules = self._homework_review_payloads(item)
        return normalize_homework_review(str(item_id), detail, peers, rules)

    @staticmethod
    def _homework_deadline_open(value: Any) -> bool:
        """批量操作只接受能够确认且尚未到达的截止时间。"""
        if not value:
            return False
        try:
            deadline = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.astimezone()
            return deadline > datetime.now().astimezone()
        except (OSError, TypeError, ValueError):
            return False

    def _post_homework_review_payloads(self, payloads: list[dict[str, Any]]) -> int:
        submitted = 0
        api = BrowserApiClient(lambda: int(self.config.debug_port), self.headers)
        try:
            for payload in payloads:
                try:
                    result = api.request(
                        "POST", "https://lms.dgut.edu.cn/homeworkapi/stuHomework/savePeerReview",
                        json=payload, timeout=15,
                    ).json()
                except Exception as error:
                    if self._is_login_rejected(error):
                        raise HomeworkAuthRequiredError("登录已失效，请重新登录") from error
                    raise RuntimeError(f"已提交 {submitted} 份，第 {submitted + 1} 份提交失败：{self._redact(error)}") from error
                code = result.get("code") if isinstance(result, dict) else None
                if code not in (None, 0, 1, 200):
                    message = result.get("message") or result.get("msg") or f"返回状态 {code}"
                    raise RuntimeError(f"已提交 {submitted} 份，第 {submitted + 1} 份提交失败：{self._redact(message)}")
                submitted += 1
        finally:
            api.close()
        return submitted

    def submit_homework_reviews(self, item_id: str, drafts: Any) -> dict[str, Any]:
        """提交前重新读取服务端互评对象，避免使用过期或伪造的学生/互评标识。"""
        item = self.homework_scan.item(str(item_id))
        if item is None:
            raise ValueError("未找到互评作业，请重新扫描")
        detail, peers, rules = self._homework_review_payloads(item)
        if self.user_id is None:
            raise HomeworkAuthRequiredError("登录信息缺少用户标识，请重新登录")
        payloads = build_peer_review_payloads(detail, peers, rules, drafts, self.user_id)
        submitted = self._post_homework_review_payloads(payloads)
        return {"submitted": submitted, "total": len(payloads), "message": f"已提交 {submitted} 份互评"}

    def pending_homework_review_candidates(self) -> dict[str, Any]:
        """只读预检批量 100 分候选，详情接口必须明确确认当前处于互评时间。"""
        if self.user_id is None:
            raise HomeworkAuthRequiredError("登录信息缺少用户标识，请重新登录")
        snapshot = self.homework_scan.snapshot()
        if snapshot.get("state") != "completed":
            raise ValueError("作业扫描尚未完成，请完成扫描后再检查")
        preliminary = [
            item for group in snapshot.get("groups", []) for item in group.get("items", [])
            if item.get("needsAction") and not item.get("archived") and self._homework_deadline_open(item.get("endTime"))
        ]
        candidates: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for item in preliminary:
            try:
                detail, peers, rules = self._homework_review_payloads(item)
                review = normalize_homework_review(str(item.get("id") or ""), detail, peers, rules)
                if float(review.get("fullScore") or 0) < 100:
                    continue
                tasks = [task for task in review.get("tasks", []) if not task.get("wasScored") and task.get("editable")]
                if not tasks:
                    continue
                drafts = [{
                    "key": task["key"], "score": 100, "comment": "",
                    "ruleScores": {rule["key"]: rule["score"] for rule in task.get("rules", [])},
                } for task in tasks]
                build_peer_review_payloads(detail, peers, rules, drafts, self.user_id)
                candidates.append({
                    "itemId": str(item.get("id") or ""),
                    "courseName": str(item.get("courseName") or "未命名课程")[:200],
                    "title": str(item.get("title") or "未命名作业")[:200],
                    "reviewCount": len(tasks),
                })
            except HomeworkAuthRequiredError:
                raise
            except Exception as error:
                failures.append({
                    "itemId": str(item.get("id") or ""),
                    "title": str(item.get("title") or "未命名作业")[:200],
                    "reason": self._redact(error),
                })
        return {"items": candidates, "failures": failures}

    def submit_pending_homework_reviews(self, item_ids: Any) -> dict[str, Any]:
        """为快照中未互评、未过期且服务端仍允许填写的未评分内容提交 100 分。"""
        if self.user_id is None:
            raise HomeworkAuthRequiredError("登录信息缺少用户标识，请重新登录")
        if not isinstance(item_ids, list) or not item_ids or len(item_ids) > 100:
            raise ValueError("待提交作业清单无效，请重新扫描")
        requested = [str(value or "") for value in item_ids]
        if any(not value for value in requested) or len(set(requested)) != len(requested):
            raise ValueError("待提交作业清单包含无效或重复项目")
        snapshot = self.homework_scan.snapshot()
        if snapshot.get("state") != "completed":
            raise ValueError("作业扫描尚未完成，请完成扫描后再提交")
        all_items = {
            str(item.get("id") or ""): item
            for group in snapshot.get("groups", []) for item in group.get("items", [])
        }
        if set(requested) - set(all_items):
            raise ValueError("待提交作业已失效，请重新扫描")
        candidates = [
            all_items[item_id] for item_id in requested
            if all_items[item_id].get("needsAction")
            and not all_items[item_id].get("archived")
            and self._homework_deadline_open(all_items[item_id].get("endTime"))
        ]
        submitted_assignments = 0
        submitted_reviews = 0
        skipped_assignments = 0
        failures: list[dict[str, str]] = []
        for item in candidates:
            try:
                detail, peers, rules = self._homework_review_payloads(item)
                review = normalize_homework_review(str(item.get("id") or ""), detail, peers, rules)
                if float(review.get("fullScore") or 0) < 100:
                    raise ValueError("作业满分低于 100，无法填写 100 分")
                tasks = [task for task in review.get("tasks", []) if not task.get("wasScored") and task.get("editable")]
                if not tasks:
                    skipped_assignments += 1
                    continue
                drafts = [{
                    "key": task["key"], "score": 100, "comment": "",
                    "ruleScores": {rule["key"]: rule["score"] for rule in task.get("rules", [])},
                } for task in tasks]
                payloads = build_peer_review_payloads(detail, peers, rules, drafts, self.user_id)
                submitted_reviews += self._post_homework_review_payloads(payloads)
                submitted_assignments += 1
            except HomeworkAuthRequiredError:
                raise
            except Exception as error:
                failures.append({
                    "itemId": str(item.get("id") or ""),
                    "title": str(item.get("title") or "未命名作业")[:200],
                    "reason": self._redact(error),
                })
        parts = [f"已为 {submitted_assignments} 个作业提交 {submitted_reviews} 份 100 分互评"]
        if skipped_assignments:
            parts.append(f"{skipped_assignments} 个作业没有未评分内容")
        if failures:
            parts.append(f"{len(failures)} 个作业提交失败")
        return {
            "candidateAssignments": len(candidates),
            "submittedAssignments": submitted_assignments,
            "submittedReviews": submitted_reviews,
            "skippedAssignments": skipped_assignments,
            "failures": failures,
            "message": "；".join(parts),
        }

    def _log(self, text: str, kind: str = "muted", module: str = "app") -> None:
        """默认归属“程序”：登录、浏览器、设置、课程列表都走这里。

        只有真正属于签到监测的输出才传 ``module="sign"``。
        """
        self.emit(text, kind, module)

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
            quiz_mode=quiz_mode or ("ai" if self.config.course_quiz_auto_answer else "disabled"),
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
            self._auto_course_generation += 1
            self.course_controller.stop()
            self.course_scan.reset_open_state()

    @staticmethod
    def _target_identifiers(url: str, state: dict[str, Any]) -> set[str]:
        values: set[str] = set()
        try:
            for entries in parse_qs(urlsplit(url).query).values():
                values.update(str(item) for item in entries if item)
        except ValueError:
            pass
        for key in ("courseId", "classId", "chapterId", "nodeId", "pageId", "course", "page"):
            value = state.get(key)
            if value not in (None, ""):
                values.add(str(value))
        return values

    def _navigate_course_target(self, item: dict[str, Any], generation: int, timeout: float = 25.0) -> str:
        url = str(item.get("url") or "")
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "ua.dgut.edu.cn" or "/learnCourse" not in parts.path:
            raise RuntimeError("所选条目没有可验证的真实课件地址")
        self.course_scan.set_open_state("opening", selected_id=item["id"])
        try:
            targets = self.api._list_targets()
        except RuntimeError:
            if not self.start_browser("https://ua.dgut.edu.cn"):
                raise RuntimeError("无法启动程序管理的浏览器")
            deadline = time.monotonic() + 8
            targets = []
            while time.monotonic() < deadline and not targets:
                try:
                    targets = self.api._list_targets()
                except RuntimeError:
                    time.sleep(0.2)
        target = next((entry for entry in targets if entry.get("type") == "page" and "ua.dgut.edu.cn/learnCourse" in str(entry.get("url") or "")), None)
        if target is None:
            self._open_debug_tab(url)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                target = next((entry for entry in self.api._list_targets() if entry.get("type") == "page" and str(entry.get("url") or "") == url), None)
                if target:
                    break
                time.sleep(0.15)
        if not target or not target.get("webSocketDebuggerUrl"):
            raise RuntimeError("创建课件标签页超时")
        ws = create_connection(str(target["webSocketDebuggerUrl"]), timeout=10, enable_multithread=True)
        message_id = 0
        try:
            def call(method: str, params: dict, call_timeout: float = 10) -> dict:
                nonlocal message_id
                message_id += 1
                ws.settimeout(call_timeout)
                ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
                while True:
                    result = json.loads(ws.recv())
                    if result.get("id") == message_id:
                        if result.get("error"):
                            raise RuntimeError("浏览器拒绝打开所选课件")
                        return result.get("result") or {}
            self.course_scan.set_open_state("waiting_page")
            call("Page.enable", {})
            if self.token:
                for cookie in (
                    {"name": "AUTHORIZATION", "httpOnly": True},
                    {"name": "token", "httpOnly": False},
                ):
                    cookie_result = call("Network.setCookie", {
                        **cookie, "value": self.token, "domain": ".dgut.edu.cn",
                        "path": "/", "secure": True,
                    })
                    if cookie_result.get("success") is False:
                        raise RuntimeError("浏览器未能恢复学校页面登录状态")
            call("Page.navigate", {"url": url})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if generation != self._auto_course_generation:
                    raise RuntimeError("用户已停止自动启动")
                self.course_scan.set_open_state("validating")
                expression = """(function(){try{var o=function(v){return typeof v==='function'?v():v};var r=window.koLearnCourseViewModel;var c=r&&o(r.course),p=r&&o(r.currentPage);return {ready:!!(r&&p),url:location.href,courseId:String(c&&o(c.id)||''),classId:String(c&&o(c.classId)||''),pageId:String(p&&o(p.id)||''),course:String(c&&o(c.id)||''),page:String(p&&o(p.id)||'')};}catch(e){return {ready:false,url:location.href}}})()"""
                response = call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, 5)
                state = ((response.get("result") or {}).get("value") or {})
                current_url = str(state.get("url") or "")
                if urlsplit(current_url).hostname not in {"ua.dgut.edu.cn"}:
                    if "login" in current_url.lower():
                        raise AuthRequiredError("登录已失效")
                if state.get("ready"):
                    expected = {str(item[key]) for key in ("courseId", "classId", "chapterId", "pageId") if item.get(key)}
                    actual = self._target_identifiers(current_url, state)
                    if expected and not expected.issubset(actual):
                        raise RuntimeError("页面与所选课件不匹配，未启动刷课")
                    return str(target["webSocketDebuggerUrl"])
                time.sleep(0.25)
        finally:
            ws.close()
        raise RuntimeError("课件页面加载超时")

    def auto_open_course(self, item_id: str, start: Callable[[], bool]) -> bool:
        """后台打开并校验一个条目；互斥与代际号阻止重复启动和停止后复活。"""
        if not self._auto_course_lock.acquire(blocking=False):
            return False
        try:
            if self._course_controller and self._course_controller._running:
                raise RuntimeError("已有刷课任务正在运行，请先停止当前任务")
            item = self.course_scan.item(item_id)
            if not item:
                raise RuntimeError("所选课件已不在当前扫描结果中，请刷新后重试")
            if not item.get("canAutoOpen"):
                raise RuntimeError(str(item.get("unavailableReason") or "该课件不能自动打开"))
            self._auto_course_generation += 1
            generation = self._auto_course_generation
            ws_url = self._navigate_course_target(item, generation)
            self.course_scan.set_open_state("connected")
            self.course_controller.preferred_ws_url = ws_url
            self.course_controller.preferred_page_id = str(item.get("pageId") or "*")
            if generation != self._auto_course_generation:
                raise RuntimeError("用户已停止自动启动")
            self.course_scan.set_open_state("starting")
            if not start():
                raise RuntimeError("课件已打开，但现有刷课控制器启动失败")
            self.course_scan.set_open_state("started")
            return True
        except AuthRequiredError as error:
            self._discard_credentials()
            self.course_scan.set_open_state("auth_required", selected_id=item_id, error=str(error))
            return False
        except Exception as error:
            if "用户已停止自动启动" in str(error):
                self.course_scan.reset_open_state()
                return False
            phase = "timeout" if "超时" in str(error) else "failed"
            self.course_scan.set_open_state(phase, selected_id=item_id, error=str(error))
            return False
        finally:
            self._auto_course_lock.release()

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

    def _apply_token(
        self, token: str, user_id: int | None, *, display_name: str = "", account_name: str = "",
    ) -> None:
        self.token = token
        self.user_id = user_id
        self.display_name = display_name.strip()[:80]
        self.account_name = account_name.strip()[:80]
        self.headers["Authorization"] = token

    def _discard_credentials(self) -> None:
        """丢弃已被服务端或登录跳转拒绝的内存会话。"""
        rejected = self.token or str(self.headers.get("Authorization") or "")
        if rejected:
            if self._load_login_cache().get("token") == rejected:
                try:
                    self._login_cache_path.unlink(missing_ok=True)
                except OSError:
                    pass
        self.token = ""
        self.user_id = None
        self.display_name = ""
        self.account_name = ""
        self.headers.pop("Authorization", None)

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
        return cls._browser_identity(cookies)[0]

    @staticmethod
    def _identity_text(value: Any) -> str:
        if value in (None, "") or isinstance(value, (dict, list)):
            return ""
        return " ".join(str(value).split())[:80]

    @classmethod
    def _browser_identity(cls, cookies: list[dict]) -> tuple[int | None, str, str]:
        direct = cls._browser_cookie(cookies, "userid")
        user_id = int(direct) if direct.isdigit() else None
        raw_user = cls._browser_cookie(cookies, "USERINFO")
        try:
            value = json.loads(unquote(raw_user)) if raw_user else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        containers = [value] + [
            value[key] for key in ("user", "userInfo", "data") if isinstance(value.get(key), dict)
        ]
        if user_id is None:
            raw_id = next((container.get("userId") for container in containers if container.get("userId") not in (None, "")), None)
            try:
                user_id = int(raw_id) if raw_id is not None else None
            except (TypeError, ValueError):
                user_id = None
        display_keys = ("realName", "name", "studentName", "nickName", "userName")
        account_keys = ("loginName", "username", "userNo", "studentNo", "studentId", "account")
        display_name = next((cls._identity_text(container.get(key)) for container in containers for key in display_keys if cls._identity_text(container.get(key))), "")
        account_name = next((cls._identity_text(container.get(key)) for container in containers for key in account_keys if cls._identity_text(container.get(key))), "")
        return user_id, display_name, account_name

    def login_status(self) -> dict[str, Any]:
        """返回可展示的当前身份；不向前端暴露 Token、Cookie 或密码。"""
        authenticated = bool(self.token and self.courses)
        label = self.display_name or self.account_name or (str(self.user_id) if self.user_id is not None else "")
        return {
            "authenticated": authenticated,
            "displayName": self.display_name,
            "accountName": self.account_name,
            "userId": self.user_id,
            "greeting": f"你好，{label}！" if authenticated and label else ("你好！" if authenticated else ""),
        }

    def _relogin_with_account(self) -> bool:
        """使用用户明确保存的账号密码重新获取 Token；每次调用只尝试一次。"""
        account = self._load_account()
        if not (account.get("enabled") and account.get("username") and account.get("password")):
            return False
        self._log("本次登录已失效，正在尝试可选的账号密码重新登录…", "info")
        try:
            # 使用学校当前有效的登录入口；appapi/user/login/app 已不下发任何 Cookie。
            response = requests.post(
                LOGIN_URL,
                data={"loginName": account["username"], "password": account["password"]},
                headers=login_headers(),
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
            response_cookies = [{"name": key, "value": value} for key, value in response.cookies.items()]
            if raw_user and not any(str(item.get("name")).casefold() == "userinfo" for item in response_cookies):
                response_cookies.append({"name": "USERINFO", "value": raw_user})
            user_id, display_name, account_name = self._browser_identity(response_cookies)
            self._apply_token(
                token, user_id, display_name=display_name,
                account_name=account_name or str(account.get("username") or ""),
            )
            self._log("账号密码重新登录成功，已更新本次登录会话。", "success")
            return True
        except requests.RequestException as error:
            self._log(f"账号密码重新登录失败：{error}", "warn")
            return False

    @staticmethod
    def _is_unauthorized(error: Exception) -> bool:
        return "401" in str(error)

    @classmethod
    def _is_login_rejected(cls, error: Exception) -> bool:
        return cls._is_unauthorized(error) or isinstance(error, BrowserLoginRedirectError)

    def refresh_courses(self) -> bool:
        """只用本次运行已取得的登录信息，通过浏览器刷新课程。"""
        if not self.token:
            self._log("请先从浏览器读取当前登录信息。", "muted")
            return False
        self._log("正在通过浏览器刷新当前账号的课程列表…", "info")
        try:
            self.courses = self._fetch_courses()
        except Exception as error:
            self._log(f"浏览器课程请求失败：{error}", "warn")
            if self._is_login_rejected(error):
                self._discard_credentials()
            if not self._is_unauthorized(error) or not self._relogin_with_account():
                return False
            try:
                self.courses = self._fetch_courses()
            except Exception as retry_error:
                self._log(f"重新登录后仍无法读取课程：{retry_error}", "warn")
                return False
        if not self.courses:
            self._log("未读取到课程，请检查账号与网络状态。", "warn")
            return False
        self._save_login_cache()
        self._log(f"已读取 {len(self.courses)} 门课程，请在下方选择。", "success")
        self.resume_course_recovery()
        self.homework_scan.refresh_archive()
        return True

    def browser_candidates(self) -> list[tuple[str, list[str]]]:
        """返回按推荐顺序排列的 Chromium 浏览器常见安装位置。"""
        return list(extra_browser_candidates().items())

    def _browser_report(self, message: str, progress: Callable[[str], None] | None = None) -> None:
        self._log(message, "info", "app")
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
            self._warn_existing_neo_port_mismatch()
            return True
        path, name = self.find_browser()
        if not path:
            self._log("未找到可用浏览器。请打开设置并手动选择浏览器程序。", "warn")
            return False
        port = int(self.config.debug_port)
        self._log(f"正在以远程调试模式启动 {name}…", "info")
        debug_profile = self.root / "browser_profile"
        command = [path, f"--remote-debugging-port={port}", "--remote-allow-origins=*", f"--user-data-dir={debug_profile}", "--no-first-run", "--no-default-browser-check"]
        if is_browseros_neo(path, name or ""):
            # neo 的 MCP 服务使用独立的 CDP 设置，必须与小皮卡指定的调试端口一致。
            command.append(f"--browseros-cdp-port={port}")
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

    def _warn_existing_neo_port_mismatch(self) -> None:
        """复用旧 neo 时无法更改其启动参数，提示用户自行重启浏览器。"""
        if not is_browseros_neo(self.config.browser_path, self.config.browser_name):
            return
        try:
            data = json.loads((self.root / "browser_profile" / ".browseros" / "config.json").read_text(encoding="utf-8"))
            cdp_port = data.get("ports", {}).get("cdp")
        except (OSError, ValueError, AttributeError, TypeError):
            return
        if type(cdp_port) is int and 1024 <= cdp_port <= 65535 and cdp_port != int(self.config.debug_port):
            self._log("当前 neo 浏览器仍使用旧的 MCP 调试端口；请关闭小皮卡启动的浏览器后重新打开。", "warn")

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

    def _load_login_cache(self) -> dict:
        try:
            saved = json.loads(self._login_cache_path.read_text(encoding="utf-8"))
            if saved.get("token") and 0 <= time.time() - saved["saved_at"] < 24 * 60 * 60:
                return saved
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return {}

    def _save_login_cache(self) -> None:
        previous = self._load_login_cache()
        saved = {
            "token": self.token, "user_id": self.user_id,
            "display_name": self.display_name, "account_name": self.account_name,
            "saved_at": previous["saved_at"] if previous.get("token") == self.token else time.time(),
        }
        try:
            self._login_cache_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            self._log(f"保存登录缓存失败：{error}", "warn")

    def _restore_browser_login(self, ws_url: str, token: str, user_id: int | None) -> None:
        cookies = [
            {"name": "AUTHORIZATION", "value": token, "httpOnly": True},
            {"name": "token", "value": token, "httpOnly": False},
        ]
        if user_id is not None:
            cookies.append({"name": "userid", "value": str(user_id), "httpOnly": False})
        for cookie in cookies:
            cookie.update(domain=".dgut.edu.cn", path="/", secure=True)
        ws = create_connection(ws_url, timeout=10)
        try:
            ws.send(json.dumps({"id": 1, "method": "Storage.setCookies", "params": {"cookies": cookies}}))
            while True:
                value = json.loads(ws.recv())
                if value.get("id") == 1:
                    if value.get("error"):
                        raise RuntimeError("浏览器拒绝恢复登录缓存")
                    return
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
        candidate_token = self._browser_cookie(dgut, "AUTHORIZATION")
        cached = self._load_login_cache()
        from_cache = not candidate_token or (automatic and candidate_token == self._rejected_browser_token)
        if from_cache and cached and cached["token"] != self._rejected_browser_token:
            candidate_token = cached["token"]
            try:
                self._restore_browser_login(ws_url, candidate_token, cached.get("user_id"))
            except Exception as error:
                if not automatic:
                    self._log(f"恢复浏览器登录缓存失败：{error}", "warn")
                return False
        if not candidate_token:
            if not automatic:
                self._log("未检测到有效登录状态，请在浏览器中完成优学院登录。", "warn")
            return False
        if automatic and candidate_token == self._rejected_browser_token:
            return False

        previous_token, previous_user_id = self.token, self.user_id
        previous_display_name, previous_account_name = self.display_name, self.account_name
        previous_authorization = self.headers.get("Authorization")
        self.headers["Authorization"] = candidate_token
        try:
            courses = self._fetch_courses()
            pending = self.sign_monitor.membership.pending
            if not courses and pending:
                # 临时退出的课程可能是唯一课程。重启后仍须用当前浏览器凭据
                # 核对原账号，再恢复班级，不能为了等课程列表而卡住恢复。
                identity = self.api.json("POST", IDENTITY_URL, json={})
                raw_id = str(identity.get("userId") or "")
                if identity.get("code") == 1 and raw_id.isdigit() and int(raw_id) == pending["userId"]:
                    _, display_name, account_name = self._browser_identity(dgut)
                    self._apply_token(candidate_token, int(raw_id), display_name=display_name, account_name=account_name)
                    self._save_login_cache()
                    self.courses = []
                    self._rejected_browser_token = ""
                    self._log("原账号身份已验证，正在继续恢复原班级。", "info")
                    self.resume_course_recovery()
                    # 尚无可选课程，前端继续检测，待班级恢复后再读取课程。
                    return False
        except Exception as error:
            rejected = self._is_login_rejected(error)
            if rejected:
                self._rejected_browser_token = candidate_token
            if rejected and (not previous_token or candidate_token == previous_token):
                self._discard_credentials()
            else:
                self.token, self.user_id = previous_token, previous_user_id
                self.display_name, self.account_name = previous_display_name, previous_account_name
                if previous_authorization:
                    self.headers["Authorization"] = previous_authorization
                else:
                    self.headers.pop("Authorization", None)
            if not automatic:
                self._log(f"获取课程列表失败：{error}", "warn")
            return False
        if not courses:
            self.token, self.user_id = previous_token, previous_user_id
            self.display_name, self.account_name = previous_display_name, previous_account_name
            if previous_authorization:
                self.headers["Authorization"] = previous_authorization
            else:
                self.headers.pop("Authorization", None)
            if not automatic:
                self._log("没有读取到课程，请确认登录账号与网络状态。", "warn")
            return False
        self.token = candidate_token
        self.user_id, self.display_name, self.account_name = self._browser_identity(dgut)
        if cached and cached["token"] == candidate_token:
            self.user_id, self.display_name, self.account_name = cached.get("user_id"), cached.get("display_name", ""), cached.get("account_name", "")
        self.courses = courses
        self._rejected_browser_token = ""
        self._save_login_cache()
        self._log("登录信息验证成功，课程列表读取完成。", "success")
        self._log(f"已读取 {len(self.courses)} 门课程，请在下方选择。", "success")
        self.resume_course_recovery()
        self.homework_scan.refresh_archive()
        return True

    def select_course(self, query: str) -> Course | None:
        """只允许选择一门课程；模糊匹配有歧义时要求用户细化输入。"""
        if self.sign_monitor.membership.pending or (self.monitor_thread and self.monitor_thread.is_alive()):
            return None
        keyword = query.strip()
        if not keyword or "," in keyword or "，" in keyword:
            return None
        matches = [course for course in self.courses if keyword == str(course.id) or keyword.lower() in course.name.lower()]
        if len(matches) != 1:
            return None
        self.selected_course = matches[0]
        return self.selected_course

    def clear_selected_course(self) -> None:
        if self.sign_monitor.membership.pending or (self.monitor_thread and self.monitor_thread.is_alive()):
            return
        self.selected_course = None

    def select_course_id(self, course_id: str) -> Course | None:
        if self.sign_monitor.membership.pending or (self.monitor_thread and self.monitor_thread.is_alive()):
            return None
        course = next((item for item in self.courses if str(item.id) == course_id), None)
        if course is not None:
            self.selected_course = course
        return course

    def _request(self, method: str, url: str, **kwargs) -> BrowserResponse:
        return self.api.request(method, url, **kwargs)

    @staticmethod
    def _redact(value: object) -> str:
        """避免服务端错误信息把常见凭据原样写进长期日志。"""
        return redact(value)

    @property
    def stop_event(self) -> threading.Event:
        return self.sign_monitor.stop_event

    @property
    def monitor_lock(self) -> threading.Lock:
        return self.sign_monitor.lock

    @property
    def monitor_thread(self) -> threading.Thread | None:
        return self.sign_monitor.thread

    @property
    def monitor_state(self) -> MonitorState:
        return self.sign_monitor.state

    @property
    def monitor_round(self) -> int:
        return self.sign_monitor.round

    @property
    def monitor_interval(self) -> int:
        return self.sign_monitor.interval

    @property
    def monitor_started_at(self) -> str:
        return self.sign_monitor.started_at

    @property
    def monitor_last_check(self) -> str:
        return self.sign_monitor.last_check

    @property
    def monitor_last_result(self) -> str:
        return self.sign_monitor.last_result

    def sign_monitor_status(self) -> dict[str, Any]:
        """返回供前端原地刷新的轻量状态，避免把每轮检查打印成日志。"""
        return self.sign_monitor.status()

    def start_monitor(self) -> bool:
        return self.sign_monitor.start()

    def stop_monitor(self) -> None:
        self.sign_monitor.stop()
