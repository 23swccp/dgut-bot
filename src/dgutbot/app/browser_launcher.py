"""管理浏览器版所需的本地 API、Vite 和调试浏览器。

冻结（PyInstaller onedir）模式下本文件同时是 dgut-bot.exe 的入口：
无参数时作为启动器查找浏览器并拉起后台服务；带 --service 参数时
作为后台服务进程运行，全程不需要系统安装 Python。
"""

from __future__ import annotations

# Velopack 官方要求：必须在主入口、其它应用初始化之前执行一次。
# 更新钩子可能在这里直接结束进程，不能把它放到后面的 main() 中。
if __name__ == "__main__":
    import velopack as _velopack

    _velopack.App().set_auto_apply_on_startup(False).run()

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from dgutbot.app.app_paths import data_root, frontend_dist, is_frozen, resource_root
from dgutbot.app.browser_dialog import choose_browser_file
from dgutbot.app.browser_paths import BROWSER_NAMES, resolve_browser_path
from dgutbot.app.browser_lifetime import BrowserLifetime, FRONTEND_DISPLAY_PATH
from dgutbot.app.campus_startup import configure_campus_login_startup, open_campus_login
from dgutbot.agent.agent_runtime import new_runtime, publish_runtime, remove_runtime
from dgutbot.app.backend_commands import AGENT_SERVICE, backend, configure_agent_registry
from dgutbot.app.web_server import (
    CLIENT_CLOSED_EVENT,
    LocalApiHandler,
    SHUTDOWN_EVENT,
    client_last_seen,
    configure_agent_api,
    reset_client_state,
    stop_backend_tasks,
    update_manager,
)
from dgutbot.app.yxy_mutex import APP_MUTEX, NamedMutex, app_mutex_exists

ROOT = data_root()
FRONTEND = resource_root() / "web"
LOG_PATH = ROOT / "browser-launcher.log"
SERVICE_LOG_PATH = ROOT / "browser-service.log"
APP_TITLE = "莞工小皮卡"
DUPLICATE_NOTICE_SECONDS = 4.0
# 助手页在用户切到优学院课件页后会成为后台标签；Chromium 会明显降低
# 后台定时器频率。真正关闭标签页有 pagehide/sendBeacon 这条快速路径，
# 心跳仅作 Beacon 丢失时的兜底，因此保留较长的后台容忍时间。
CLIENT_HEARTBEAT_TIMEOUT = 120.0
CLIENT_CLOSE_GRACE = 1.0


def client_connection_expired(client_connected: bool, last_seen: float, now: float | None = None) -> bool:
    """关闭通知丢失时，根据心跳延迟回收本地服务。"""
    current = time.monotonic() if now is None else now
    return bool(client_connected and current - last_seen >= CLIENT_HEARTBEAT_TIMEOUT)


def configure_console_encoding() -> str:
    """终端优先 UTF-8；旧控制台不支持时仅将终端降级为 GBK。"""
    encoding = os.environ.get("YXY_CONSOLE_ENCODING", "utf-8").lower()
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            utf8_ready = bool(kernel32.SetConsoleOutputCP(65001)) and bool(kernel32.SetConsoleCP(65001))
            if not utf8_ready or encoding == "gbk":
                kernel32.SetConsoleOutputCP(936)
                kernel32.SetConsoleCP(936)
                encoding = "gbk"
        except (AttributeError, OSError):
            encoding = "gbk"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding=encoding, errors="replace")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding=encoding, errors="replace")
    return encoding


def print_startup_help() -> None:
    print("启动说明：程序会优先使用有效的已保存路径；路径失效时按 Edge → Chrome → 其他 Chromium 浏览器重新查找。")
    print("若浏览器被移动到自定义位置，自动查找失败后请按提示填写 msedge.exe 或 chrome.exe 的完整地址，支持英文双引号。")
    print("-" * 72)
    print("-" * 72)


def choose_available_port(start: int, attempts: int = 20) -> int:
    """从指定端口开始选择空闲端口，避免与其它本机程序冲突。"""
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free local port found in {start}-{start + attempts - 1}.")


def choose_frontend_port(start: int = 1420, attempts: int = 20) -> int:
    """兼容旧调用；开发前端默认在 1420-1439 中选择。"""
    return choose_available_port(start, attempts)


def wait_for_frontend(process: subprocess.Popen, health_url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Frontend process exited with code {process.returncode}.")
        try:
            with urlopen(health_url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("Frontend startup timed out after 30 seconds.")


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def show_log_tail() -> None:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if lines:
        print("\n前端日志末尾：")
        print("\n".join(lines[-20:]))


def open_frontend(url: str) -> str:
    """查找并保存浏览器；传入网址时才真正打开浏览器。"""
    print("正在寻找 Chromium 浏览器：")
    path, name = backend.find_browser(progress=lambda candidate: print(f"  {candidate}"), timeout_seconds=10)
    if not path or not name:
        log_line(LOG_PATH, "未找到 Chromium 浏览器，等待终端配置。")
        print(f"检测详情已写入：{ROOT / 'browser-detection.log'}")
        return "manual"
    print(f"找到了 {name}：{path}")
    backend.update_settings(browser_name=name, browser_path=path)
    log_line(LOG_PATH, f"浏览器配置已确认：{name}，{path}")
    print(f"已保存浏览器配置地址：{path}")
    if not url:
        return "debug"
    if backend.start_browser(url):
        return "debug"
    print(f"浏览器启动失败：{path}")
    return "manual"


def configure_browser_path(value: str) -> bool:
    """验证文件选择窗口或终端传入的浏览器地址并保存。"""
    resolved = resolve_browser_path(value)
    if not resolved:
        print(f"格式或地址错误：{value}")
        print("请填写 Chromium 浏览器 .exe 的完整地址，可使用英文双引号包裹。")
        return False
    candidate = Path(resolved)
    executable = candidate.name.lower()
    browser_name = BROWSER_NAMES.get(executable, "自定义浏览器")
    browser_path = str(candidate.resolve())
    backend.update_settings(browser_name=browser_name, browser_path=browser_path)
    print(f"已保存 {browser_name} 配置：{browser_path}")
    return True


def prompt_for_browser(web_url: str) -> bool:
    """先弹出原生文件选择窗口；取消或失败后可重新选择、输入路径或退出。"""
    print("需要选择可用的浏览器。推荐 Microsoft Edge 或 Google Chrome，以便播放课程视频。")
    use_picker = True
    while True:
        if use_picker:
            try:
                selected = choose_browser_file()
            except OSError as error:
                selected = ""
                print(f"无法打开文件选择窗口：{error}；可以在下方手动填写路径。")
                log_line(LOG_PATH, f"文件选择窗口失败：{error}")
            if selected and configure_browser_path(selected):
                return True
            use_picker = False
        print("按回车打开文件选择窗口，或粘贴浏览器 .exe 地址/安装文件夹；输入 q 退出。")
        try:
            value = input("选择浏览器> ").strip()
        except (EOFError, OSError):
            print("无法读取终端输入，程序已取消启动。")
            return False
        if value.lower() == "q":
            print("已取消启动。")
            return False
        if not value:
            use_picker = True
            continue
        if configure_browser_path(value):
            return True


def request_service_shutdown(web_url: str) -> None:
    request = Request(
        f"{web_url}/api/command",
        data=b'{"command":"shutdown_app","payload":{}}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2):
            pass
    except OSError:
        pass


def log_line(path: Path, message: str) -> None:
    """无控制台的冻结进程把关键启动信息追加到日志文件。"""
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except OSError:
        pass


def show_notice_window(message: str, title: str = APP_TITLE, auto_close_ms: int | None = None) -> None:
    """无控制台的冻结进程用小窗口提示；tkinter 不可用时静默返回。"""
    try:
        import tkinter as tk

        root = tk.Tk()
        root.title(title)
        root.attributes("-topmost", True)
        root.resizable(False, False)
        tk.Label(root, text=message, justify="left", padx=24, pady=16, wraplength=380).pack()
        if auto_close_ms:
            root.after(auto_close_ms, root.destroy)
        root.mainloop()
        return
    except Exception:  # noqa: BLE001 - 无桌面环境时放弃提示
        pass


def show_already_running_notice() -> None:
    """在启动终端提示片刻，避免重复启动时窗口一闪而过。"""
    print("优学院助手已在运行，请勿重复打开。")
    print(f"此窗口将在 {int(DUPLICATE_NOTICE_SECONDS)} 秒后自动关闭……")
    time.sleep(DUPLICATE_NOTICE_SECONDS)


def static_frontend_available() -> bool:
    """发布包模式：有构建产物且没有 node_modules 时，由本地 API 服务直接托管前端。"""
    if is_frozen():
        return True
    if not (frontend_dist() / "index.html").is_file():
        return False
    return not (FRONTEND / "node_modules" / ".bin" / "vite.cmd").is_file()


def run_background_service(web_port: int, use_static: bool = False, api_port: int = 8765) -> int:
    """独立持有 API 与前端；启动终端被关闭后此进程仍继续运行。"""
    SHUTDOWN_EVENT.clear()
    reset_client_state()
    app_mutex = NamedMutex(APP_MUTEX)
    if not app_mutex.try_acquire():
        print("另一个优学院助手实例正在运行，本服务退出。")
        return 1
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm and not use_static:
        app_mutex.release()
        return 1
    server: ThreadingHTTPServer | None = None
    server_started = False
    runtime = None
    vite: subprocess.Popen | None = None
    log_file = None
    try:
        server_port = web_port if use_static else api_port
        server = ThreadingHTTPServer(("127.0.0.1", server_port), LocalApiHandler)
        runtime = new_runtime(server_port)
        configure_agent_registry(runtime.instance_id)
        configure_agent_api(runtime.auth_token, runtime.instance_id)
        publish_runtime(runtime)
        threading.Thread(target=server.serve_forever, name="local-api", daemon=True).start()
        server_started = True
        if use_static:
            print(f"发布包模式：由本地服务直接托管 web/dist，前端地址 http://127.0.0.1:{web_port}", flush=True)
        else:
            log_file = LOG_PATH.open("w", encoding="utf-8")
            vite_env = os.environ.copy()
            vite_env["YXY_API_PORT"] = str(api_port)
            vite = subprocess.Popen(
                [npm, "run", "dev", "--", "--port", str(web_port), "--strictPort"],
                cwd=FRONTEND,
                env=vite_env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        update_manager.set_ports(f"http://127.0.0.1:{web_port}", web_port)
        update_manager.set_exit_callback(SHUTDOWN_EVENT.set, stop_backend_tasks)
        update_manager.start_auto_check()
        client_connected = False
        browser_lifetime = BrowserLifetime(int(backend.config.debug_port), web_port)
        close_requested_at: float | None = None
        while not SHUTDOWN_EVENT.is_set():
            if vite is not None and vite.poll() is not None:
                return 1
            last_seen = client_last_seen()
            AGENT_SERVICE.leases.set("update_handoff", "updater", bool(update_manager.snapshot().get("readyForExit")))
            client_connected = client_connected or bool(last_seen)
            if browser_lifetime.closed():
                return 0
            if CLIENT_CLOSED_EVENT.is_set():
                close_requested_at = close_requested_at or time.monotonic()
                # A still-present assistant tab means refresh (or another open UI).
                # Explicit closure overrides course/monitor/agent keepalive leases.
                if time.monotonic() - close_requested_at >= CLIENT_CLOSE_GRACE and browser_lifetime.present is not True:
                    return 0
            else:
                close_requested_at = None
            if (client_connection_expired(client_connected, last_seen)
                    and browser_lifetime.present is not True and not AGENT_SERVICE.active()):
                return 0
            SHUTDOWN_EVENT.wait(0.2)
        return 0
    finally:
        stop_backend_tasks()
        update_manager.stop()
        if server is not None:
            if server_started:
                server.shutdown()
            server.server_close()
        stop_process(vite)
        if log_file is not None:
            log_file.close()
        if runtime is not None:
            remove_runtime(runtime.instance_id)
        app_mutex.release()


def service_command(web_port: int, use_static: bool, api_port: int = 8765) -> list[str]:
    """后台服务命令行：冻结模式通过当前 EXE 自启动，开发模式使用当前解释器。"""
    if is_frozen():
        command = [sys.executable, "--service", str(web_port), "--api-port", str(api_port)]
    else:
        service_python = Path(sys.base_prefix) / ("pythonw.exe" if sys.platform == "win32" else "bin/python")
        if not service_python.is_file():
            service_python = Path(sys.executable)
        command = ([str(sys.executable)] if is_frozen() else [str(service_python), "-m", "dgutbot.app.browser_launcher"])
        command.extend(["--service", str(web_port), "--api-port", str(api_port)])
    if use_static:
        command.append("--static")
    return command


def start_background_service(web_port: int, use_static: bool = False, api_port: int = 8765) -> subprocess.Popen:
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if is_frozen():
        # 冻结的服务进程把输出写进自己的日志文件，不需要继承句柄。
        return subprocess.Popen(
            service_command(web_port, use_static, api_port),
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    service_log = SERVICE_LOG_PATH.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(
            service_command(web_port, use_static, api_port),
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=service_log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
        )
    finally:
        service_log.close()


def redirect_frozen_output() -> None:
    """无控制台的冻结进程把 stdout/stderr 落到日志文件，避免诊断信息丢失。"""
    if sys.platform != "win32":
        return
    target = SERVICE_LOG_PATH if "--service" in sys.argv else LOG_PATH
    try:
        handle = target.open("w", encoding="utf-8", buffering=1)
        sys.stdout = handle
        sys.stderr = handle
    except OSError:
        pass


def prepare_launcher_console() -> None:
    """安装版只为交互启动器创建终端；后台服务与更新钩子保持无窗口。"""
    if is_frozen() and sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if not kernel32.GetConsoleWindow() and not kernel32.AllocConsole():
            raise OSError("无法创建启动终端，请从命令行重新启动程序。")
        kernel32.SetConsoleTitleW(f"{APP_TITLE} · 启动设置")
        sys.stdin = open("CONIN$", "r", encoding="utf-8")
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    configure_console_encoding()


def main() -> int:
    if "--campus-login" in sys.argv:
        open_campus_login()
        return 0
    if "--service" in sys.argv:
        index = sys.argv.index("--service")
        web_port = int(sys.argv[index + 1])
        api_port = int(sys.argv[sys.argv.index("--api-port") + 1]) if "--api-port" in sys.argv else 8765
        return run_background_service(web_port, "--static" in sys.argv, api_port)
    try:
        prepare_launcher_console()
    except OSError as error:
        log_line(LOG_PATH, str(error))
        show_notice_window(str(error))
        return 1
    log_line(LOG_PATH, "启动终端已就绪，开始检查浏览器配置。")
    if backend.config.campus_login_on_startup:
        try:
            # 应用更新后刷新启动命令中的程序路径。
            configure_campus_login_startup(True)
        except OSError as error:
            log_line(LOG_PATH, f"刷新校园网开机启动项失败：{error}")
    if app_mutex_exists():
        show_already_running_notice()
        return 0
    check_only = "--check" in sys.argv
    print_startup_help()
    SHUTDOWN_EVENT.clear()
    reset_client_state()
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    use_static = static_frontend_available()
    if not use_static and not npm:
        print("ERROR: Node.js/npm was not found in PATH.")
        return 1

    if not use_static and not (FRONTEND / "node_modules" / ".bin" / "vite.cmd").is_file():
        print("Installing frontend dependencies...")
        install = subprocess.run([npm, "ci"], cwd=FRONTEND, check=False)
        if install.returncode != 0:
            print("ERROR: npm ci failed.")
            return install.returncode

    service: subprocess.Popen | None = None
    try:
        if use_static:
            web_port = choose_available_port(8765)
            api_port = web_port
        else:
            web_port = choose_frontend_port()
            api_port = choose_available_port(8765)
        web_url = f"http://127.0.0.1:{web_port}"
        browser_url = f"{web_url}{FRONTEND_DISPLAY_PATH}"
        health_url = f"{web_url}/api/health"

        # 浏览器是网页界面的入口，必须先确认可用，再启动本地网页服务。
        browser_mode = open_frontend("")
        if browser_mode == "manual":
            if check_only:
                print("启动检查失败：未找到浏览器。正常启动程序后可在文件选择窗口中配置。")
                return 1
            if not prompt_for_browser(""):
                print("未配置可用浏览器，程序已取消启动。")
                return 0

        print("浏览器准备完成，开始启动网页程序。")
        service = start_background_service(web_port, use_static, api_port)
        print("正在启动本地网页程序…")
        wait_for_frontend(service, health_url)
        print(f"本地网页程序已就绪：{web_url}")

        if check_only:
            print("启动检查通过。")
            request_service_shutdown(web_url)
            service.wait(timeout=10)
            return 0

        if backend.start_browser(browser_url):
            print("已在找到的浏览器中打开网页程序。")
        else:
            print("已保存的浏览器启动失败，请重新填写浏览器地址。")
            if not prompt_for_browser("") or not backend.start_browser(browser_url):
                print("浏览器仍无法启动，程序已取消。")
                request_service_shutdown(web_url)
                return 0
        print("网页已打开，后台服务将继续运行；此启动窗口现在会自动关闭。")
        return 0
    except KeyboardInterrupt:
        print("\n启动已取消；若网页已经打开，后台服务会继续运行。")
        return 0
    except OSError as error:
        print(f"ERROR: Could not start a local service: {error}")
        show_log_tail()
        return 1
    except RuntimeError as error:
        print(f"ERROR: {error}")
        show_log_tail()
        return 1
    finally:
        # 后台服务是独立进程；退出或关闭此终端时不得清理它。
        pass


if __name__ == "__main__":
    if is_frozen() and "--service" in sys.argv:
        redirect_frozen_output()
    raise SystemExit(main())
