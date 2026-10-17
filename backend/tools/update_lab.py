"""隔离的更新实验室：真实 SDK、回环 HTTP 故障和小程序更新移交。

python backend/tools/update_lab.py                 # 本地面板
python backend/tools/update_lab.py --run-all       # 离线网络/下载回归
python backend/tools/update_lab.py --handoff       # Windows 原生替换与重启
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import velopack
from dgutbot.app.updater import UpdateManager

CASES = {
    "success": "正常检查、下载和校验",
    "current": "当前已是最新版本",
    "older": "服务器版本较旧，不降级",
    "transient": "首次返回 503，自动重试成功",
    "timeout": "请求无响应，超时后恢复并重试",
    "missing": "更新清单 404，修复后重试",
    "invalid": "更新清单损坏，修复后重试",
    "truncated": "下载断流，恢复后重试",
    "checksum": "包被篡改，校验拦截后重试",
    "restart": "下载后重建管理器，恢复待安装状态",
}
PACKAGE_ID = "DgutBotUpdateLab"
OLD_VERSION, NEW_VERSION = "1.0.0", "1.0.1"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def manifest(version):
    return (f'<package><metadata><id>{PACKAGE_ID}</id><version>{version}</version>'
            '<title>DgutBot Update Lab</title><authors>Update Lab</authors>'
            '<description>Isolated updater fixture</description><mainExe>lab-app.exe</mainExe>'
            '<channel>win</channel><os>win</os><rid>win-x64</rid>'
            '<machineArchitecture>x64</machineArchitecture></metadata></package>')


def make_package(version=NEW_VERSION, executable=None, updater=b"Update Lab: network-only placeholder"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{PACKAGE_ID}.nuspec", manifest(version))
        archive.writestr("lib/app/sq.version", manifest(version))
        archive.writestr("lib/app/payload.bin", bytes(range(256)) * 2048)
        if executable is not None:
            archive.writestr("lib/app/lab-app.exe", executable)
        if updater is not None:
            archive.writestr("lib/app/Squirrel.exe", updater)
    return buffer.getvalue()


def locator(root):
    require(json.loads((root / "update-lab.json").read_text())["packageId"] == PACKAGE_ID,
            "不是更新实验室创建的安装目录")
    return velopack.VelopackLocatorConfig(
        root, root / "Update.exe", root / "packages", root / "current" / "sq.version",
        root / "current", True,
    )


def prepare(root, updater=b"", old_executable=None):
    root.mkdir(parents=True, exist_ok=False)
    (root / "current").mkdir()
    (root / "packages").mkdir()
    (root / ".portable").touch()
    write_json(root / "update-lab.json", {"packageId": PACKAGE_ID})
    (root / "Update.exe").write_bytes(updater)
    (root / "current" / "sq.version").write_text(manifest(OLD_VERSION), encoding="utf-8")
    if old_executable:
        (root / "current" / "lab-app.exe").write_bytes(old_executable)
    (root / "user-data").mkdir()
    write_json(root / "user-data" / "sentinel.json", {"preserve": "isolated user data"})


class FeedServer:
    def __init__(self, scenario, package=None):
        self.scenario = scenario
        self.version = OLD_VERSION if scenario == "current" else "0.9.0" if scenario == "older" else NEW_VERSION
        self.package = package if package is not None else make_package(self.version)
        self.recovered = False
        self.requests = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                with owner._lock:
                    owner.requests.append({"path": path, "time": round(time.monotonic(), 3)})
                    feed_count = sum(r["path"].endswith(".json") for r in owner.requests)
                    recovered = owner.recovered
                scenario = "success" if recovered else owner.scenario
                if path.endswith("releases.win.json"):
                    if scenario == "timeout":
                        owner._closed.wait(5)
                        return
                    if scenario == "missing":
                        self.reply(404, b"missing feed")
                        return
                    if scenario == "transient" and feed_count == 1:
                        self.reply(503, b"temporary failure")
                        return
                    if scenario == "invalid":
                        self.reply(200, b"{invalid-json")
                        return
                    # 与正式源一样经历 latest -> 固定标签的重定向。
                    if "/latest/" in path:
                        self.send_response(302)
                        self.send_header("Location", f"/releases/download/v{owner.version}/releases.win.json")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.reply(200, json.dumps({"Assets": [owner.asset()]}).encode())
                elif path == f"/releases/download/v{owner.version}/{owner.asset()['FileName']}":
                    data = owner.package
                    if scenario == "checksum":
                        data = data[:-1] + bytes([data[-1] ^ 0xFF])
                    if scenario == "truncated":
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data[:1024])
                        self.wfile.flush()
                        self.close_connection = True
                    else:
                        self.reply(200, data)
                else:
                    self.reply(404, b"unknown fixture path")

            def reply(self, status, data):
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def asset(self):
        return {"PackageId": PACKAGE_ID, "Version": self.version, "Type": "Full",
                "FileName": f"{PACKAGE_ID}-{self.version}-full.nupkg",
                "SHA1": hashlib.sha1(self.package).hexdigest(),
                "SHA256": hashlib.sha256(self.package).hexdigest(), "Size": len(self.package),
                "NotesMarkdown": "本地合成更新包", "NotesHtml": ""}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self._closed.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class LabManager(UpdateManager):
    """仅替换传输和安装位置；检查、下载、消息、恢复沿用产品实现。"""
    def __init__(self, root, origin, *, allow_install=False):
        parsed = urlsplit(origin)
        require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
                and not parsed.username and not parsed.password and not parsed.path
                and not parsed.query and not parsed.fragment, "测试更新源必须是 http://127.0.0.1:<端口>")
        self.install_root = root
        self.origin = origin
        self.allow_install = allow_install
        super().__init__(root / "user-data", version=OLD_VERSION, repository="local/update-lab")

    def native(self, path, timeout):
        source = velopack.HttpSource(
            self.origin + path, velopack.HttpOptions(Headers=[], TimeoutMilliseconds=timeout),
        )
        return velopack.UpdateManager(source, locator=locator(self.install_root))

    def _default_manager(self):
        return self.native("/releases/latest/download", 400)

    def _manager_for_download(self, update):
        return self.native(f"/releases/download/v{update.TargetFullRelease.Version}", 2000)

    def install(self):
        if not self.allow_install:
            return {"ok": False, "error": "网络测试包不执行安装；请运行 --handoff 验证原生替换和重启。"}
        return super().install()

    def _install_worker(self):
        require(self.allow_install, "网络场景不能执行安装")
        return super()._install_worker()

    def shutdown_for_update(self, stop_backend):
        # 此测试进程没有产品浏览器/课程任务，退出不能触碰它们。
        return {"ok": self.snapshot()["readyForExit"]}


def wait_state(manager, expected, history, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = manager.snapshot()
        state = snapshot["state"]
        if not history or history[-1] != state:
            history.append(state)
        if state in expected:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"等待 {expected} 超时，当前为 {manager.snapshot()}")


def run_case(case, base):
    require(case in CASES, "未知测试场景")
    root = base / f"{case}-{uuid.uuid4().hex[:8]}"
    prepare(root)
    sentinel = (root / "user-data" / "sentinel.json").read_bytes()
    history, stages = [], []
    result = {"case": case, "label": CASES[case], "root": str(root), "passed": False}
    started = time.monotonic()
    manager = None
    try:
        with FeedServer(case) as feed:
            manager = LabManager(root, feed.origin)
            check = manager.check(manual=True)
            if case in {"timeout", "missing", "invalid"}:
                require(not check["ok"], "故障清单必须报告检查失败")
                require(not manager.snapshot()["canInstall"], "检查失败不能产生待安装更新")
                stages.append({"stage": "expected-check-failure", "error": manager.snapshot()["error"]})
                require(not manager._check_thread.is_alive(), "底层请求超时后必须释放线程")
                feed.recovered = True
                check = manager.check(manual=True)
            require(check["ok"], f"检查失败：{check}")
            if case in {"current", "older"}:
                require(not check["updateAvailable"], "同版本或旧版本不能触发升级")
                require(not list((root / "packages").glob("*.nupkg")), "没有更新时不得下载")
            else:
                snapshot = wait_state(manager, {"ready_to_install", "download_failed"}, history)
                if case in {"truncated", "checksum"}:
                    require(snapshot["state"] == "download_failed", "损坏下载必须被拒绝")
                    require(not snapshot["canInstall"], "校验失败不能安装")
                    stages.append({"stage": "expected-download-failure", "error": snapshot["error"]})
                    feed.recovered = True
                    require(manager.start_download()["ok"], "下载重试未被接受")
                    # start_download 异步启动，先等 worker 完成，避免读到上一次失败。
                    manager._worker.join(6)
                    snapshot = wait_state(manager, {"ready_to_install"}, history)
                require(snapshot["canInstall"], f"未准备好安装：{snapshot}")
                package = root / "packages" / feed.asset()["FileName"]
                require(hashlib.sha256(package.read_bytes()).hexdigest() == feed.asset()["SHA256"], "包哈希不符")
                recovered = LabManager(root, feed.origin)
                recovered.restore()
                require(recovered.snapshot()["canInstall"], "新管理器未恢复已下载更新")
                recovered.stop()
                stages.append({"stage": "download-verified-and-restored", "version": NEW_VERSION})
            require((root / "user-data" / "sentinel.json").read_bytes() == sentinel, "用户数据被改变")
            result.update(passed=True, requests=list(feed.requests), final=manager.snapshot())
    except Exception as error:
        result["error"] = str(error)
    finally:
        if manager:
            manager.stop()
            for worker in (manager._check_thread, manager._worker):
                if worker:
                    worker.join(5)
        result.update(seconds=round(time.monotonic() - started, 2), states=history, stages=stages)
        write_json(root / "report.json", result)
    return result


def compile_fixture(root, version):
    compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    require(compiler.is_file(), "找不到 Windows .NET Framework C# 编译器")
    source = root / f"app-{version}.cs"
    source.write_text(
        'using System; using System.IO; class Lab { static void Main() {'
        f'File.WriteAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "started-version.txt"), "{version}");'
        '} }', encoding="utf-8",
    )
    exe = root / f"app-{version}.exe"
    subprocess.run([str(compiler), "/nologo", "/target:winexe", f"/out:{exe}", str(source)],
                   check=True, capture_output=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return exe.read_bytes()


def run_handoff(base, package_path=None):
    """用现有 nupkg 中的真实 Update.exe，只更新新建的便携夹具目录。"""
    require(sys.platform == "win32", "原生移交测试仅支持 Windows")
    candidates = sorted((ROOT / "Releases").glob("*-full.nupkg"), key=lambda p: p.stat().st_mtime)
    source = Path(package_path) if package_path else candidates[-1] if candidates else None
    require(source is not None and source.is_file(), "需要现有完整 nupkg：--package <文件>，或先在本地构建一次")
    root = base / f"handoff-{uuid.uuid4().hex[:8]}"
    with zipfile.ZipFile(source) as archive:
        updater = archive.read("lib/app/Squirrel.exe")
    prepare(root, updater)
    old, new = compile_fixture(root, OLD_VERSION), compile_fixture(root, NEW_VERSION)
    executable = root / "current" / "lab-app.exe"
    executable.write_bytes(old)
    sentinel = (root / "user-data" / "sentinel.json").read_bytes()
    package = make_package(executable=new, updater=updater)
    result = {"case": "handoff", "root": str(root), "passed": False, "updaterFrom": str(source)}
    started = time.monotonic()
    try:
        subprocess.run([str(executable)], check=True, timeout=10)
        require((root / "current" / "started-version.txt").read_text() == OLD_VERSION, "旧程序未运行")
        with FeedServer("success", package) as feed:
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--handoff-worker", str(root), "--origin", feed.origin],
                capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            require(completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace"))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                marker = root / "current" / "started-version.txt"
                if marker.exists() and marker.read_text() == NEW_VERSION:
                    break
                time.sleep(0.1)
            require(marker.exists() and marker.read_text() == NEW_VERSION, "Update.exe 未重启新版程序")
            require(executable.read_bytes() == new, "实际程序文件未被替换")
            require(NEW_VERSION in (root / "current" / "sq.version").read_text(), "安装清单版本未更新")
            require((root / "user-data" / "sentinel.json").read_bytes() == sentinel, "更新改变了用户数据")
            result.update(passed=True, installedVersion=NEW_VERSION, requests=list(feed.requests))
    except Exception as error:
        result["error"] = str(error)
    result["seconds"] = round(time.monotonic() - started, 2)
    write_json(root / "report.json", result)
    return result


def handoff_worker(root, origin):
    require((root / ".portable").is_file(), "原生移交只允许隔离的便携测试目录")
    manager = LabManager(root, origin, allow_install=True)
    history = []
    require(manager.check()["ok"], "夹具检查失败")
    wait_state(manager, {"ready_to_install"}, history)
    require(manager.install()["ok"], "夹具移交失败")
    wait_state(manager, {"waiting_for_exit"}, history)
    write_json(root / "handoff.json", {"pid": os.getpid(), "states": history, "readyForExit": True})
    manager.stop()
    return 0  # 真正退出此进程，Update.exe 随后替换并重启夹具。


class Dashboard:
    def __init__(self, base):
        self.base = base
        self.lock = threading.Lock()
        self.running = False
        self.results = []
        self.token = uuid.uuid4().hex

    def snapshot(self):
        with self.lock:
            return {"running": self.running, "cases": CASES, "results": list(self.results), "root": str(self.base)}

    def start(self, case):
        require(case == "all" or case in CASES or case == "handoff", "未知场景")
        with self.lock:
            if self.running:
                return False
            self.running = True

        def work():
            try:
                for selected in CASES if case == "all" else [case]:
                    try:
                        result = run_handoff(self.base) if selected == "handoff" else run_case(selected, self.base)
                    except Exception as error:
                        result = {"case": selected, "passed": False, "error": str(error)}
                    with self.lock:
                        self.results.insert(0, result)
                        write_json(self.base / "results.json", self.results)
            finally:
                with self.lock:
                    self.running = False

        threading.Thread(target=work, daemon=True).start()
        return True


def serve(base, port):
    dashboard = Dashboard(base)
    page = Path(__file__).with_suffix(".html").read_text(encoding="utf-8").replace("__TOKEN__", dashboard.token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data, mime="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                self.reply(200, page.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                self.reply(200, json.dumps(dashboard.snapshot(), ensure_ascii=False).encode())
            else:
                self.reply(404, b'{}')

        def do_POST(self):
            if self.headers.get("X-Lab-Token") != dashboard.token:
                self.reply(403, b'{}')
                return
            try:
                require(self.path == "/api/run", "未知接口")
                length = int(self.headers.get("Content-Length", "0"))
                require(0 < length <= 1024, "请求体大小无效")
                payload = json.loads(self.rfile.read(length))
                accepted = dashboard.start(payload["case"])
                self.reply(202 if accepted else 409, json.dumps({"ok": accepted}).encode())
            except (ValueError, KeyError, AssertionError) as error:
                self.reply(400, json.dumps({"error": str(error)}, ensure_ascii=False).encode())

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"更新实验室：http://127.0.0.1:{server.server_port}/", flush=True)
    print(f"测试数据和报告：{base}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run-all", action="store_true")
    mode.add_argument("--case", choices=CASES)
    mode.add_argument("--handoff", action="store_true")
    mode.add_argument("--handoff-worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--origin", help=argparse.SUPPRESS)
    parser.add_argument("--package", type=Path, help="原生移交使用的现有完整 nupkg")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--output", type=Path, default=ROOT / ".update-lab")
    args = parser.parse_args(argv)
    if args.handoff_worker:
        return handoff_worker(args.handoff_worker, args.origin)
    base = args.output.resolve()
    base.mkdir(parents=True, exist_ok=True)
    if args.run_all or args.case or args.handoff:
        results = [run_handoff(base, args.package)] if args.handoff else [
            run_case(case, base) for case in CASES if args.run_all or case == args.case
        ]
        write_json(base / "results.json", results)
        for result in results:
            print(f"{'PASS' if result['passed'] else 'FAIL'} {result['case']} ({result.get('seconds', 0)}s) {result.get('error', '')}")
        print(f"报告：{base / 'results.json'}")
        return int(any(not r["passed"] for r in results))
    serve(base, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
