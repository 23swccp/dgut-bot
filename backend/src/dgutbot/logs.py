"""data.md 运行日志：事件、命令与签到详情统一写盘。

设计边界：
- 只写本机文件，不参与任何网络请求，也不回传给学校或第三方。
- 先脱敏再落盘；调用方传入的文本都要经过 :func:`redact`。
- 单文件超过 :data:`MAX_BYTES` 时轮转为同目录的 ``*.1.md``，只保留一份历史。
- 写盘失败绝不影响主流程：调用方负责吞掉异常。
"""

from __future__ import annotations

import os
import platform
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# 单文件上限。超过后当前文件改名为 <name>.1.md，新文件继续写。
MAX_BYTES = 2 * 1024 * 1024

_LOCK = threading.RLock()
_STATE = {"header": False}

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


def _rotate(path: Path) -> None:
    """只在持锁时调用；轮转失败就继续往原文件写。"""
    try:
        if path.stat().st_size < MAX_BYTES:
            return
        backup = path.with_name(f"{path.stem}.1{path.suffix}")
        os.replace(path, backup)
    except OSError:
        pass


def append_block(path: Path, lines: list[str]) -> None:
    """整块追加，轮转与并发写都在同一把锁里完成。"""
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        with path.open("a", encoding="utf-8") as file:
            file.writelines(lines)


def append_block_with_header(path: Path, lines: list[str], header: str) -> None:
    """首次写入（或轮转、删档之后）先落一条会话头，方便把一次运行分开。"""
    with _LOCK:
        _rotate(path)
        try:
            fresh = not _STATE["header"] or not path.exists() or path.stat().st_size == 0
        except OSError:
            fresh = not _STATE["header"]
        if fresh:
            append_block(path, [header])
            _STATE["header"] = True
        append_block(path, lines)


def session_header() -> str:
    try:
        from version import APP_VERSION

        version = APP_VERSION
    except Exception:  # 版本号缺失不应阻断日志
        version = "unknown"
    return (
        f"\n## 运行开始 · {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"- 版本 {version} · Python {platform.python_version()} · {platform.platform()}\n\n"
    )


def stamp(moment: object = "") -> str:
    """事件时间戳统一成本地可读格式，解析失败就用当前时间。"""
    text = str(moment or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return f"{datetime.now():%Y-%m-%d %H:%M:%S}"


class RunLogSink:
    """把事件流里的每条事件写成一行，供真实环境排查。"""

    def __init__(self, resolve: Callable[[], tuple[bool, Path]]) -> None:
        self._resolve = resolve

    def on_event(self, event: dict[str, Any]) -> None:
        try:
            enabled, path = self._resolve()
            if not enabled:
                return
            append_block_with_header(path, [self.line(event)], session_header())
        except Exception:  # 日志失败不影响主流程
            pass

    @staticmethod
    def line(event: dict[str, Any]) -> str:
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