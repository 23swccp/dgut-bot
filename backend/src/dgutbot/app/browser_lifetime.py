"""Observe only the local assistant tab; never close unrelated browser processes."""

import json
import time
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, build_opener


FRONTEND_DISPLAY_PATH = "/(不要删favicon.ico页)"


class BrowserLifetime:
    def __init__(self, debug_port: int, web_port: int):
        self.debug_port, self.web_port = debug_port, web_port
        self.seen = False
        self.present: bool | None = None
        self.missing_since: float | None = None
        self.next_poll = 0.0
        self._opener = build_opener(ProxyHandler({}))

    def _read_presence(self) -> bool | None:
        try:
            with self._opener.open(f"http://127.0.0.1:{self.debug_port}/json/list", timeout=0.5) as response:
                targets = json.loads(response.read(2_000_001))
            if not isinstance(targets, list):
                return None
            for target in targets:
                if not isinstance(target, dict) or target.get("type") != "page":
                    continue
                url = urlsplit(str(target.get("url") or ""))
                if (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"}
                        and url.port == self.web_port
                        and unquote(url.path) in {"", "/", "/index.html", "/ai.html", FRONTEND_DISPLAY_PATH}):
                    return True
            return False
        except (OSError, ValueError):
            return None

    def closed(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if now < self.next_poll:
            return False
        self.next_poll = now + 1.0
        self.present = self._read_presence()
        if self.present:
            self.seen = True
            self.missing_since = None
        elif self.seen:
            if self.missing_since is None:
                self.missing_since = now
            # Two absent snapshots confirm tab closure; allow longer for a transient
            # debugging connection failure / browser process exit without a beacon.
            grace = 1.0 if self.present is False else 3.0
            return now - self.missing_since >= grace
        return False
