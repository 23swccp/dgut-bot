"""源码开发环境的无窗口校园网开机入口。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dgutbot.app.campus_startup import open_campus_login  # noqa: E402


open_campus_login()
