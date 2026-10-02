"""发行包启动冒烟测试：只验证本地服务、静态资源与生命周期。

- 不访问真实课程、不提交签到、不执行真实刷课。
- 用法：python scripts/smoke.py [PyInstaller onedir 目录]
  不传参数时默认验证 dist/dgut-bot。

验证项：
1. 从发行目录直接启动 dgut-bot.exe（无源码参与）。
2. 本地后端服务在 127.0.0.1:8765 就绪。
3. 首页 HTML 可加载；frontend/dist 的 JS/CSS 正常返回。
4. 前后端 API 可通信（get_app_info/get_settings）。
5. 能检测到 Edge 或 Chrome。
6. 配置写入独立用户数据目录，不进入 Velopack current。
7. 关闭后端口释放；再次启动不会因“已有实例”卡死。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import struct
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8765"
HEALTH_TIMEOUT = 90.0
PORT_CLOSE_TIMEOUT = 20.0


class SmokeError(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)
    print(f"  [OK] {message}")


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def wait_health(timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError as error:
            last_error = error
        time.sleep(0.5)
    raise SmokeError(f"本地服务未在 {timeout:.0f} 秒内就绪：{last_error}")


def http_get(path: str) -> tuple[int, bytes, dict[str, str]]:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as response:
        return response.status, response.read(), dict(response.headers)


def post_command(command: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{BASE}/api/command",
        data=json.dumps({"command": command, "payload": payload or {}}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def launch(exe: Path) -> subprocess.Popen:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        [str(exe)],
        cwd=str(exe.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def close_debug_browser(config: dict, port_was_free_before_launch: bool) -> None:
    """通过 CDP 关闭小皮卡启动的调试浏览器，只影响独立 profile。

    启动前 9222 已被占用时不做关闭，避免误关用户自己的调试浏览器。
    """
    if not port_was_free_before_launch:
        print("  （调试端口在启动前已被占用，跳过调试浏览器关闭）")
        return
    import requests

    port = int(config.get("debug_port", 9222))
    try:
        info = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2).json()
        ws_url = info.get("webSocketDebuggerUrl")
        if not ws_url:
            return
        from websocket import create_connection

        ws = create_connection(ws_url, timeout=5)
        try:
            ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            try:
                ws.recv()
            except Exception:  # noqa: BLE001 - 浏览器可能直接断开
                pass
        finally:
            ws.close()
        time.sleep(2)
    except Exception:  # noqa: BLE001 - 调试浏览器不存在时静默跳过
        pass


def wait_port_closed(timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_open(8765):
            return
        time.sleep(0.5)
    raise SmokeError("关闭命令后本地服务端口仍未释放，后台服务可能残留")


def shutdown_service() -> None:
    try:
        post_command("shutdown_app")
    except OSError:
        pass
    wait_port_closed(PORT_CLOSE_TIMEOUT)


def expected_version() -> str:
    sys.path.insert(0, str(ROOT))
    from version import APP_VERSION

    return APP_VERSION


def cli_json(exe: Path, args: list[str], expected_code: int) -> dict:
    completed = subprocess.run([str(exe), *args], input=b"", capture_output=True, timeout=40,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    check(completed.returncode == expected_code, "CLI exit code matches the protocol")
    check(not completed.stderr, "CLI stderr is empty")
    value = json.loads(completed.stdout.decode("utf-8"))
    check(value["schemaVersion"] == 1, "CLI stdout is one UTF-8 JSON document")
    return value


def pe_subsystem(exe: Path) -> int:
    with exe.open("rb") as stream:
        stream.seek(0x3C)
        offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(offset + 24 + 68)
        return struct.unpack("<H", stream.read(2))[0]


def main(argv: list[str]) -> int:
    smoke_data = Path(os.environ.get("YXY_SMOKE_DATA_DIR", ROOT / ".smoke-data")).resolve()
    os.environ["YXY_DATA_DIR"] = str(smoke_data)
    if len(argv) > 1:
        release_dir = Path(argv[1]).resolve()
    else:
        release_dir = (ROOT / "dist" / "dgut-bot").resolve()
    print(f"发行目录：{release_dir}")

    exe = release_dir / "dgut-bot.exe"
    check(exe.is_file(), "dgut-bot.exe 存在")
    cli = release_dir / "dgutctl.exe"
    check(cli.is_file(), "dgutctl.exe 存在")
    check(pe_subsystem(exe) == 2 and pe_subsystem(cli) == 3, "主程序按需创建终端，CLI 为 console 程序")
    check((release_dir / "_internal").is_dir(), "_internal 资源目录存在")
    index = release_dir / "_internal" / "frontend" / "dist" / "index.html"
    check(index.is_file(), "frontend/dist/index.html 存在")

    check(not port_open(8765), "验证端口未被其他服务占用")
    try:
        absent = cli_json(cli, ["call", "system.health"], 3)
        check(absent["error"]["code"] == "SERVICE_NOT_RUNNING", "未启动服务时返回结构化错误")
        debug_port_was_free = not port_open(int(9222))
        print("首次启动…")
        launch(exe)
        wait_health(HEALTH_TIMEOUT)
        print("  [OK] 本地后端服务启动")
        capabilities = cli_json(cli, ["capabilities"], 0)
        names = [tool["name"] for tool in capabilities["result"]["tools"]]
        check(names == sorted(names) and "quiz.submit_answers" in names, "发行版 CLI 能力发现完整且稳定")
        version = cli_json(cli, ["call", "system.version"], 0)
        check(version["result"]["appVersion"] == expected_version(), "发行版 CLI 与 GUI 版本一致")

        status, body, _headers = http_get("/")
        check(status == 200 and b'id="root"' in body, "首页 HTML 可加载")

        assets = sorted((release_dir / "_internal" / "frontend" / "dist" / "assets").iterdir())
        js = next((a for a in assets if a.suffix == ".js"), None)
        css = next((a for a in assets if a.suffix == ".css"), None)
        check(js is not None and css is not None, "frontend/dist 存在 JS 与 CSS 资源")
        for asset in (js, css):
            status, _body, headers = http_get(f"/assets/{asset.name}")
            content_type = headers.get("Content-Type", "")
            ok_type = "javascript" in content_type or "css" in content_type
            check(status == 200 and ok_type, f"静态资源 /assets/{asset.name} 返回 {content_type}")

        info = post_command("get_app_info")
        check(info.get("ok") and info["info"]["version"] == expected_version(), "API 通信正常且版本一致")
        settings = post_command("get_settings")
        check(settings.get("ok"), "get_settings 可用")

        detected = post_command("detect_browsers").get("browsers", [])
        check(bool(detected), f"检测到 Chromium 浏览器：{[item['name'] for item in detected]}")

        check((smoke_data / "config.json").is_file(), "config.json 写入独立用户数据目录")
        check((smoke_data / "browser-launcher.log").is_file(), "启动日志写入独立用户数据目录")
        check(not (release_dir / "config.json").exists(), "Velopack current 目录没有用户配置")

        print("关闭服务…")
        close_debug_browser(settings.get("config", {}), debug_port_was_free)
        shutdown_service()
        check(not (smoke_data / "agent-runtime.json").exists(), "后台退出已清理实例文件")
        print("  [OK] 后台服务已退出，端口 8765 已释放")

        print("再次启动（验证不误报已有实例）…")
        launch(exe)
        wait_health(HEALTH_TIMEOUT)
        print("  [OK] 第二次启动正常")
        close_debug_browser(post_command("get_settings").get("config", {}), debug_port_was_free)
        shutdown_service()
    finally:
        if port_open(8765):
            try:
                post_command("shutdown_app")
            except OSError:
                pass
            try:
                wait_port_closed(PORT_CLOSE_TIMEOUT)
            except SmokeError:
                pass

    print("冒烟测试全部通过。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except SmokeError as error:
        print(f"冒烟测试失败：{error}", file=sys.stderr)
        raise SystemExit(1)
