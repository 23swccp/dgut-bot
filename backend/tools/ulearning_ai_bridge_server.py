"""Run an experimental, loopback-only OpenAI-shaped course-AI bridge."""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dgutbot.experimental.ulearning_ai import UlearningAiError  # noqa: E402
from dgutbot.experimental.ulearning_ai_bridge import UlearningAiBridge  # noqa: E402


MAX_REQUEST_BYTES = 1_048_576


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "DgutAiBridge/0.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "Not found."}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "Not found."}})
            return
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": {"message": "Authentication failed."}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("Request size is invalid.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be an object.")
            if payload.get("stream"):
                raise ValueError("Streaming responses are not implemented in this experiment.")
            if payload.get("tools"):
                raise ValueError("Client-defined tools are not implemented yet.")
            reply = self.server.bridge.complete(payload.get("messages"))  # type: ignore[attr-defined]
            completion_id = f"chatcmpl_{uuid4().hex}"
            response = {
                "id": completion_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "ulearning-course-ai",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": reply.text,
                        "reasoning_content": reply.reasoning,
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            self._send_json(HTTPStatus.OK, response)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": str(error)}})
        except UlearningAiError as error:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": {"message": str(error)}})
        except Exception:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": "Bridge request failed."}})

    def _authorized(self) -> bool:
        supplied = str(self.headers.get("Authorization") or "")
        expected = f"Bearer {self.server.auth_token}"  # type: ignore[attr-defined]
        return hmac.compare_digest(supplied.encode(), expected.encode())

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8786, help="Loopback HTTP port")
    parser.add_argument("--debug-port", type=int, default=9222, help="App-launched browser debug port")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    auth_token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), BridgeHandler)
    server.bridge = UlearningAiBridge(debug_port=args.debug_port)  # type: ignore[attr-defined]
    server.auth_token = auth_token  # type: ignore[attr-defined]
    print(json.dumps({
        "baseUrl": f"http://127.0.0.1:{args.port}/v1",
        "apiKey": auth_token,
        "streaming": False,
        "tools": False,
    }))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
