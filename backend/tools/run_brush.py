"""刷课控制器实战驱动：独立进程运行 CourseController，输出到 stdout。"""

import sys
import time
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from dgutbot.course.yxy_course import CourseConfig, CourseController


def emit(text: str, kind: str = "info") -> None:
    print(f"[{kind}] {text}", flush=True)


def main() -> int:
    controller = CourseController(emit)
    if not controller.start(CourseConfig()):
        return 1
    try:
        while controller.state_machine.state.value not in ("STOPPED", "COMPLETED"):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
