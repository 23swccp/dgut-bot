"""Detect the open LMS homework editor and type into it through CDP Input."""

from __future__ import annotations

import json
import threading
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from websocket import WebSocketException, create_connection


MAX_HOMEWORK_CHARS = 5000
HOMEWORK_HOST = "lms.dgut.edu.cn"

PROBE_JS = r"""(() => {
  try {
    const frames = [...document.querySelectorAll('iframe.tox-edit-area__iframe')]
      .filter(frame => {
        const box = frame.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
      });
    if (frames.length !== 1) return {ready: false, reason: frames.length ? '页面有多个作业输入框' : '作业输入框尚未打开'};
    const frame = frames[0];
    const body = frame.contentDocument?.body;
    const editor = window.tinymce?.get(frame.id.replace(/_ifr$/, ''));
    if (!body || !editor || editor.getBody() !== body || body.getAttribute('contenteditable') !== 'true'
        || editor.mode?.get() !== 'design') return {ready: false, reason: '作业输入框尚未就绪'};
    const box = frame.getBoundingClientRect();
    if (box.left >= innerWidth || box.top >= innerHeight || box.right <= 0 || box.bottom <= 0)
      return {ready: false, reason: '请把作业输入框滚动到可见区域'};
    return {ready: true, x: Math.max(1, box.left) + 20, y: Math.max(1, box.top) + 20,
      pasteBlocked: typeof editor.settings?.paste_preprocess === 'function',
      textLength: editor.getContent({format: 'text'}).length};
  } catch (_) { return {ready: false, reason: '无法读取作业输入框'}; }
})()"""

EDITOR_TEXT_JS = r"""(() => {
  const frame = document.querySelector('iframe.tox-edit-area__iframe');
  const editor = frame && window.tinymce?.get(frame.id.replace(/_ifr$/, ''));
  return editor && editor.getBody() === frame.contentDocument?.body
    ? editor.getContent({format: 'text'}) : null;
})()"""

FOCUS_JS = r"""(() => {
  const frame = document.querySelector('iframe.tox-edit-area__iframe');
  return !!frame && document.activeElement === frame
    && frame.contentDocument?.activeElement === frame.contentDocument?.body;
})()"""

EDITOR_HTML_JS = r"""(() => {
  const frame = document.querySelector('iframe.tox-edit-area__iframe');
  const editor = frame && window.tinymce?.get(frame.id.replace(/_ifr$/, ''));
  return editor && editor.getBody() === frame.contentDocument?.body
    ? editor.getBody().innerHTML : null;
})()"""


class HomeworkInput:
    def __init__(self, debug_port: Callable[[], int]):
        self._debug_port = debug_port
        self._send_lock = threading.Lock()
        self._opener = build_opener(ProxyHandler({}))

    def _targets(self) -> list[dict[str, Any]]:
        with self._opener.open(f"http://127.0.0.1:{self._debug_port()}/json/list", timeout=3) as response:
            targets = json.load(response)
        return [target for target in targets if self._is_homework_target(target)]

    @staticmethod
    def _is_homework_target(target: dict[str, Any]) -> bool:
        if target.get("type") != "page" or not target.get("webSocketDebuggerUrl"):
            return False
        parsed = urlsplit(str(target.get("url") or ""))
        return parsed.scheme == "https" and parsed.hostname == HOMEWORK_HOST and parsed.path.startswith("/homework/")

    @staticmethod
    def _call(ws: Any, message_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}, ensure_ascii=False))
        while True:
            response = json.loads(ws.recv())
            if response.get("id") != message_id:
                continue
            if response.get("error"):
                raise RuntimeError("浏览器输入连接失败，请重新检测作业输入框")
            return response.get("result") or {}

    @classmethod
    def _evaluate(cls, ws: Any, message_id: int, expression: str) -> Any:
        result = cls._call(ws, message_id, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if result.get("exceptionDetails"):
            raise RuntimeError("作业页面状态已变化，请重新检测")
        return (result.get("result") or {}).get("value")

    def _probe(self, target: dict[str, Any]) -> dict[str, Any]:
        ws = create_connection(str(target["webSocketDebuggerUrl"]), timeout=5)
        try:
            result = self._evaluate(ws, 1, PROBE_JS)
            return result if isinstance(result, dict) else {"ready": False, "reason": "作业页面未就绪"}
        finally:
            ws.close()

    def _ready_target(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        targets = self._targets()
        if not targets:
            return None, {"state": "missing", "message": "请在程序浏览器中打开优学院的写作业页面"}
        ready: list[tuple[dict[str, Any], dict[str, Any]]] = []
        reasons: list[str] = []
        for target in targets:
            try:
                result = self._probe(target)
            except (OSError, ValueError, RuntimeError, WebSocketException) as error:
                reasons.append(str(error))
                continue
            if result.get("ready"):
                ready.append((target, result))
            else:
                reasons.append(str(result.get("reason") or "作业输入框尚未就绪"))
        if len(ready) > 1:
            return None, {"state": "ambiguous", "message": "检测到多个作业输入框，请只保留要输入的作业页面"}
        if ready:
            target, probe = ready[0]
            return target, {"state": "ready", "message": "已识别可输入的作业框", "pasteBlocked": bool(probe.get("pasteBlocked"))}
        return None, {"state": "missing", "message": reasons[0] if reasons else "作业输入框尚未就绪"}

    def status(self) -> dict[str, Any]:
        try:
            _, status = self._ready_target()
            return status
        except (OSError, ValueError, WebSocketException) as error:
            return {"state": "disconnected", "message": f"无法连接程序浏览器：{error}"}

    def send(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("请输入要发送的作业内容")
        if len(text) > MAX_HOMEWORK_CHARS:
            raise ValueError(f"单次最多输入 {MAX_HOMEWORK_CHARS} 字")
        if not self._send_lock.acquire(blocking=False):
            raise RuntimeError("正在输入上一段内容，请稍候")
        try:
            target, status = self._ready_target()
            if target is None:
                raise RuntimeError(status["message"])
            ws = create_connection(str(target["webSocketDebuggerUrl"]), timeout=8)
            try:
                return self._send_to_editor(ws, text)
            finally:
                ws.close()
        finally:
            self._send_lock.release()

    def _send_to_editor(self, ws: Any, text: str) -> dict[str, Any]:
        probe = self._evaluate(ws, 1, PROBE_JS)
        if not isinstance(probe, dict) or not probe.get("ready"):
            raise RuntimeError("作业输入框已变化，请重新检测")
        before = self._evaluate(ws, 2, EDITOR_TEXT_JS)
        if not isinstance(before, str):
            raise RuntimeError("无法读取作业输入框，请重新检测")
        if len(before) + len(text) > MAX_HOMEWORK_CHARS:
            raise ValueError(f"作业框总字数不能超过 {MAX_HOMEWORK_CHARS} 字")
        self._call(ws, 3, "Page.bringToFront")
        x, y = float(probe["x"]), float(probe["y"])
        self._call(ws, 4, "Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        self._call(ws, 5, "Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        if self._evaluate(ws, 6, FOCUS_JS) is not True:
            raise RuntimeError("作业输入框未获得焦点，请先在网页中点一下输入框")
        end_key = {"key": "End", "code": "End", "modifiers": 2, "windowsVirtualKeyCode": 35}
        self._call(ws, 7, "Input.dispatchKeyEvent", {"type": "rawKeyDown", **end_key})
        self._call(ws, 8, "Input.dispatchKeyEvent", {"type": "keyUp", **end_key})
        previous = before
        message_id = 9
        for line_index, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n")):
            if line_index:
                html_before = self._evaluate(ws, message_id, EDITOR_HTML_JS)
                message_id += 1
                enter = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}
                self._call(ws, message_id, "Input.dispatchKeyEvent", {"type": "keyDown", "text": "\r", "unmodifiedText": "\r", **enter})
                message_id += 1
                self._call(ws, message_id, "Input.dispatchKeyEvent", {"type": "keyUp", **enter})
                message_id += 1
                html_after = self._evaluate(ws, message_id, EDITOR_HTML_JS)
                message_id += 1
                if not isinstance(html_after, str) or html_after == html_before:
                    raise RuntimeError("换行未写入作业框；可能已有部分内容，请检查后再试")
                previous = self._evaluate(ws, message_id, EDITOR_TEXT_JS)
                message_id += 1
                if not isinstance(previous, str):
                    raise RuntimeError("作业输入框已变化；可能已有部分内容，请检查后再试")
            for offset in range(0, len(line), 80):
                chunk = line[offset:offset + 80]
                self._call(ws, message_id, "Input.insertText", {"text": chunk})
                message_id += 1
                actual = self._evaluate(ws, message_id, EDITOR_TEXT_JS)
                message_id += 1
                if not isinstance(actual, str) or actual == previous or not actual.endswith(chunk):
                    raise RuntimeError("输入结果未通过核对；作业框可能已收到部分内容，请检查后再试")
                previous = actual
        return {"state": "sent", "message": f"已输入 {len(text)} 字，请在作业页面核对后自行提交", "count": len(text)}
