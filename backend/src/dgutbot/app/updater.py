"""Velopack 更新服务到现有浏览器前端状态协议的薄适配层。

Python 请求层负责系统代理和 GitHub 传输；版本比较、下载锁、完整性校验、
安装与重启由 Velopack SDK/Update.exe 实现。
"""

from __future__ import annotations

import json
import queue
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
import velopack

from .update_transport import UpdateTransport


GITHUB_NETWORK_HINT = "请开启支持 GitHub 的网络加速器后重试。"
CHECK_TIMEOUT_SECONDS = 40.0
CHECK_RETRY_DELAY_SECONDS = 1.0


def is_transient_update_error(error: Exception) -> bool:
    text = str(error).lower()
    return isinstance(error, (TimeoutError, requests.ConnectionError, requests.Timeout)) or any(
        word in text for word in ("timeout", "timed out", "connection", "dns", "network", "resolve host")
    ) or bool(re.search(r"\b(408|429|500|502|503|504)\b", text))


def update_error_message(error: Exception) -> str:
    message = str(error) or error.__class__.__name__
    text = message.lower()
    if any(word in text for word in ("not properly installed", "not installed", "auto-locate app manifest")):
        return "当前运行环境缺少 Velopack 安装信息。源码运行可以检查更新，但不能安装；已安装的程序请使用完整安装器重新安装。"
    if "未配置" in message:
        return message
    if re.search(r"\b404\b", text):
        return f"GitHub Release 缺少更新清单或更新包，请联系维护者检查发布附件。详情：{message}"
    if re.search(r"\b(403|429)\b", text):
        return f"更新服务器拒绝访问或请求过于频繁，请稍后重试。详情：{message}"
    if is_transient_update_error(error):
        return f"{message}；{GITHUB_NETWORK_HINT}"
    return message


def select_targets_to_close(targets: list[dict[str, Any]], base_url: str) -> list[dict[str, Any]]:
    """只选择与助手本地地址精确同源的 Chromium 页面。"""
    expected = urlparse(base_url)
    selected: list[dict[str, Any]] = []
    for target in targets:
        if target.get("type") != "page":
            continue
        current = urlparse(str(target.get("url", "")))
        if (
            current.scheme == expected.scheme
            and (current.hostname or "").lower() == (expected.hostname or "").lower()
            and current.port == expected.port
        ):
            selected.append(target)
    return selected


def close_assistant_tabs(debug_port: int, base_url: str) -> int:
    """通过 CDP 精确关闭助手页面，不结束用户的其它浏览器标签。"""
    targets = requests.get(f"http://127.0.0.1:{debug_port}/json", timeout=2).json()
    selected = select_targets_to_close(targets, base_url)
    if not selected:
        return 0
    from websocket import create_connection

    version = requests.get(f"http://127.0.0.1:{debug_port}/json/version", timeout=2).json()
    socket = create_connection(version["webSocketDebuggerUrl"], timeout=5)
    closed = 0
    try:
        for request_id, target in enumerate(selected, start=1):
            socket.send(json.dumps({
                "id": request_id,
                "method": "Target.closeTarget",
                "params": {"targetId": target.get("id")},
            }))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                response = json.loads(socket.recv())
                if response.get("id") == request_id:
                    closed += int(response.get("result", {}).get("success") is not False)
                    break
    finally:
        socket.close()
    return closed


class UpdateManager:
    """把 Velopack 的同步 Python API映射为原有前端所需状态。"""

    def __init__(
        self,
        root: Path,
        *,
        version: str,
        repository: str,
        emit_event: Callable[..., dict[str, Any]] | None = None,
        debug_port: Callable[[], int] | None = None,
        manager_factory: Callable[[], Any] | None = None,
        transport: UpdateTransport | None = None,
    ) -> None:
        self.root = Path(root)
        self.version = version
        self.repository = repository
        self.emit_event = emit_event or (lambda *args, **kwargs: {})
        self.debug_port = debug_port or (lambda: 9222)
        self.base_url = "http://127.0.0.1:8765"
        self._manager_factory = manager_factory or self._default_manager
        self._custom_manager = manager_factory is not None
        self._transport = transport or UpdateTransport()
        self._notes_lock = threading.Lock()
        self._notes_fetching: set[str] = set()
        self._notes_cache: dict[str, str] = {}
        self._notes_path = self.root / "update-release-notes.json"
        try:
            saved = json.loads(self._notes_path.read_text(encoding="utf-8"))
            if saved.get("repository") == repository:
                self._notes_cache = {k: v for k, v in saved.get("notes", {}).items()
                                     if isinstance(k, str) and isinstance(v, str)}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self._manager: Any | None = None
        self._pending: Any | None = None
        self._lock = threading.RLock()
        self._manager_lock = threading.Lock()
        self._check_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._exit_callback: Callable[[], None] = lambda: None
        self._stop_backend_callback: Callable[[], None] = lambda: None
        self._messages_path = self.root / "update-ui-state.json"
        self._state: dict[str, Any] = {
            "state": "idle",
            "latestVersion": "",
            "changelog": "",
            "downloaded": 0,
            "total": 0,
            "percent": 0,
            "error": "",
            "messages": [],
            "lastMessageId": 0,
        }

    def _default_manager(self) -> Any:
        if not self.repository:
            raise RuntimeError("未配置 GitHub 更新仓库")
        # 此 manager 仅用于安装定位、恢复和移交，不执行远程检查/下载。
        return self._native_manager(velopack.HttpSource(self._feed_url().rsplit("/", 1)[0]))

    def _native_manager(self, source: Any) -> Any:
        return velopack.UpdateManager(source)

    def _feed_url(self) -> str:
        if not self.repository:
            raise RuntimeError("未配置 GitHub 更新仓库")
        return f"https://github.com/{self.repository}/releases/latest/download/releases.win.json"

    def _package_url(self, asset: Any) -> str:
        from urllib.parse import quote
        return f"https://github.com/{self.repository}/releases/download/v{quote(str(asset.Version), safe='')}/{quote(asset.FileName, safe='')}"

    def _query_update(self) -> Any:
        if self._custom_manager:
            return self._get_manager().check_for_updates()
        feed = self._transport.read(self._feed_url())
        with tempfile.TemporaryDirectory(prefix="dgut-update-feed-") as directory:
            source = str(Path(directory))
            (Path(directory) / "releases.win.json").write_bytes(feed)
            try:
                manager = self._native_manager(source)
            except Exception as error:
                if not self._missing_install(error):
                    raise
                # 仅给 SDK 提供当前版本做比较。临时定位绝不用于下载或安装。
                with tempfile.TemporaryDirectory(prefix="dgut-update-check-") as directory:
                    root = Path(directory)
                    (root / "packages").mkdir()
                    (root / "Update.exe").touch()
                    from xml.sax.saxutils import escape
                    (root / "sq.version").write_text(
                        '<package><metadata><id>DgutBot</id><version>' + escape(self.version) +
                        '</version><title>DgutBot</title><authors>DgutBot</authors>'
                        '<mainExe>DgutBot.exe</mainExe><channel>win</channel></metadata></package>', encoding="utf-8")
                    locator = velopack.VelopackLocatorConfig(root, root / "Update.exe", root / "packages",
                                                          root / "sq.version", root, True)
                    return velopack.UpdateManager(source, locator=locator).check_for_updates()
            return manager.check_for_updates()

    @staticmethod
    def _missing_install(error: Exception) -> bool:
        return any(word in str(error).lower() for word in ("not properly installed", "not installed", "auto-locate app manifest"))

    def _download_update(self, update: Any, progress: Callable[[int], None]) -> None:
        if self._custom_manager:
            self._get_manager().download_updates(update, progress)
            return
        # 下载前确认真实安装定位；源码检查生成的临时定位不会保存在这里。
        self._get_manager()
        asset = update.TargetFullRelease
        name = asset.FileName
        if not name or name in {".", ".."} or any(c in name for c in '/\\:%?#') or name.endswith((" ", ".")):
            raise ValueError("更新包文件名无效")
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="dgut-update-package-", dir=self.root) as directory:
            source = Path(directory)
            self._transport.download(self._package_url(asset), source / name, int(asset.Size), progress)
            self._set("verifying")
            # 本地目录源避免 SDK 再经过任何代理。取完整包，仍由 SDK 校验后放入安装缓存。
            full_update = velopack.UpdateInfo(asset, [], update.IsDowngrade)
            self._native_manager(str(source)).download_updates(full_update, lambda value: progress(95 + int(value) // 20))

    def _refresh_notes(self, asset: Any) -> None:
        if self._custom_manager:
            return
        version = str(asset.Version)
        with self._notes_lock:
            if version in self._notes_fetching:
                return
            self._notes_fetching.add(version)

        def fetch() -> None:
            try:
                notes = self._transport.release_notes(self.repository, version)
                if notes is None:
                    return
                with self._lock:
                    self._notes_cache[version] = notes
                    self._notes_cache = dict(list(self._notes_cache.items())[-5:])
                    if self._state["latestVersion"] == version:
                        self._state["changelog"] = notes
                    payload = {"repository": self.repository, "notes": dict(self._notes_cache)}
                with self._notes_lock:
                    self.root.mkdir(parents=True, exist_ok=True)
                    temporary = self._notes_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(self._notes_path)
            except Exception:
                pass  # 说明是可选信息，限流或网络失败不能改变版本检查/下载状态。
            finally:
                with self._notes_lock:
                    self._notes_fetching.discard(version)

        threading.Thread(target=fetch, name="update-release-notes", daemon=True).start()

    def _notes(self, asset: Any) -> str:
        with self._lock:
            return self._notes_cache.get(str(asset.Version), str(getattr(asset, "NotesMarkdown", "") or ""))

    def _get_manager(self) -> Any:
        # SDK 初始化不能占用 UI 状态锁，否则超时和状态轮询也会被阻塞。
        with self._manager_lock:
            if self._manager is None:
                self._manager = self._manager_factory()
            return self._manager

    def _set(self, state: str | None = None, **values: Any) -> None:
        with self._lock:
            if state is not None:
                self._state["state"] = state
            self._state.update(values)

    def _add_message(self, kind: str, title: str, body: str) -> None:
        with self._lock:
            next_id = int(self._state["lastMessageId"]) + 1
            self._state["lastMessageId"] = next_id
            self._state["messages"].insert(0, {
                "id": next_id,
                "kind": kind,
                "title": title,
                "body": body,
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "read": False,
            })
            self._state["messages"] = self._state["messages"][:50]
        self._persist_messages()

    def _persist_messages(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = {
                    "messages": self._state["messages"],
                    "lastMessageId": self._state["lastMessageId"],
                }
            temporary = self._messages_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            temporary.replace(self._messages_path)
        except OSError:
            pass

    def restore(self) -> None:
        try:
            saved = json.loads(self._messages_path.read_text(encoding="utf-8"))
            with self._lock:
                self._state["messages"] = list(saved.get("messages") or [])[:50]
                self._state["lastMessageId"] = int(saved.get("lastMessageId") or 0)
        except (OSError, ValueError, TypeError):
            pass
        try:
            pending = self._get_manager().get_update_pending_restart()
        except Exception:
            pending = None
        if pending is not None:
            self._pending = pending
            self._set(
                "ready_to_install",
                latestVersion=str(pending.Version),
                changelog=self._notes(pending),
                total=int(getattr(pending, "Size", 0) or 0),
                downloaded=int(getattr(pending, "Size", 0) or 0),
                percent=100,
            )
            self._refresh_notes(pending)

    def set_ports(self, base_url: str, _frontend_port: int) -> None:
        self.base_url = base_url

    def set_exit_callback(
        self,
        callback: Callable[[], None],
        stop_backend: Callable[[], None] | None = None,
    ) -> None:
        """设置服务退出通知，供前端请求丢失时的看门狗兜底。"""
        self._exit_callback = callback
        self._stop_backend_callback = stop_backend or (lambda: None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            messages = [dict(item) for item in state["messages"]]
        return {
            "currentVersion": self.version,
            "state": state["state"],
            "latestVersion": state["latestVersion"],
            "publishedAt": "",
            "changelog": state["changelog"],
            "downloaded": state["downloaded"],
            "total": state["total"],
            "percent": state["percent"],
            "error": state["error"],
            "messages": messages,
            "unreadCount": sum(not item.get("read", False) for item in messages),
            "downloading": state["state"] in {"downloading", "verifying"},
            "handoff": state["state"] in {"handoff", "waiting_for_exit"},
            "readyForExit": state["state"] == "waiting_for_exit",
            "canInstall": state["state"] == "ready_to_install",
            "canRetryDownload": state["state"] in {"available", "download_failed"},
            "pendingFailureDialog": None,
        }

    def check(self, manual: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._state["state"] in {"checking", "downloading", "verifying", "ready_to_install", "handoff", "waiting_for_exit"}:
                if manual and self._state["state"] == "ready_to_install" and self._pending is not None:
                    self._refresh_notes(getattr(self._pending, "TargetFullRelease", self._pending))
                return {"ok": True, "skipped": True}
            if self._check_thread and self._check_thread.is_alive():
                # Python 无法取消 SDK 的原生请求。保留单个请求，避免并发借用
                # 同一个 SDK manager 或每次重试都泄漏一个线程。
                message = "上次更新请求仍未结束，请稍后重试；若持续无响应，请重启程序。"
                self._set(error=message)
                return {"ok": False, "error": message}
            previous_state = self._state["state"]
            self._set("checking", error="")

        # 后台线程只返回结果；超时后绝不能自行修改状态或触发自动下载。
        results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def query() -> None:
            try:
                try:
                    pending = self._get_manager().get_update_pending_restart()
                except Exception as error:
                    if self._custom_manager or not self._missing_install(error):
                        raise
                    pending = None
                update = None
                if pending is None:
                    for attempt in range(2):
                        try:
                            update = self._query_update()
                            break
                        except Exception as error:
                            if attempt or not is_transient_update_error(error):
                                raise
                            if self._stop.wait(CHECK_RETRY_DELAY_SECONDS):
                                raise RuntimeError("更新检查已停止") from None
                results.put((True, (pending, update)))
            except Exception as error:
                results.put((False, error))

        try:
            with self._lock:
                self._check_thread = threading.Thread(target=query, name="velopack-check-sdk", daemon=True)
                self._check_thread.start()
            try:
                ok, result = results.get(timeout=CHECK_TIMEOUT_SECONDS)
            except queue.Empty:
                raise TimeoutError(f"连接 GitHub 超时（{CHECK_TIMEOUT_SECONDS:g} 秒），请检查网络后重试；若持续无响应，请重启程序。") from None
            if not ok:
                raise result
            pending, update = result
            return self._apply_check_result(pending, update, manual=manual)
        except Exception as error:
            display = update_error_message(error)
            self._set(previous_state, error=f"检查更新失败：{display}")
            if manual:
                self._add_message("error", "检查更新失败", display)
            return {"ok": False, "error": display, "retryable": is_transient_update_error(error)}

    def _apply_check_result(self, pending: Any, update: Any, *, manual: bool) -> dict[str, Any]:
        if pending is not None:
            self._pending = pending
            asset = pending
            self._set("ready_to_install", latestVersion=str(asset.Version), changelog=self._notes(asset),
                      downloaded=int(asset.Size or 0), total=int(asset.Size or 0), percent=100)
            self._refresh_notes(asset)
            return {"ok": True, "updateAvailable": True, "version": str(asset.Version), "downloaded": True}
        if update is None:
            self._pending = None
            self._set("idle", latestVersion="", changelog="", downloaded=0, total=0, percent=0, error="")
            if manual:
                self._add_message("info", f"已是最新版本 v{self.version}", "当前没有可用的更新。")
            return {"ok": True, "updateAvailable": False}
        self._pending = update
        asset = update.TargetFullRelease
        self._set("available", latestVersion=str(asset.Version), changelog=self._notes(asset),
                  downloaded=0, total=int(asset.Size or 0), percent=0, error="")
        self._add_message("info", f"发现新版本 v{asset.Version}", str(asset.NotesMarkdown or "本次更新。")[:200])
        self._refresh_notes(asset)
        # 发现版本只通知前端，用户确认后通过 download_update 开始下载。
        return {"ok": True, "updateAvailable": True, "version": str(asset.Version)}

    def start_auto_check(self, delay: float = 5.0) -> None:
        def delayed() -> None:
            # 启动时可能尚未联网；仅对网络类故障进行有限次数的延后重试。
            for wait in (delay, 30.0, 120.0):
                if self._stop.wait(wait):
                    return
                result = self.check(manual=False)
                if result.get("ok") or not result.get("retryable"):
                    return

        threading.Thread(target=delayed, name="velopack-auto-check", daemon=True).start()

    def start_download(self) -> dict[str, Any]:
        with self._lock:
            if self._state["state"] in {"checking", "downloading", "verifying", "ready_to_install", "handoff", "waiting_for_exit"}:
                return {"ok": True, "skipped": True}
            if self._check_thread and self._check_thread.is_alive():
                return {"ok": False, "error": "上次更新请求仍未结束，请稍后重试；若持续无响应，请重启程序。"}
        if self._pending is None:
            threading.Thread(target=lambda: self.check(manual=True), name="velopack-check", daemon=True).start()
            return {"ok": True, "checking": True}
        self._start_download_thread()
        return {"ok": True}

    def _start_download_thread(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._download_worker, name="velopack-download", daemon=True)
        self._worker.start()

    def _download_worker(self) -> None:
        update = self._pending
        if update is None:
            return
        total = int(getattr(getattr(update, "TargetFullRelease", update), "Size", 0) or 0)
        self._set("downloading", downloaded=0, total=total, percent=0, error="")

        def progress(percent: int) -> None:
            value = max(0, min(100, int(percent)))
            self._set(downloaded=round(total * value / 100), total=total, percent=value)

        try:
            self._download_update(update, progress)
        except Exception as error:
            display = update_error_message(error)
            self._add_message("error", f"⚠ v{self._state['latestVersion']} 下载失败", display)
            self._set("download_failed", error=display)
            return
        self._add_message("success", f"✓ v{self._state['latestVersion']} 已由 Velopack 下载并校验", "可以随时安装。")
        self._set("ready_to_install", downloaded=total, total=total, percent=100, error="")

    def install(self) -> dict[str, Any]:
        if self.snapshot()["state"] != "ready_to_install" or self._pending is None:
            return {"ok": False, "error": "当前没有已下载待安装的更新"}
        threading.Thread(target=self._install_worker, name="velopack-apply", daemon=True).start()
        return {"ok": True}

    def _install_worker(self) -> None:
        self._set("handoff", error="")
        try:
            self._get_manager().wait_exit_then_apply_updates(self._pending, silent=False, restart=True)
        except Exception as error:
            message = str(error) or error.__class__.__name__
            self._set("ready_to_install", error=f"无法启动 Velopack 更新器：{message}")
            return
        self._set("waiting_for_exit")
        self.emit_event("UPDATE_HANDOFF", "success", "update", "Velopack 已接管更新，正在退出应用……")
        threading.Thread(target=self._exit_watchdog, name="velopack-exit-watchdog", daemon=True).start()

    def _exit_watchdog(self) -> None:
        if not self._stop.wait(10):
            self.shutdown_for_update(self._stop_backend_callback)
            self._exit_callback()

    def shutdown_for_update(self, stop_backend: Callable[[], None]) -> dict[str, Any]:
        if self.snapshot()["state"] != "waiting_for_exit":
            return {"ok": False, "error": "Velopack 更新器尚未接管，已取消退出"}
        try:
            stop_backend()
        except Exception as error:
            self.emit_event("UPDATE_STOP_FAILED", "warning", "update", f"停止后台任务失败：{error}")
        try:
            close_assistant_tabs(self.debug_port(), self.base_url)
        except Exception as error:
            self.emit_event("UPDATE_TAB_CLOSE_FAILED", "warning", "update", f"关闭助手标签页失败：{error}")
        return {"ok": True}

    def mark_read(self) -> None:
        with self._lock:
            for message in self._state["messages"]:
                message["read"] = True
        self._persist_messages()

    def ack_failure(self) -> None:
        return

    def stop(self) -> None:
        self._stop.set()
