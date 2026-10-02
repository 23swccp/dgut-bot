"""Windows 浏览器路径清理与系统注册位置查询，不启动浏览器。"""

from __future__ import annotations

import os
import stat
import time
from collections import deque
from pathlib import Path
from typing import Callable, Iterator


BROWSER_NAMES = {
    "msedge.exe": "Microsoft Edge",
    "chrome.exe": "Google Chrome",
    "brave.exe": "Brave",
    "vivaldi.exe": "Vivaldi",
    "launcher.exe": "Opera",
    "opera.exe": "Opera",
    "360chromex.exe": "360 极速浏览器",
    "360chrome.exe": "360 极速浏览器",
    "qqbrowser.exe": "QQ 浏览器",
    "sogouexplorer.exe": "搜狗高速浏览器",
    "360se.exe": "360 安全浏览器",
}

BROWSER_INSTALLATIONS = {
    "Microsoft Edge": ("Microsoft/Edge", ("msedge.exe",)),
    "Google Chrome": ("Google/Chrome", ("chrome.exe",)),
    "Brave": ("BraveSoftware/Brave-Browser", ("brave.exe",)),
    "Vivaldi": ("Vivaldi", ("vivaldi.exe",)),
    "Opera": ("Opera", ("launcher.exe", "opera.exe")),
    "Chromium": ("Chromium", ("chrome.exe",)),
    "360 极速浏览器": ("360ChromeX/Chrome", ("360chromex.exe", "360chrome.exe")),
    "QQ 浏览器": ("Tencent/QQBrowser", ("qqbrowser.exe",)),
    "搜狗高速浏览器": ("SogouExplorer", ("sogouexplorer.exe",)),
    "360 安全浏览器": ("360/360se6", ("360se.exe",)),
    "Opera GX": ("Opera GX", ("launcher.exe", "opera.exe")),
}

# 同一浏览器不同版本或安装方式的目录，仍使用同一名称和检测顺序。
BROWSER_INSTALLATION_ALIASES = {
    "Opera": ("Opera Software/Opera Stable",),
    "360 极速浏览器": ("360ChromeX", "360Chrome/Chrome", "360Chrome"),
    "QQ 浏览器": ("QQBrowser",),
    "搜狗高速浏览器": ("Sogou/SogouExplorer",),
    "360 安全浏览器": ("360se6", "360/360Browser/Browser", "360Browser/Browser"),
}


def scan_browser_directory(
    root: Path,
    executables: tuple[str, ...],
    deadline: float,
    report: Callable[[str], None],
    *,
    max_depth: int = 3,
    max_directories: int = 128,
    max_entries: int = 4096,
) -> Iterator[str]:
    """只在指定安装目录内广度优先查找，不跟随符号链接或 Windows 联接。"""
    queue = deque([(root, 0)])
    directories = entries = 0
    while queue:
        if time.monotonic() >= deadline:
            report("检测超时：尚未检查完安装目录，可手动填写浏览器路径。")
            return
        if directories >= max_directories or entries >= max_entries:
            report(f"扫描达到数量上限：{root}；可手动指定更具体的目录。")
            return
        directory, depth = queue.popleft()
        directories += 1
        try:
            attributes = directory.lstat()
            if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & 0x400:
                report(f"跳过链接目录：{directory}")
                continue
            report(f"扫描目录：{directory}")
            # 先直接查找，避免浏览器本体排在大量资源文件之后。
            for executable in executables:
                if time.monotonic() >= deadline:
                    report("检测超时：尚未检查完安装目录，可手动填写浏览器路径。")
                    return
                candidate = directory / executable
                if candidate.is_file():
                    yield str(candidate.resolve())
                    return
            if depth >= max_depth:
                continue
            with os.scandir(directory) as children:
                for child in children:
                    entries += 1
                    if time.monotonic() >= deadline:
                        report("检测超时：尚未检查完安装目录，可手动填写浏览器路径。")
                        return
                    if entries >= max_entries:
                        report(f"扫描达到数量上限：{root}；可手动指定更具体的目录。")
                        return
                    if child.is_dir(follow_symlinks=False):
                        if len(queue) + directories >= max_directories:
                            report(f"扫描达到目录数量上限：{root}；其余子目录已跳过。")
                            break
                        queue.append((Path(child.path), depth + 1))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            report(f"无法检查目录：{directory}（{type(error).__name__}：{error}）")


def browser_scan_roots(paths: list[str]) -> list[Path]:
    """从候选程序路径提取浏览器自己的目录，绝不递归扫描整个磁盘。"""
    roots = []
    for value in paths:
        path = Path(value).parent
        if path.name.lower() in {"application", "app"}:
            path = path.parent
        key = os.path.normcase(str(path))
        if key not in {os.path.normcase(str(root)) for root in roots}:
            roots.append(path)
    return roots


def normalize_browser_path(value: str) -> str:
    # Explorer 的“复制为路径”会带引号，部分复制来源还会带方向标记。
    value = value.translate(dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff")))
    value = value.strip().strip('"\'“”').strip()
    return os.path.expanduser(os.path.expandvars(value)) if value else ""


def resolve_browser_path(value: str) -> str:
    value = normalize_browser_path(value)
    if not value:
        return ""
    candidate = Path(value)
    try:
        if candidate.is_dir():
            # 接受浏览器顶层文件夹、App/Application 或便携版的嵌套目录。
            return next(scan_browser_directory(
                candidate, tuple(BROWSER_NAMES), time.monotonic() + 5, lambda _message: None,
            ), "")
        if candidate.suffix.lower() == ".exe" and candidate.is_file():
            return str(candidate.resolve())
    except (OSError, ValueError):
        pass
    return ""


def registered_browser_paths(executable: str) -> list[str]:
    """同时读取当前用户/整机和 32/64 位 App Paths 注册项。"""
    try:
        import winreg
    except ImportError:
        return []
    paths = []
    key_name = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                if isinstance(value, str) and value.strip():
                    paths.append(normalize_browser_path(value))
            except OSError:
                continue
    return list(dict.fromkeys(paths))


def extra_browser_candidates() -> dict[str, list[str]]:
    paths: dict[str, list[str]] = {}
    for executable, name in BROWSER_NAMES.items():
        paths.setdefault(name, []).extend(registered_browser_paths(executable))
    # 环境变量缺失或由 32 位宿主启动时，仍检查系统盘的标准安装位置。
    drive = os.environ.get("SystemDrive") or "C:"
    roots = [
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("ProgramW6432", ""),
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("APPDATA", ""),
        str(Path(os.environ["LOCALAPPDATA"]) / "Programs") if os.environ.get("LOCALAPPDATA") else "",
        str(Path(drive + "\\") / "Program Files"),
        str(Path(drive + "\\") / "Program Files (x86)"),
    ]
    try:
        roots.extend(str(Path.home() / "AppData" / folder) for folder in ("Local", "Roaming"))
    except RuntimeError:
        pass
    # 只枚举本地固定磁盘；不访问网络盘、光驱或可移动盘。
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
        for index in range(26):
            drive_root = f"{chr(65 + index)}:\\"
            if mask & (1 << index) and kernel32.GetDriveTypeW(drive_root) == 3:
                roots.extend(str(Path(drive_root) / folder) for folder in ("Program Files", "Program Files (x86)", "Programs"))
    roots = list(dict.fromkeys(root for root in roots if root))
    for name, (relative, executables) in BROWSER_INSTALLATIONS.items():
        candidates = paths.setdefault(name, [])
        for relative_path in (relative, *BROWSER_INSTALLATION_ALIASES.get(name, ())):
            for root in roots:
                for folder in ("Application", "App", ""):
                    candidates.extend(str(Path(root) / relative_path / folder / exe) for exe in executables)
    # 安装目录表是唯一的浏览器顺序来源，注册项不能改变 Edge / Chrome 的优先级。
    return {name: list(dict.fromkeys(paths.get(name, []))) for name in BROWSER_INSTALLATIONS}
