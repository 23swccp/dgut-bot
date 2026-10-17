"""更新专用 HTTP：显式系统代理、有限等待，下载后交给原生 SDK 校验。"""
from __future__ import annotations

import json
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import requests


class UpdateTransport:
    def __init__(self, *, check_timeout: float = 15, download_timeout: float = 1800):
        self.check_timeout = check_timeout
        self.download_timeout = download_timeout

    @staticmethod
    def proxies(url: str) -> dict[str, str]:
        host = urlsplit(url).hostname
        if host in {"127.0.0.1", "localhost", "::1"} or (host and urllib.request.proxy_bypass(host)):
            return {}
        raw = urllib.request.getproxies()
        proxies = {}
        for scheme in ("http", "https"):
            value = raw.get(scheme) or raw.get("all") or raw.get("https") or raw.get("http")
            if value:
                proxies[scheme] = value if "://" in value else "http://" + value
        return proxies

    @contextmanager
    def response(self, url: str, timeout: float):
        # 禁用隐式 netrc/环境代理，再显式应用系统代理，避免匿名更新携带账户认证。
        with requests.Session() as session:
            session.trust_env = False
            session.max_redirects = 5
            try:
                with session.get(url, stream=True, timeout=(min(5, timeout), min(10, timeout)),
                                 proxies=self.proxies(url), headers={"Accept-Encoding": "identity",
                                 "User-Agent": "DgutBot-Updater"}) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"更新请求 HTTP {response.status_code}")
                    yield response
            except requests.Timeout:
                raise requests.Timeout("更新请求超时，请检查系统代理或网络后重试。") from None
            except requests.RequestException as error:
                # requests 异常可能包含代理认证和重定向 URL，不直接返回原始异常。
                raise requests.ConnectionError(f"更新请求连接失败（{type(error).__name__}），请检查系统代理或网络。") from None

    def read(self, url: str, *, timeout: float | None = None, limit: int = 8 * 1024 * 1024) -> bytes:
        timeout = self.check_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        data = bytearray()
        with self.response(url, timeout) as response:
            for chunk in response.iter_content(64 * 1024):
                if time.monotonic() > deadline:
                    raise requests.Timeout("更新请求超过总等待时间。")
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError("更新说明或清单超过大小限制")
        return bytes(data)

    def release_notes(self, repository: str, version: str) -> str | None:
        from urllib.parse import quote
        url = f"https://api.github.com/repos/{repository}/releases/tags/{quote('v' + version, safe='')}"
        data = json.loads(self.read(url, timeout=3, limit=2 * 1024 * 1024))
        if data.get("tag_name") != "v" + version or data.get("draft"):
            raise ValueError("Release 版本不匹配")
        body = data.get("body")
        return body if isinstance(body, str) else None

    def download(self, url: str, path: Path, size: int, progress: Callable[[int], None]) -> None:
        """临时文件不进入安装目录；只有完整传输才交给 SDK 做最终校验。"""
        deadline = time.monotonic() + self.download_timeout
        received = 0
        with self.response(url, self.download_timeout) as response, path.open("wb") as output:
            for chunk in response.iter_content(64 * 1024):
                if time.monotonic() > deadline:
                    raise requests.Timeout("更新包下载超过总等待时间。")
                received += len(chunk)
                if received > size:
                    raise ValueError("更新包大小超过清单记录，请重新检查更新。")
                output.write(chunk)
                progress(min(95, int(received * 95 / max(size, 1))))
        if received != size:
            raise ValueError("更新包传输不完整，请重试下载。")
