"""Inspect and optionally trigger one benign AI-workbench request through CDP Input.

This is an experimental companion to ``yxy_capture_fixed.py``.  Page JavaScript
only observes element metadata; clicks and text entry always use the CDP Input
domain so the action can be verified from the resulting DOM state.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener

from websocket import create_connection


TARGET_MARKER = "course/workbench"
CONTROL_EXPRESSION = r"""
(() => {
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const controls = [];
  const visited = new Set();
  const visit = (root, framePath) => {
    if (!root || visited.has(root)) return;
    visited.add(root);
    for (const element of root.querySelectorAll('*')) {
      if (element.shadowRoot) visit(element.shadowRoot, `${framePath}/shadow`);
      if (element.tagName === 'IFRAME') {
        try { visit(element.contentDocument, `${framePath}/iframe`); } catch (_) {}
      }
    }
    for (const element of root.querySelectorAll('textarea,input,[contenteditable="true"],button')) {
      if (!visible(element)) continue;
      const rect = element.getBoundingClientRect();
      controls.push({
        index: controls.length,
        framePath,
        tag: element.tagName.toLowerCase(),
        type: element.getAttribute('type') || '',
        placeholder: element.getAttribute('placeholder') || '',
        ariaLabel: element.getAttribute('aria-label') || '',
        title: element.getAttribute('title') || '',
        text: (element.innerText || '').trim().slice(0, 80),
        value: typeof element.value === 'string' ? element.value.slice(0, 80) : '',
        disabled: Boolean(element.disabled),
        rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
      });
    }
  };
  visit(document, 'top');
  return controls;
})()
"""


class Connection:
    def __init__(self, url: str) -> None:
        self.ws = create_connection(url, timeout=5, enable_multithread=True)
        self.command_id = 0

    def close(self) -> None:
        self.ws.close()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.command_id += 1
        command_id = self.command_id
        self.ws.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == command_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} failed: {message['error'].get('message', 'unknown error')}")
                return message.get("result") or {}


def find_target(port: int) -> dict[str, Any]:
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        targets = json.loads(response.read())
    matches = [
        target for target in targets
        if target.get("type") == "page" and TARGET_MARKER in str(target.get("url") or "")
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one AI workbench tab, found {len(matches)}")
    return matches[0]


def child_frames(tree: dict[str, Any]):
    for child in tree.get("childFrames") or []:
        yield child
        yield from child_frames(child)


def find_ai_frame(connection: Connection) -> dict[str, Any]:
    tree = connection.call("Page.getFrameTree").get("frameTree") or {}
    matches = []
    for node in child_frames(tree):
        frame = node.get("frame") or {}
        hostname = (urlsplit(str(frame.get("url") or "")).hostname or "").lower()
        if hostname == "aijx.dgut.edu.cn":
            matches.append(frame)
    if len(matches) != 1:
        raise RuntimeError(f"Expected one AI child frame, found {len(matches)}")
    return matches[0]


def safe_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def frame_context(connection: Connection, frame_id: str) -> int:
    result = connection.call("Page.createIsolatedWorld", {
        "frameId": frame_id,
        "worldName": "dgut-ai-probe",
        "grantUniveralAccess": False,
    })
    return int(result["executionContextId"])


def frame_offset(connection: Connection, frame_id: str) -> tuple[float, float]:
    owner = connection.call("DOM.getFrameOwner", {"frameId": frame_id})
    box = connection.call("DOM.getBoxModel", {"backendNodeId": owner["backendNodeId"]}).get("model") or {}
    content = box.get("content") or []
    if len(content) < 2:
        raise RuntimeError("The AI frame has no visible content box")
    return float(content[0]), float(content[1])


def read_controls(connection: Connection, context_id: int) -> list[dict[str, Any]]:
    result = connection.call("Runtime.evaluate", {
        "expression": CONTROL_EXPRESSION,
        "returnByValue": True,
        "contextId": context_id,
    })
    value = (result.get("result") or {}).get("value")
    if not isinstance(value, list):
        raise RuntimeError("The workbench did not return a control inventory")
    return value


def center(control: dict[str, Any]) -> tuple[float, float]:
    rect = control["rect"]
    return rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2


def click(connection: Connection, control: dict[str, Any], offset: tuple[float, float]) -> None:
    x, y = center(control)
    x += offset[0]
    y += offset[1]
    connection.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    connection.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})


def choose_editor(controls: list[dict[str, Any]]) -> dict[str, Any]:
    editors = [item for item in controls if item["tag"] == "textarea" and not item["disabled"]]
    if len(editors) != 1:
        raise RuntimeError(f"Expected one visible textarea, found {len(editors)}")
    return editors[0]


def editor_value(controls: list[dict[str, Any]]) -> str | None:
    editors = [item for item in controls if item["tag"] == "textarea" and not item["disabled"]]
    if len(editors) != 1:
        return None
    return str(editors[0].get("value") or "")


def choose_send_button(controls: list[dict[str, Any]], editor: dict[str, Any]) -> dict[str, Any]:
    editor_rect = editor["rect"]
    candidates = []
    for item in controls:
        if item["tag"] != "button" or item["disabled"]:
            continue
        rect = item["rect"]
        vertically_inside = editor_rect["y"] <= rect["y"] <= editor_rect["y"] + editor_rect["height"] + 80
        right_side = rect["x"] >= editor_rect["x"] + editor_rect["width"] * 0.65
        if vertically_inside and right_side:
            candidates.append(item)
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one send button near the editor, found {len(candidates)}")
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--prompt", help="Send one diagnostic prompt after listing controls")
    args = parser.parse_args()

    target = find_target(args.port)
    connection = Connection(target["webSocketDebuggerUrl"])
    try:
        frame = find_ai_frame(connection)
        context_id = frame_context(connection, frame["id"])
        offset = frame_offset(connection, frame["id"])
        controls = read_controls(connection, context_id)
        print(json.dumps({"frame": safe_url(frame["url"]), "controls": controls}, ensure_ascii=False, indent=2))
        if not args.prompt:
            return 0
        editor = choose_editor(controls)
        send_button = choose_send_button(controls, editor)
        click(connection, editor, offset)
        connection.call("Input.insertText", {"text": args.prompt})
        populated = read_controls(connection, context_id)
        if editor_value(populated) != args.prompt:
            raise RuntimeError("The editor did not contain the diagnostic prompt")
        click(connection, send_button, offset)
        transitioned = False
        editor_cleared = False
        for _ in range(20):
            time.sleep(0.1)
            try:
                after = read_controls(connection, context_id)
            except RuntimeError:
                transitioned = True
                break
            value = editor_value(after)
            if value is None:
                transitioned = True
                break
            if not value:
                editor_cleared = True
                break
        if not (transitioned or editor_cleared):
            raise RuntimeError("The AI workbench did not acknowledge the send action")
        print(json.dumps({"sent": True, "acknowledged": True}, ensure_ascii=False))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
