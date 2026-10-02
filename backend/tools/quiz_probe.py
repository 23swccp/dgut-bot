"""只读课件采样入口；TabConnection 保留供浏览器模拟器使用。"""
from __future__ import annotations

import json
import time
from websocket import create_connection


class TabConnection:
    """一个标签页的所有 CDP 会话共用一条 websocket（flattened 模式）。"""

    def __init__(self, ws_url: str) -> None:
        self.ws = create_connection(ws_url, timeout=15)
        self._next_id = 0

    def send(self, method: str, params: dict | None = None, session_id: str | None = None, timeout: float = 10.0):
        self._next_id += 1
        msg: dict = {"id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        deadline = time.monotonic() + timeout
        while True:
            self.ws.settimeout(max(0.05, deadline - time.monotonic()))
            raw = json.loads(self.ws.recv())
            if raw.get("id") == self._next_id:
                return raw.get("result"), raw.get("error")
            # 其他响应/事件暂存子会话登记
            if raw.get("method") == "Target.attachedToTarget":
                info = raw["params"]["targetInfo"]
                ATTACHED[info["targetId"]] = raw["params"]["sessionId"]


ATTACHED: dict[str, str] = {}  # target_id -> session_id


def main() -> int:
    try:
        from .course_capture import main as capture_main
    except ImportError:
        from course_capture import main as capture_main
    return capture_main()


if __name__ == "__main__":
    raise SystemExit(main())
