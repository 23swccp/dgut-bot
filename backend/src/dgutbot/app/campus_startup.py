"""Windows 登录后打开东莞理工学院校园网登录页。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from dgutbot.app.app_paths import is_frozen, resource_root


DGUT_LOGIN_URL = "https://login.dgut.edu.cn/eportal/index.jsp"
RUN_VALUE_NAME = "DgutBotCampusLogin"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def open_campus_login() -> bool:
    """立即使用 Windows 默认浏览器打开固定校园网登录入口。"""
    if os.name != "nt":
        return False
    os.startfile(DGUT_LOGIN_URL)  # type: ignore[attr-defined]
    return True


def campus_startup_command() -> str:
    """生成当前安装/开发环境可用的当前用户启动命令。"""
    if is_frozen():
        executable = str(Path(sys.executable).resolve())
        return f'"{executable}" --campus-login'
    interpreter = shutil.which("pythonw.exe") or sys.executable
    script = resource_root() / "scripts" / "campus_login_startup.pyw"
    return f'"{interpreter}" "{script}"'


def configure_campus_login_startup(enabled: bool) -> None:
    """在 HKCU 注册或移除登录启动项，不需要管理员权限。"""
    if os.name != "nt":
        raise OSError("开机打开校园网登录页目前仅支持 Windows")
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, campus_startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
            except FileNotFoundError:
                pass
