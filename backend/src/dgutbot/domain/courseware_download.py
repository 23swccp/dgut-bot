"""课件附件下载；沿用 Requests 的重定向流程并约束鉴权头的目标。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests


def school_download_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme.lower() == "https" and (host == "dgut.edu.cn" or host.endswith(".dgut.edu.cn"))


class CoursewareDownloadSession(requests.Session):
    def rebuild_auth(self, prepared_request, response) -> None:
        super().rebuild_auth(prepared_request, response)
        if not school_download_url(prepared_request.url):
            prepared_request.headers.pop("Authorization", None)
            prepared_request.headers.pop("ua-authorization", None)


def _download_headers(url: str, token: str) -> dict[str, str]:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if token and school_download_url(url):
        headers.update({"Authorization": token, "ua-authorization": token, "version": "1"})
    return headers


def _resource_request_url(url: str, token: str) -> str:
    parts = urlsplit(url)
    if token and school_download_url(url) and (
        parts.path.startswith("/uobs/view/") or parts.hostname == "uobs.dgut.edu.cn"
    ):
        # UA 播放器和文档组件使用 token 查询参数；只用于学校资源服务器。
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "token"]
        query.append(("token", token))
        return urlunsplit(parts._replace(query=urlencode(query)))
    return url


def _save_response(session, url: str, destination: Path, token: str) -> int:
    with session.get(_resource_request_url(url, token), headers=_download_headers(url, token),
                     timeout=120, stream=True) as response:
        response.raise_for_status()
        written = 0
        with destination.open("wb") as output:
            for chunk in response.iter_content(1 << 20):
                if chunk:
                    if not written and chunk.lstrip().startswith(b'{"error":'):
                        message = json.loads(chunk).get("error")
                        raise RuntimeError(f"下载接口返回错误：{message}")
                    output.write(chunk)
                    written += len(chunk)
        return written


def download_file(url: str, destination: Path, *, token: str) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.part")
    try:
        with CoursewareDownloadSession() as session:
            if urlsplit(url).path.lower().endswith(".m3u8"):
                written = _save_hls(session, url, temporary, token)
            else:
                written = _save_response(session, url, temporary, token)
        os.replace(temporary, destination)
        return written
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _hls_attributes(line: str) -> dict[str, str]:
    return {key: value.strip('"') for key, value in re.findall(r'([\w-]+)=("[^"]*"|[^,]*)', line)}


def _save_hls(session, url: str, destination: Path, token: str) -> int:
    """先下载完结 HLS 的播放列表、分段和密钥，再仅用本地文件封装 MP4。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("HLS 音视频合并需要 FFmpeg，请安装并加入 PATH")
    with tempfile.TemporaryDirectory(prefix=".hls-", dir=destination.parent) as staging:
        root = Path(staging)
        assets: dict[str, str] = {}
        playlists: dict[str, str] = {}

        def asset(source: str) -> str:
            if source not in assets:
                name = f"asset-{len(assets):05d}{Path(urlsplit(source).path).suffix}"
                _save_response(session, source, root / name, token)
                assets[source] = name
            return assets[source]

        def playlist(source: str) -> str:
            if source in playlists:
                return playlists[source]
            name = f"playlist-{len(playlists):04d}.m3u8"
            playlists[source] = name
            with session.get(_resource_request_url(source, token), headers=_download_headers(source, token),
                             timeout=120) as response:
                response.raise_for_status()
                lines = response.content.decode("utf-8-sig").splitlines()
                base = response.url
            variants = []
            for index, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF:"):
                    uri = next((entry.strip() for entry in lines[index + 1:] if entry.strip() and not entry.startswith("#")), "")
                    variants.append((_hls_attributes(line), line, uri))
            if variants:
                attributes, stream, uri = max(variants, key=lambda item: int(item[0].get("BANDWIDTH", "0")))
                output = ["#EXTM3U"]
                audio = [line for line in lines if line.startswith("#EXT-X-MEDIA:")
                         and _hls_attributes(line).get("TYPE") == "AUDIO"
                         and _hls_attributes(line).get("GROUP-ID") == attributes.get("AUDIO")
                         and "URI" in _hls_attributes(line)]
                if audio:
                    chosen = next((line for line in audio if _hls_attributes(line).get("DEFAULT") == "YES"), audio[0])
                    local = playlist(urljoin(base, _hls_attributes(chosen)["URI"]))
                    output.append(re.sub(r'URI="[^"]*"', f'URI="{local}"', chosen))
                output.extend([stream, playlist(urljoin(base, uri))])
            else:
                if "#EXT-X-ENDLIST" not in lines:
                    raise RuntimeError("HLS 尚未完结，请在课程发布完整音视频后重试")
                output = []
                for line in lines:
                    if line.startswith("#"):
                        line = re.sub(r'URI="([^"]*)"', lambda match: f'URI="{asset(urljoin(base, match[1]))}"', line)
                    elif line.strip():
                        line = asset(urljoin(base, line.strip()))
                    output.append(line)
            (root / name).write_text("\n".join(output) + "\n", encoding="utf-8")
            return name

        local = playlist(url)
        result = subprocess.run([
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file,crypto,data",
            "-allowed_extensions", "ALL", "-i", str(root / local), "-map", "0:v?", "-map", "0:a?",
            "-c", "copy", "-f", "mp4", "-y", str(destination),
        ], capture_output=True)
        if result.returncode:
            raise RuntimeError("HLS 合并失败：" + result.stderr.decode("utf-8", errors="replace")[:500])
        return destination.stat().st_size
