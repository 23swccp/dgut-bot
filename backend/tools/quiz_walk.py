"""实战辅助：在章节内循环 下一页 → 清弹窗 → 测验自动作答。

用法：python quiz_walk.py [--option=C] [--judgment=错误] [--rounds=8]
只处理单选/判断/多选占位能覆盖的题；填空题跳过并继续前进。
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from dgutbot.course.yxy_quiz import QuizHandler, StandaloneBackend

NAV_JS = """
(function() {
  const b = document.querySelector('.next-page-btn');
  if (!b) return JSON.stringify({found: false});
  const r = b.getBoundingClientRect();
  if (r.width <= 0) return JSON.stringify({found: false});
  return JSON.stringify({found: true, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)});
})()
"""

ACTIVE_NODE_JS = "var n = document.querySelector('.page-name.active'); n ? n.innerText.trim() : 'none'"


def main() -> int:
    args = sys.argv[1:]
    rounds = 8
    option_label, judgment_label = "C", "错误"
    for arg in args:
        if arg.startswith("--rounds="):
            rounds = int(arg.split("=", 1)[1])
        elif arg.startswith("--option="):
            option_label = arg.split("=", 1)[1].upper()
        elif arg.startswith("--judgment="):
            judgment_label = arg.split("=", 1)[1]

    backend = StandaloneBackend(dry_run=False, log=print)
    handler = QuizHandler(
        evaluate=backend.evaluate,
        click=backend.click,
        type_text=backend.type_text,
        log=print,
        dry_run=False,
    )

    for round_index in range(1, rounds + 1):
        print(f"\n===== 第 {round_index} 轮 =====", flush=True)
        node = backend.evaluate(ACTIVE_NODE_JS)
        print("当前节点：", node, flush=True)

        # 先清一次可能残留的弹窗
        state = handler.read_state()
        if state.modal is not None:
            handler.handle_modal(state.modal, {"modals": 0}, advance=True)
            state = handler.read_state()

        if state.present and any(not q.finished for q in state.questions):
            summary = handler.answer_all(option_label=option_label, judgment_label=judgment_label)
            print(f"测验结果：{summary}", flush=True)
            time.sleep(random.uniform(1.5, 3.0))

        nav_raw = backend.evaluate(NAV_JS)
        nav = json.loads(nav_raw) if isinstance(nav_raw, str) else None
        if not nav or not nav.get("found"):
            print("没有下一页了，结束。", flush=True)
            break
        print(f"点击下一页 {nav}", flush=True)
        backend.click(nav["x"], nav["y"])
        time.sleep(random.uniform(2.0, 3.5))

        # 翻页后的链式弹窗（未答完确认 / 章节统计 / 走神检测）
        for _ in range(4):
            state = handler.read_state()
            if state.modal is None:
                break
            handler.handle_modal(state.modal, {"modals": 0}, advance=True)
            time.sleep(1.0)
    print("\n实战循环结束。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
