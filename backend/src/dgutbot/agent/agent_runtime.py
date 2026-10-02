"""Secure discovery file for the single local service instance."""

from __future__ import annotations

import json
import os
import secrets
import stat
import csv
import subprocess
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

from dgutbot.agent.agent_protocol import AgentError, SCHEMA_VERSION
from dgutbot.app.app_paths import agent_runtime_root


RUNTIME_FILENAME = "agent-runtime.json"


@dataclass(frozen=True)
class RuntimeInfo:
    schema_version: int
    pid: int
    port: int
    instance_id: str
    auth_token: str
    started_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "pid": self.pid,
            "port": self.port,
            "instanceId": self.instance_id,
            "authToken": self.auth_token,
            "startedAt": self.started_at,
        }


def runtime_path(root: Path | None = None) -> Path:
    return (root or agent_runtime_root()) / RUNTIME_FILENAME


def new_runtime(port: int, *, pid: int | None = None) -> RuntimeInfo:
    return RuntimeInfo(
        SCHEMA_VERSION,
        pid or os.getpid(),
        int(port),
        f"instance_{uuid4().hex}",
        secrets.token_urlsafe(32),
        datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def publish_runtime(info: RuntimeInfo, root: Path | None = None) -> Path:
    path = runtime_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if os.name == "nt":
                # Apply a current-user-only DACL BEFORE writing any secret bytes.
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                identity = subprocess.run(
                    ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                    check=True, creationflags=flags, timeout=5,
                )
                sid = list(csv.reader(identity.stdout.decode("utf-8", errors="replace").splitlines()))[-1][-1]
                if not sid.startswith("S-1-"):
                    raise OSError("Cannot restrict runtime file permissions.")
                subprocess.run(
                    ["icacls", str(temporary), "/inheritance:r", "/grant:r", f"*{sid}:F"],
                    capture_output=True, check=True, creationflags=flags, timeout=5,
                )
            else:
                temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
            stream.write(json.dumps(info.as_dict(), ensure_ascii=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def remove_runtime(instance_id: str, root: Path | None = None) -> None:
    path = runtime_path(root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("instanceId") == instance_id:
            path.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        return


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # os.kill(pid, 0) is NOT a read-only probe on Windows: Python uses
        # TerminateProcess for signals other than CTRL_C/CTRL_BREAK.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of exit.
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def load_runtime(root: Path | None = None, *, verify_pid: bool = True) -> RuntimeInfo:
    path = runtime_path(root)
    try:
        with path.open("rb") as stream:
            content = stream.read(8193)
        if len(content) > 8192:
            raise ValueError("Runtime file too large")
        raw = json.loads(content.decode("utf-8"))
    except FileNotFoundError as error:
        raise AgentError("SERVICE_NOT_RUNNING", "The local service is not running.", retryable=True) from error
    except (OSError, ValueError, UnicodeError) as error:
        raise AgentError("RUNTIME_FILE_INVALID", "The service runtime file is invalid.", retryable=True) from error
    try:
        if not isinstance(raw, dict) or any(type(raw.get(k)) is not int for k in ("schemaVersion", "pid", "port")):
            raise ValueError("Invalid runtime fields")
        info = RuntimeInfo(
            int(raw["schemaVersion"]),
            int(raw["pid"]),
            int(raw["port"]),
            str(raw["instanceId"]),
            str(raw["authToken"]),
            str(raw["startedAt"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AgentError("RUNTIME_FILE_INVALID", "The service runtime file is invalid.", retryable=True) from error
    if info.schema_version != SCHEMA_VERSION or info.pid <= 0 or not (1 <= info.port <= 65535):
        raise AgentError("RUNTIME_FILE_INVALID", "The service runtime file is invalid.", retryable=True)
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", info.auth_token) or not re.fullmatch(r"instance_[A-Za-z0-9_-]{1,128}", info.instance_id):
        raise AgentError("RUNTIME_FILE_INVALID", "The service runtime file is invalid.", retryable=True)
    if verify_pid and not pid_is_running(info.pid):
        raise AgentError("SERVICE_NOT_RUNNING", "The recorded service process is no longer running.", retryable=True)
    return info


def verify_runtime_health(info: RuntimeInfo, timeout: float = 2.0) -> None:
    try:
        with urlopen(f"http://127.0.0.1:{info.port}/api/health", timeout=timeout) as response:
            raw = json.loads(response.read(8193).decode("utf-8"))
    except (OSError, ValueError, URLError, UnicodeError) as error:
        raise AgentError("SERVICE_UNREACHABLE", "The local service cannot be reached.", retryable=True) from error
    if not isinstance(raw, dict) or raw.get("ok") is not True:
        raise AgentError("SERVICE_UNREACHABLE", "The local service cannot be reached.", retryable=True)
