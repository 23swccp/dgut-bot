"""Windows 命名互斥锁封装：App 锁用于防止后台服务多开。

仅依赖 ctypes；非 Windows 平台退化为进程内字典，保证测试可以在任意环境运行。
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any

APP_MUTEX = r"Local\YxyAssistant.App"

ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000


class MutexBackend:
    """互斥锁后端接口：acquire 返回句柄（已存在时返回 (handle, True)），release 释放。"""

    def acquire(self, name: str) -> tuple[Any, bool] | None:
        raise NotImplementedError

    def release(self, handle: Any) -> None:
        raise NotImplementedError

    def exists(self, name: str) -> bool:
        raise NotImplementedError


class WindowsMutexBackend(MutexBackend):
    """基于 CreateMutexW / ReleaseMutex / OpenMutexW 的真实命名互斥锁。"""

    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self._kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        self._kernel32.OpenMutexW.restype = ctypes.c_void_p
        self._kernel32.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        self._kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    def acquire(self, name: str) -> tuple[Any, bool] | None:
        handle = self._kernel32.CreateMutexW(None, False, name)
        if not handle:
            return None
        already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        return (handle, already)

    def release(self, handle: Any) -> None:
        if handle:
            self._kernel32.ReleaseMutex(handle)
            self._kernel32.CloseHandle(handle)

    def exists(self, name: str) -> bool:
        handle = self._kernel32.OpenMutexW(SYNCHRONIZE, False, name)
        if handle:
            self._kernel32.CloseHandle(handle)
            return True
        return False


class InProcessMutexBackend(MutexBackend):
    """测试与非 Windows 平台的后备实现：同名锁全局共享，进程退出即释放。"""

    _held: dict[str, int] = {}

    def acquire(self, name: str) -> tuple[Any, bool] | None:
        self._held[name] = self._held.get(name, 0) + 1
        return (name, self._held[name] > 1)

    def release(self, handle: Any) -> None:
        name = str(handle)
        count = self._held.get(name, 0) - 1
        if count > 0:
            self._held[name] = count
        else:
            self._held.pop(name, None)

    def exists(self, name: str) -> bool:
        return name in self._held


def default_backend() -> MutexBackend:
    if sys.platform == "win32":
        try:
            return WindowsMutexBackend()
        except (AttributeError, OSError):
            return InProcessMutexBackend()
    return InProcessMutexBackend()


_BACKEND: MutexBackend | None = None


def backend() -> MutexBackend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = default_backend()
    return _BACKEND


def set_backend(used: MutexBackend) -> None:
    """测试注入点。"""
    global _BACKEND
    _BACKEND = used


class NamedMutex:
    """持有某个命名互斥锁的上下文对象；with 语句结束或进程退出时自动释放。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle: Any = None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            return True
        acquired = backend().acquire(self.name)
        if acquired is None:
            return False
        self._handle, already = acquired
        # already=True 表示锁已由其他进程持有；此时我们并没有获得所有权，
        # 立即释放句柄并向调用者报告失败。
        if already:
            backend().release(self._handle)
            self._handle = None
            return False
        return True

    def release(self) -> None:
        if self._handle is not None:
            backend().release(self._handle)
            self._handle = None

    def __enter__(self) -> "NamedMutex":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


def app_mutex_exists() -> bool:
    return backend().exists(APP_MUTEX)
