"""运行日志只存内存；此处提供显式导出的行格式与后台输出适配，不写文件。"""

from __future__ import annotations

import io
import re
import threading
from datetime import datetime
from typing import Any, Callable

SECRET_PATTERNS = (
    r"(?i)(authorization|token|password|cookie)(\s*[:=]\s*)([^\s,;}&]+)",
    r"(?i)(bearer\s+)([A-Za-z0-9._~+\-/]+=*)",
)


def redact(value: object) -> str:
    """避免服务端错误信息把常见凭据原样写进长期日志。"""
    text = str(value)
    for pattern in SECRET_PATTERNS:
        text = re.sub(
            pattern,
            lambda match: f"{match.group(1)}{match.group(2) if match.lastindex and match.lastindex >= 3 else ''}[已隐藏]",
            text,
        )
    return text


def stamp(moment: object = "") -> str:
    """事件时间戳统一成本地可读格式，解析失败就用当前时间。"""
    text = str(moment or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return f"{datetime.now():%Y-%m-%d %H:%M:%S}"


def format_event_line(event: dict[str, Any]) -> str:
    time = stamp(event.get("time"))
    level = str(event.get("level") or "info")
    category = str(event.get("category") or "")
    code = str(event.get("code") or "")
    message = redact(event.get("message") or "").rstrip()
    data = event.get("data") or {}
    extra = ""
    if isinstance(data, dict) and data:
        rendered = ", ".join(
            f"{key}={value}" for key, value in sorted(data.items()) if key != "legacyKind"
        )
        extra = f" · {redact(rendered)}" if rendered else ""
    return f"{time} · {level} · {category or code} · {message}{extra}\n"


class EventLogStream(io.TextIOBase):
    """将后台 stdout/stderr 按行送入有限事件缓冲区，不持有文件句柄。"""

    MAX_LINE_CHARS = 16_384

    def __init__(self, emit_line: Callable[[str], None]) -> None:
        super().__init__()
        self._emit_line = emit_line
        self._pending = ""
        self._lock = threading.RLock()

    @property
    def encoding(self) -> str:
        return "utf-8"

    def writable(self) -> bool:
        return True

    def _send(self, line: str) -> None:
        if line.strip():
            try:
                self._emit_line(redact(line.rstrip("\r")))
            except Exception:  # 诊断输出不能中断业务，也不能递归写 stderr。
                pass

    def write(self, text: str) -> int:
        with self._lock:
            self._pending += text
            while "\n" in self._pending or len(self._pending) >= self.MAX_LINE_CHARS:
                newline = self._pending.find("\n")
                end = min(newline, self.MAX_LINE_CHARS) if newline >= 0 else self.MAX_LINE_CHARS
                line = self._pending[:end]
                self._pending = self._pending[end + (newline == end):]
                self._send(line)
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._pending:
                self._send(self._pending)
                self._pending = ""
