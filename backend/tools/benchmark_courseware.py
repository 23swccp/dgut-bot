"""课件纯解析基准：合成内容、单进程单线程，不访问网络或用户数据。"""

from __future__ import annotations

import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dgutbot.domain.courseware_export import html_to_markdown, parse_chapter


def measure(action: Callable[[], object], *, iterations: int = 20, rounds: int = 7) -> float:
    action()
    samples = []
    for _ in range(rounds):
        started = time.perf_counter()
        for _ in range(iterations):
            action()
        samples.append((time.perf_counter() - started) / iterations)
    return statistics.median(samples)


def main() -> None:
    paragraph = '<p><span><font>课程正文与说明文字</font></span> <strong>重点</strong> <a href="https://example.invalid/doc.pdf">资料</a></p>'
    results = []
    for kib in (5, 15, 53):
        fragment = paragraph * max(1, kib * 1024 // len(paragraph.encode("utf-8")))
        size = len(fragment.encode("utf-8"))
        seconds = measure(lambda: html_to_markdown(fragment))
        results.append({"case": "html_to_markdown", "bytes": size, "ms": round(seconds * 1000, 3),
                        "pagesPerSecond": round(1 / seconds, 1), "MiBPerSecond": round(size / seconds / 2**20, 3)})
    pages = [{"content": f"页面 {index}", "contentType": 5,
              "coursepageDTOList": [{"type": 12, "content": "<p>合成的短正文</p>"}]} for index in range(100)]
    payload = {"wholepageItemDTOList": [{"wholepageDTOList": pages}]}
    seconds = measure(lambda: parse_chapter(payload, chapter_title="基准章节", chapter_index=1))
    results.append({"case": "parse_chapter_100_small_pages", "ms": round(seconds * 1000, 3),
                    "pagesPerSecond": round(100 / seconds, 1)})
    print(json.dumps({"python": platform.python_version(), "platform": platform.platform(),
                      "processor": platform.processor(), "rounds": 7, "iterationsPerRound": 20,
                      "aggregation": "median seconds per invocation", "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
