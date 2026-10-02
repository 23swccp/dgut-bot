"""构建期 sitecustomize：修复 Windows 商店版 Python 失效的 sys.executable 别名。

仅在通过 scripts/freeze.py 注入 PYTHONPATH 时生效；
可执行路径正常的环境中是空操作。PyInstaller 的主进程与隔离子进程
都需要此修复，否则 compat.py 导入期的 getsize 检测会崩溃。
"""

import os
import sys


def _fix_executable() -> None:
    exe = getattr(sys, "_base_executable", None) or sys.executable
    if exe and os.path.exists(exe):
        return
    basename = os.path.basename(exe or "python.exe")
    candidates = (
        os.path.join(sys.base_prefix, basename),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", basename),
    )
    for candidate in candidates:
        try:
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                sys.executable = candidate
                sys._base_executable = candidate  # type: ignore[attr-defined]
                return
        except OSError:
            continue


_fix_executable()
