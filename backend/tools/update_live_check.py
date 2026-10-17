"""真实发行 EXE 的联网更新检查，复制安装文件并使用独立数据目录。

python backend/tools/update_live_check.py <安装目录/current/dgut-bot.exe>
默认清除子进程的代理环境变量，保留 Windows 系统代理；--inherit-proxy-env 对照继承环境。
不会修改原安装目录，不调用下载、安装或学校接口；旧程序自带的自动下载仅写隔离副本。
需要 Windows 及真实 GitHub 网络，不能作为离线 CI 测试。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def child_environment(data: Path, *, inherit_proxy: bool) -> dict[str, str]:
    env = os.environ.copy()
    if not inherit_proxy:
        for name in list(env):
            if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
                del env[name]
    env["YXY_DATA_DIR"] = str(data)
    return env


def safe_error(error: object) -> str:
    def redact(match):
        try:
            url = urlsplit(match.group())
            host = url.hostname or ""
            if url.port:
                host += f":{url.port}"
            return urlunsplit((url.scheme, host, url.path, "", ""))
        except ValueError:
            return "[URL]"
    return re.sub(r"https?://[^\s]+", redact, str(error))


def clone_install(exe: Path, root: Path) -> Path:
    manifest = exe.parent / "sq.version"
    updater = exe.parent.parent / "Update.exe"
    if not exe.is_file() or exe.name.lower() != "dgut-bot.exe" or not manifest.is_file() or not updater.is_file():
        raise ValueError("请指定真实安装目录 current/dgut-bot.exe（需同时存在 sq.version 和上一级 Update.exe）")
    tree = ET.parse(manifest)
    package_id = next((item.text for item in tree.iter() if item.tag.rsplit("}", 1)[-1] == "id"), None)
    if package_id != "DgutBot":
        raise ValueError("安装清单不是 DgutBot")
    current = root / "app" / "current"
    shutil.copytree(exe.parent, current)
    shutil.copy2(updater, current.parent / "Update.exe")
    (current.parent / "packages").mkdir()
    (current.parent / ".portable").touch()
    return current / exe.name


def run(exe: Path, output: Path, *, inherit_proxy: bool = False) -> tuple[dict, Path]:
    root = output.resolve() / ("live-check-" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    cloned = clone_install(exe.resolve(), root)
    port = None
    for candidate in range(8765, 8785):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            port = candidate
            break
    if port is None:
        raise RuntimeError("产品 API 端口均被占用")
    base = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))

    def command(name):
        request = Request(base + "/api/command",
                          data=json.dumps({"command": name, "payload": {}}).encode(),
                          headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=3) as response:
            return json.load(response)

    report = {"sourceExecutable": str(exe.resolve()), "executable": str(cloned),
              "proxyEnvironment": "inherited" if inherit_proxy else "system-only", "stages": []}
    process = subprocess.Popen([str(cloned), "--service", str(port), "--static"], cwd=cloned.parent,
                               env=child_environment(root / "data", inherit_proxy=inherit_proxy),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    owns_service = False
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"发行程序提前退出：{process.returncode}（请先关闭其它小皮卡实例）")
            try:
                initial = command("get_update_status")["update"]
                owns_service = True
                break
            except OSError:
                time.sleep(.2)
        else:
            raise RuntimeError("发行程序服务未就绪")
        start = time.monotonic()
        command("check_update")
        completed = False
        while time.monotonic() - start < 48:
            snapshot = command("get_update_status")["update"]
            stages = report["stages"]
            if not stages or stages[-1]["state"] != snapshot["state"]:
                stages.append({"state": snapshot["state"], "seconds": round(time.monotonic() - start, 2)})
            changed = (snapshot.get("lastMessageId") != initial.get("lastMessageId")
                       or snapshot.get("error") != initial.get("error")
                       or snapshot.get("latestVersion") != initial.get("latestVersion")
                       or any(item["state"] == "checking" for item in stages))
            if snapshot["state"] != "checking" and (changed or snapshot["state"] == "ready_to_install"):
                completed = True
                break
            time.sleep(.25)
        report.update(currentVersion=snapshot["currentVersion"], state=snapshot["state"],
                      latestVersion=snapshot["latestVersion"], error=safe_error(snapshot.get("error", "")),
                      seconds=round(time.monotonic() - start, 2))
        report["passed"] = completed and not snapshot.get("error")
        if not completed:
            report["probeError"] = "未在等待期限内观察到完成的更新检查"
    except Exception as error:
        report.update(passed=False, probeError=safe_error(error))
    finally:
        if owns_service and process.poll() is None:
            try:
                command("shutdown_app")
            except OSError:
                pass
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
        report["exitCode"] = process.returncode
        path = root / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report, path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("--inherit-proxy-env", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".update-lab")
    args = parser.parse_args()
    report, path = run(args.exe, args.output, inherit_proxy=args.inherit_proxy_env)
    print(json.dumps(report, ensure_ascii=True))
    print(f"Report: {path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
