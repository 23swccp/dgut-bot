"""跨环境运行 PyInstaller 的入口包装。

Windows 商店版 Python 在部分机器上 sys.executable / sys._base_executable
指向已失效的应用别名：PyInstaller.compat 导入期的 getsize 检测会崩溃，
构建期隔离子进程也无法从该路径 CreateProcess。
本包装注入 scripts/_pyi_boot/sitecustomize.py（主进程与隔离子进程共用），
再进入 PyInstaller。普通 python.org 安装与虚拟环境不受影响。

用法：python scripts/freeze.py <pyinstaller 参数...>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BOOT_DIR = Path(__file__).resolve().parent / "_pyi_boot"


def main() -> None:
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(BOOT_DIR)] + ([existing] if existing else [])
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)
    # 当前进程在解释器启动时没有加载 sitecustomize，这里手动应用同样的修复。
    sys.path.insert(0, str(BOOT_DIR))
    import sitecustomize  # noqa: E402

    sitecustomize._fix_executable()  # noqa: SLF001 - 本仓库自带的引导模块
    from PyInstaller.__main__ import run

    run()


if __name__ == "__main__":
    main()
