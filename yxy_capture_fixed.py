import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

try:
    import websocket
except ImportError:
    print("请先安装 websocket-client: pip install websocket-client")
    sys.exit(1)

LOG_DIR = os.path.expandvars(r"%USERPROFILE%\Desktop\dgut.yxy-checkin_assistant")
LOG_FILE = os.path.join(LOG_DIR, f"yxy_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")

DGUT_DOMAINS = [
    "dgut.edu.cn", "dgut",
    "ulearning.cn", "ulearning",
    "goeasy", "goeasy.io", "goeasy.com.cn",
    "websocket",
]
CHROME_DEBUG_URL = "http://127.0.0.1:9222/json"
CHROME_VERSION_URL = "http://127.0.0.1:9222/json/version"
# Chrome 136+ / Edge 的默认用户目录不再允许开启远程调试。
# 使用独立目录既能正常启用 CDP，也不会干扰日常浏览器窗口。
CDP_PROFILE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "YxyCaptureCDPProfile",
)
TARGET_URL = "https://lms.dgut.edu.cn"

# 调试开关：如果还是抓不到，改成 True 看所有请求
SHOW_ALL_REQUESTS = False

# 开始抓包后，每 120 秒（2 分钟）刷新一次东莞理工/优学院页面。
# 长时间挂机时可修改此值；单位为秒，设为 0 可关闭自动刷新。
AUTO_REFRESH_INTERVAL = 120
AUTO_REFRESH_URL_KEYWORDS = ("dgut.edu.cn", "ulearning.cn")

# 抓取 API/JSON 的响应正文。静态 JS/CSS/图片不保存，避免日志过度膨胀。
CAPTURE_RESPONSE_BODIES = True
RESPONSE_BODY_URL_MARKERS = ("/courseapi/", "/classroomapi/", "/appapi/", "/api/")

# 默认不记录无助于分析签到状态的静态资源；终端输入 a 后仍可查看全量请求。
# 某些 Edge/CDP 事件不会提供可靠的 resource type，因此还要按 URL 后缀兜底。
IGNORED_RESOURCE_TYPES = {"image", "script", "stylesheet", "font", "media", "manifest", "prefetch"}
IGNORED_URL_EXTENSIONS = {
    ".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".wav", ".ogg", ".mp4", ".webm", ".m3u8",
}

EDGE_PATHS = [
    os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]
CHROME_PATHS = [
    os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

running = True
capture_enabled = False     # 仅在终端输入 s 后才附加标签页并记录网络请求
log_fp = None              # 全局日志文件句柄
browser_ws = None
sessions = {}              # session_id -> {target_id, target_type, url}
session_request_maps = {}  # session_id -> {request_id -> request_info}
cmd_id = 0
cmd_lock = threading.Lock()
cmd_callbacks = {}         # cmd_id -> callback function


def log(msg="", fp=None):
    global log_fp
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    out = fp if fp else log_fp
    if out:
        out.write(line + "\n")
        out.flush()


def should_log(url):
    if SHOW_ALL_REQUESTS:
        return True
    url_lower = str(url).lower()
    return any(d in url_lower for d in DGUT_DOMAINS)


def should_capture_response_body(url, mime_type):
    if not CAPTURE_RESPONSE_BODIES or not should_log(url):
        return False
    url_lower = url.lower()
    mime_lower = (mime_type or "").lower()
    return "json" in mime_lower or any(marker in url_lower for marker in RESPONSE_BODY_URL_MARKERS)


def should_log_http_resource(url, resource_type):
    if not should_log(url):
        return False
    if SHOW_ALL_REQUESTS:
        return True
    if str(resource_type).lower() in IGNORED_RESOURCE_TYPES:
        return False
    try:
        path = urlsplit(str(url)).path.lower()
    except ValueError:
        path = str(url).lower().split("?", 1)[0]
    return not any(path.endswith(extension) for extension in IGNORED_URL_EXTENSIONS)


def log_headers(title, headers):
    """逐项原样记录 CDP 提供的请求/响应头。"""
    log(title)
    if not headers:
        log("      (无)")
        return
    for key, value in headers.items():
        log(f"      {key}: {value}")


def log_body(title, body, content_type=""):
    """原样记录文本正文，使 JSON 和表单参数保持可搜索。"""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body or "")
    log(f"{title} ({len(text)} chars)")
    if text:
        log(text)
    else:
        log("      (空)")


def next_cmd_id():
    global cmd_id
    with cmd_lock:
        cmd_id += 1
        return cmd_id


def send_to_browser(method, params=None, callback=None):
    if not browser_ws:
        return
    msg = {"id": next_cmd_id(), "method": method}
    if params:
        msg["params"] = params
    if callback:
        cmd_callbacks[msg["id"]] = callback
    try:
        browser_ws.send(json.dumps(msg))
    except Exception as e:
        log(f"send_to_browser 失败 ({method}): {e}")


def send_to_session(session_id, method, params=None, callback=None):
    if not browser_ws or not session_id:
        return
    msg = {"id": next_cmd_id(), "method": method}
    if params:
        msg["params"] = params
    if callback:
        cmd_callbacks[msg["id"]] = callback
    msg["sessionId"] = session_id
    try:
        browser_ws.send(json.dumps(msg))
    except Exception as e:
        log(f"send_to_session 失败 ({method}): {e}")


def find_browser():
    for p in EDGE_PATHS + CHROME_PATHS:
        if os.path.isfile(p):
            return p, "Edge" if "edge" in p.lower() else "Chrome"
    return None, None


def is_debug_port_open():
    try:
        resp = urlopen(Request(CHROME_VERSION_URL), timeout=2)
        data = json.loads(resp.read())
        return bool(data.get("webSocketDebuggerUrl"))
    except Exception:
        return False


def launch_browser(browser_path, browser_name):
    # 不关闭用户已打开的 Chrome/Edge。独立 profile 可与日常浏览器并存，
    # 也是新版浏览器允许 remote debugging 的必要条件。
    os.makedirs(CDP_PROFILE_DIR, exist_ok=True)
    log(f"正在启动 {browser_name}（远程调试模式，端口 9222）...")
    subprocess.Popen(
        [browser_path,
         "--remote-debugging-address=127.0.0.1",
         "--remote-debugging-port=9222",
         f"--user-data-dir={CDP_PROFILE_DIR}",
         "--no-first-run",
         "--no-default-browser-check",
         "--remote-allow-origins=*",
         TARGET_URL],
        # shell=True 会让 Windows 对参数重新解析，导致调试参数可能失效。
        shell=False,
    )
    log(f"{browser_name} 已启动并打开优学院登录页，请在浏览器中登录，抓包会在后台持续进行...")
    return True


def get_debug_targets():
    try:
        resp = urlopen(Request(CHROME_DEBUG_URL), timeout=3)
        return json.loads(resp.read())
    except Exception:
        return []


def get_browser_ws_url():
    # /json/version 是 Chrome/Edge 获取 browser target 的标准接口。
    try:
        resp = urlopen(Request(CHROME_VERSION_URL), timeout=3)
        data = json.loads(resp.read())
        ws_url = data.get("webSocketDebuggerUrl")
        if ws_url:
            return ws_url
    except Exception:
        pass

    # 个别旧版本会把 browser target 放在 /json。
    for t in get_debug_targets():
        if t.get("type") == "browser":
            return t.get("webSocketDebuggerUrl")

    return None


def attach_to_target(target_id):
    def on_network_enabled(msg):
        sid = (msg.get("sessionId", "?"))[:8]
        if "error" in msg:
            log(f"  ⚠️ session {sid} Network.enable 失败: {msg['error']}")
        else:
            log(f"✅ session {sid} Network.enable 成功")

    def on_attached(msg):
        if "error" in msg:
            err = msg["error"]
            tid = target_id[:8] if target_id else "?"
            log(f"  ⚠️ attach {tid} 失败: {err}")
            return
        session_id = (msg.get("result") or {}).get("sessionId")
        if not session_id:
            return
        sessions[session_id] = {"target_id": target_id, "type": "page", "url": ""}
        session_request_maps[session_id] = {}
        log(f"✅ 已附加到标签页 {target_id[:8]} (session {session_id[:8]})")
        send_to_session(session_id, "Network.enable", callback=on_network_enabled)
        send_to_session(session_id, "Page.enable")

    send_to_browser("Target.attachToTarget", {
        "targetId": target_id,
        "flatten": True,
    }, callback=on_attached)


def detach_session(session_id):
    if session_id in sessions:
        del sessions[session_id]
    if session_id in session_request_maps:
        del session_request_maps[session_id]


def handle_session_message(session_id, msg):
    try:
        _handle_session_message(session_id, msg)
    except Exception as e:
        import traceback
        log(f"💥 处理 session 消息时出错: {e}")
        try:
            log(f"   消息: {json.dumps(msg)[:300]}")
        except Exception:
            pass
        log(f"   {traceback.format_exc().replace(chr(10), ' | ')}")


def _handle_session_message(session_id, msg):
    info = sessions.get(session_id, {})
    target_id = info.get("target_id", "?")[:8]
    target_url = info.get("url", "?")
    request_map = session_request_maps.get(session_id, {})

    method = msg.get("method", "")
    if not method:
        return

    if method == "Page.frameNavigated":
        frame = (msg.get("params") or {}).get("frame", {})
        url = frame.get("url", "")
        info["url"] = url
        if url and should_log(url):
            log(f"📄 [{target_id}] 跳转: {url}")

    elif method == "Network.requestWillBeSent":
        params = (msg.get("params") or {})
        req = params.get("request", {})
        req_id = params.get("requestId", "")
        url = req.get("url", "")
        method_http = req.get("method", "")
        resource_type = params.get("type", "")
        info_req = request_map.setdefault(req_id, {})
        info_req.update({
            "url": url, "method": method_http,
            "resource_type": resource_type,
            "log_network": should_log_http_resource(url, resource_type),
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        })
        if info_req["log_network"]:
            full_url = url
            request_headers = req.get("headers") or {}
            content_type = next((str(value) for key, value in request_headers.items() if str(key).lower() == "content-type"), "")
            log(f"  [{target_id}] >> {method_http} {full_url}")
            log_headers(f"  [{target_id}] >> 请求头 {method_http} {full_url}", request_headers)
            extra_headers = info_req.get("request_extra_headers")
            if extra_headers and not info_req.get("request_extra_headers_logged"):
                log_headers(f"  [{target_id}] >> CDP 补充请求头 {full_url}", extra_headers)
                info_req["request_extra_headers_logged"] = True

            post_data = req.get("postData")
            if post_data is not None:
                log_body(f"  [{target_id}] >> 请求体 {method_http} {full_url}", post_data, content_type)
            elif req.get("hasPostData"):
                def on_post_data(body_msg, page_url=url, http_method=method_http, body_content_type=content_type):
                    if "error" in body_msg:
                        log(f"  [{target_id}] ⚠️ 读取请求体失败: {page_url} | {body_msg['error']}")
                        return
                    body = (body_msg.get("result") or {}).get("postData", "")
                    log_body(f"  [{target_id}] >> 请求体 {http_method} {page_url}", body, body_content_type)

                send_to_session(session_id, "Network.getRequestPostData", {"requestId": req_id}, callback=on_post_data)

    elif method == "Network.requestWillBeSentExtraInfo":
        params = msg.get("params") or {}
        req_id = params.get("requestId", "")
        info_req = request_map.setdefault(req_id, {})
        extra_headers = params.get("headers") or {}
        info_req["request_extra_headers"] = extra_headers
        url = info_req.get("url", "")
        if url and info_req.get("log_network") and not info_req.get("request_extra_headers_logged"):
            log_headers(f"  [{target_id}] >> CDP 补充请求头 {url}", extra_headers)
            info_req["request_extra_headers_logged"] = True

    elif method == "Network.responseReceived":
        params = (msg.get("params") or {})
        resp = params.get("response", {})
        req_id = params.get("requestId", "")
        url = resp.get("url", "")
        status = resp.get("status", 0)
        mime = resp.get("mimeType", "")
        size = resp.get("encodedDataLength", 0)
        info_req = request_map.setdefault(req_id, {})
        resource_type = params.get("type", info_req.get("resource_type", ""))
        info_req.update({
            "url": url, "status": status, "mime": mime,
            "resource_type": resource_type,
            "log_network": should_log_http_resource(url, resource_type),
        })
        if info_req["log_network"]:
            full_url = url
            log(f"  [{target_id}] << {status} {size}b {mime[:20]} {full_url}")
            log_headers(f"  [{target_id}] << 响应头 {status} {full_url}", resp.get("headers") or {})
            extra_headers = info_req.get("response_extra_headers")
            if extra_headers and not info_req.get("response_extra_headers_logged"):
                log_headers(f"  [{target_id}] << CDP 补充响应头 {full_url}", extra_headers)
                info_req["response_extra_headers_logged"] = True

    elif method == "Network.responseReceivedExtraInfo":
        params = msg.get("params") or {}
        req_id = params.get("requestId", "")
        info_req = request_map.setdefault(req_id, {})
        extra_headers = params.get("headers") or {}
        info_req["response_extra_headers"] = extra_headers
        url = info_req.get("url", "")
        if url and info_req.get("log_network") and not info_req.get("response_extra_headers_logged"):
            log_headers(f"  [{target_id}] << CDP 补充响应头 {url}", extra_headers)
            info_req["response_extra_headers_logged"] = True

    elif method == "Network.loadingFinished":
        params = (msg.get("params") or {})
        req_id = params.get("requestId", "")
        info_req = request_map.get(req_id, {})
        url = info_req.get("url", "?")
        if info_req.get("log_network"):
            log(f"  [{target_id}] ✅ 加载完成: {url}")
        if info_req.get("log_network") and should_capture_response_body(url, info_req.get("mime", "")):
            def on_response_body(body_msg, page_url=url, content_type=info_req.get("mime", "")):
                if "error" in body_msg:
                    log(f"  [{target_id}] ⚠️ 读取响应正文失败: {page_url} | {body_msg['error']}")
                    return
                result = body_msg.get("result") or {}
                body = result.get("body", "")
                if result.get("base64Encoded"):
                    try:
                        body = base64.b64decode(body).decode("utf-8", errors="replace")
                    except Exception as e:
                        log(f"  [{target_id}] ⚠️ 响应正文 Base64 解码失败: {page_url} | {e}")
                        return
                log_body(f"  [{target_id}] << 响应正文 {page_url}", body, content_type)

            send_to_session(session_id, "Network.getResponseBody", {"requestId": req_id}, callback=on_response_body)

    elif method == "Network.loadingFailed":
        params = (msg.get("params") or {})
        req_id = params.get("requestId", "")
        error_text = params.get("errorText", "")
        info_req = request_map.get(req_id, {})
        url = info_req.get("url", "?")
        if info_req.get("log_network"):
            log(f"  [{target_id}] ❌ 加载失败 [{error_text}]: {url}")

    elif method == "Network.webSocketCreated":
        params = (msg.get("params") or {})
        req_id = params.get("requestId", "")
        url = params.get("url", "")
        initiator = (params.get("initiator") or {}).get("type", "")
        if req_id:
            request_map.setdefault(req_id, {}).update({"url": url, "resource_type": "WebSocket"})
        if url and should_log(url):
            log(f"  [{target_id}] 🌐 WS 创建: {url} (initiator: {initiator})")

    elif method == "Network.webSocketWillSendHandshakeRequest":
        params = (msg.get("params") or {})
        req_id = params.get("requestId", "")
        req = params.get("request", {})
        headers = (req.get("headers") or {})
        info_req = request_map.get(req_id, {})
        url = info_req.get("url", "?")
        if should_log(url):
            log_headers(f"  [{target_id}] 🌐 WS 握手请求: {url}", headers)

    elif method == "Network.webSocketHandshakeResponseReceived":
        params = (msg.get("params") or {})
        req_id = params.get("requestId", "")
        resp = params.get("response", {})
        status = resp.get("status", 0)
        info_req = request_map.get(req_id, {})
        url = info_req.get("url", "?")
        if should_log(url):
            log(f"  [{target_id}] 🌐 WS 握手响应: {status} {url}")

    elif method == "Network.webSocketFrameReceived":
        params = (msg.get("params") or {})
        req_id = params.get("requestId", "")
        payload = (params.get("response") or {}).get("payloadData", "")
        info_req = request_map.get(req_id, {})
        url = info_req.get("url", "?")
        if payload and payload not in ("2", "3", '2::', '3::', "🅥🅗🅞🅚") and should_log(url):
            log(f"  [{target_id}] 🌐 WS << {url} | {payload}")

    elif method == "Network.webSocketFrameSent":
        params = (msg.get("params") or {})
        req_id = params.get("requestId", "")
        payload = (params.get("response") or {}).get("payloadData", "")
        info_req = request_map.get(req_id, {})
        url = info_req.get("url", "?")
        if payload and payload not in ("2", "3", '2::', '3::', "🅥🅗🅞🅚") and should_log(url):
            log(f"  [{target_id}] 🌐 WS >> {url} | {payload}")

    elif method == "Network.webSocketClosed":
        params = (msg.get("params") or {})
        req_id = params.get("requestId", "")
        info_req = request_map.get(req_id, {})
        url = info_req.get("url", "?")
        if should_log(url):
            log(f"  [{target_id}] 🌐 WS 关闭: {url}")


def on_browser_message(ws, message):
    if not running:
        return
    try:
        msg = json.loads(message)
    except json.JSONDecodeError:
        return

    try:
        _handle_browser_message(ws, msg)
    except Exception as e:
        import traceback
        log(f"💥 处理消息时出错: {e}")
        log(f"   消息: {message[:300]}")
        log(f"   {traceback.format_exc().replace(chr(10), ' | ')}")


def _handle_browser_message(ws, msg):
    msg_id = msg.get("id")
    if msg_id and msg_id in cmd_callbacks:
        cb = cmd_callbacks.pop(msg_id)
        cb(msg)
        return

    session_id = msg.get("sessionId")
    method = msg.get("method", "")

    # Browser-level events (no sessionId)
    if not session_id:
        if method == "Target.targetCreated":
            target = (msg.get("params") or {}).get("targetInfo", {})
            target_id = target.get("id")
            if not target_id:
                return
            target_type = target.get("type")
            url = target.get("url", "")
            if should_log(url):
                log(f"📑 新标签页: {url}")
            if target_type == "page":
                attach_to_target(target_id)

        elif method == "Target.targetDestroyed":
            target_id = (msg.get("params") or {}).get("targetId")
            for sid, info in list(sessions.items()):
                if info.get("target_id") == target_id:
                    detach_session(sid)
                    log(f"🗑️  标签页关闭: {target_id[:8]}")
                    break

        elif method == "Target.targetInfoChanged":
            target = (msg.get("params") or {}).get("targetInfo", {})
            target_id = target.get("id")
            url = target.get("url", "")
            for info in sessions.values():
                if info.get("target_id") == target_id:
                    info["url"] = url
                    break
        return

    # Session-level events
    handle_session_message(session_id, msg)


def on_browser_open(ws):
    log("🌐 Browser CDP 连接已建立，等待开始抓包...")
    if capture_enabled:
        start_capture()
    else:
        log("浏览器调试已就绪；在此终端输入 s 并按回车后开始抓包。")


def start_capture():
    """启用目标发现并附加现有标签页；可在 CDP 重连后安全地重复调用。"""
    global capture_enabled
    was_enabled = capture_enabled
    capture_enabled = True
    if not was_enabled:
        log("▶ 已开始抓包：正在监听所有标签页...")
    send_to_browser("Target.setDiscoverTargets", {"discover": True})
    # 附加到当前所有已存在的 page target
    attached_ids = {info.get("target_id") for info in sessions.values()}
    for t in get_debug_targets():
        target_id = t.get("id")
        if t.get("type") == "page" and target_id and target_id not in attached_ids:
            attach_to_target(target_id)


def on_browser_close(ws, close_status_code, close_msg):
    if running:
        log("Browser 连接断开")


def on_browser_error(ws, error):
    log(f"Browser WebSocket 错误: {error}")


def auto_attach_loop(fp):
    """Background thread: periodically attach to new page targets."""
    global running
    while running:
        time.sleep(3)
        if not running or not browser_ws or not capture_enabled:
            continue
        attached_ids = {info.get("target_id") for info in sessions.values()}
        for t in get_debug_targets():
            target_id = t.get("id")
            target_type = t.get("type")
            if target_type == "page" and target_id and target_id not in attached_ids:
                log(f"🔄 发现新标签页，自动附加: {t.get('url', '')}", fp)
                attach_to_target(target_id)


def auto_refresh_loop(fp):
    """抓包开始后定时刷新东莞理工/优学院页面。"""
    global running
    next_refresh = None
    while running:
        time.sleep(1)
        if not running:
            break
        if not capture_enabled or AUTO_REFRESH_INTERVAL <= 0:
            next_refresh = None
            continue
        if next_refresh is None:
            next_refresh = time.monotonic() + AUTO_REFRESH_INTERVAL
            continue
        if time.monotonic() < next_refresh:
            continue

        next_refresh = time.monotonic() + AUTO_REFRESH_INTERVAL
        refreshed_targets = set()
        # 新附加的旧标签页可能尚未产生 frameNavigated 事件，
        # 因此同时从调试目标列表取当前 URL。
        target_urls = {t.get("id"): t.get("url", "") for t in get_debug_targets()}
        for sid, info in list(sessions.items()):
            target_id = info.get("target_id")
            url = info.get("url", "") or target_urls.get(target_id, "")
            if (not url.startswith(("http://", "https://"))
                    or not any(k in url.lower() for k in AUTO_REFRESH_URL_KEYWORDS)
                    or target_id in refreshed_targets):
                continue
            refreshed_targets.add(target_id)

            def on_reloaded(msg, page_url=url):
                if "error" in msg:
                    log(f"⚠️ 自动刷新失败: {page_url} | {msg['error']}", fp)
                else:
                    log(f"🔄 已自动刷新: {page_url}", fp)

            send_to_session(sid, "Page.reload", {"ignoreCache": False}, callback=on_reloaded)


def keyboard_listener(fp):
    global running, SHOW_ALL_REQUESTS
    while running:
        try:
            cmd = input().strip().lower()
        except EOFError:
            break
        if cmd == "s":
            if capture_enabled:
                log("抓包已经在进行中。", fp)
            elif not browser_ws:
                log("浏览器调试器尚未连接，请稍候再输入 s。", fp)
            else:
                start_capture()
        elif cmd == "q":
            running = False
            if browser_ws:
                browser_ws.close()
            break
        elif cmd == "a":
            SHOW_ALL_REQUESTS = not SHOW_ALL_REQUESTS
            log(f"全量请求显示: {'开启' if SHOW_ALL_REQUESTS else '关闭'}", fp)
        elif cmd == "l":
            log("--- 已附加的标签页 ---", fp)
            if not sessions:
                log("  (暂无)", fp)
            for sid, info in sessions.items():
                url = info.get("url", "?")
                log(f"  [{info.get('target_id', '?')[:8]}] {url}", fp)
        elif cmd == "r":
            log("重新附加到所有标签页...", fp)
            attached_ids = {info.get("target_id") for info in sessions.values()}
            for t in get_debug_targets():
                if t.get("type") == "page" and t.get("id") not in attached_ids:
                    attach_to_target(t.get("id"))


def capture():
    global running, browser_ws, log_fp
    fp = open(LOG_FILE, "w", encoding="utf-8")
    log_fp = fp
    log(f"日志文件: {LOG_FILE}", fp)
    log("=" * 60, fp)
    log("优学院 CDP 抓包工具 v2.4（完整 API URL + WebSocket + 静态资源过滤）", fp)
    log("=" * 60, fp)
    log("快捷键: [s] 开始抓包  [r] 重新附加  [l] 列出标签页  [a] 切换全量显示  [q] 退出", fp)
    log("浏览器会先以调试模式启动；输入 s 并按回车后才开始监听网络请求。", fp)
    log("⚠️ 日志会原样包含请求体、API/JSON 正文、Cookie、Token 和完整头信息，请勿公开分享。", fp)
    log("默认已过滤图片、JS、CSS、字体和媒体资源；输入 a 可临时切换全量显示。", fp)
    if AUTO_REFRESH_INTERVAL > 0:
        log(f"自动刷新：抓包开始后每 {AUTO_REFRESH_INTERVAL} 秒刷新东莞理工/优学院页面。", fp)
    log("", fp)

    def handle_sigint(sig, frame):
        global running
        running = False
        log("\n正在停止...", fp)
        if browser_ws:
            browser_ws.close()
        fp.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)
    threading.Thread(target=keyboard_listener, args=(fp,), daemon=True).start()
    threading.Thread(target=auto_attach_loop, args=(fp,), daemon=True).start()
    threading.Thread(target=auto_refresh_loop, args=(fp,), daemon=True).start()

    reconnect_delay = 1
    while running:
        if not is_debug_port_open():
            browser_path, browser_name = find_browser()
            if not browser_path:
                log("未找到 Edge 或 Chrome 浏览器", fp)
                break
            launch_browser(browser_path, browser_name)
            time.sleep(2)

        browser_ws_url = None
        for attempt in range(15):
            browser_ws_url = get_browser_ws_url()
            if browser_ws_url:
                break
            log(f"等待 Browser target 就绪... ({attempt + 1}/15)", fp)
            time.sleep(1)

        if not browser_ws_url:
            log("未找到 Browser target，当前 http://127.0.0.1:9222/json 返回:", fp)
            targets = get_debug_targets()
            if not targets:
                log("  (空，端口 9222 完全没有响应)", fp)
            for t in targets:
                log(f"  type={t.get('type'):10} url={t.get('url', '')}", fp)
            log("5 秒后重试...", fp)
            time.sleep(5)
            continue

        log(f"Browser WebSocket: {browser_ws_url}", fp)

        browser_ws = websocket.WebSocketApp(
            browser_ws_url,
            on_open=on_browser_open,
            on_message=on_browser_message,
            on_error=on_browser_error,
            on_close=on_browser_close,
        )

        browser_ws.run_forever()

        if not running:
            break

        log(f"⚠️ CDP 连接断开，{reconnect_delay} 秒后自动重连...", fp)
        sessions.clear()
        cmd_callbacks.clear()
        session_request_maps.clear()
        browser_ws = None
        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 30)

    fp.close()


if __name__ == "__main__":
    capture()
