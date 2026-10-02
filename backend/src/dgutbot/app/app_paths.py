"""统一的只读资源与持久用户数据路径。

Velopack 更新会整体替换安装目录中的 ``current``，因此配置、登录缓存、
浏览器资料和日志必须放在稳定的 LocalAppData 数据目录。开发模式仍默认
使用源码目录；测试和便携迁移工具可用 ``YXY_DATA_DIR`` 显式覆盖。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """当前进程是否运行在 PyInstaller 冻结环境中。"""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """只读内置资源目录：冻结后为 PyInstaller 解包目录，开发时为源码目录。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parents[4]


def data_root() -> Path:
    """返回不会随 Velopack 更新被替换的用户数据目录。"""
    override = os.environ.get("YXY_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return (base / "DgutBot" / "data").resolve()
    return Path(__file__).resolve().parents[4]


def frontend_dist() -> Path:
    """前端是随版本替换的只读资源，始终从 PyInstaller 资源目录读取。"""
    return resource_root() / "frontend" / "dist"


def agent_runtime_root() -> Path:
    """Keep discovery credentials out of the source tree even in development.

    Installed mode equals data_root(); YXY_DATA_DIR still isolates tests.
    """
    if os.environ.get("YXY_DATA_DIR", "").strip() or is_frozen():
        return data_root()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return (base / "DgutBot" / "data").resolve()
