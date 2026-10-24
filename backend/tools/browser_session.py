"""维护脚本的临时浏览器会话；不提供账号密码登录、探测或持久化。"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote, urlsplit

import requests
from websocket import WebSocketException, create_connection

from dgutbot.domain.school_auth import IDENTITY_URL, USER_AGENT


class BrowserSessionError(ValueError):
    pass


class BrowserSession:
    def __init__(self) -> None:
        self._session: requests.Session | None = None

    def clear(self) -> None:
        if self._session is not None:
            self._session.cookies.clear()
            self._session.headers.pop("Authorization", None)
            self._session.close()
        self._session = None

    @staticmethod
    def _browser_cookies(port: int) -> list[dict[str, Any]]:
        with requests.Session() as local:
            local.trust_env = False
            response = local.get(f"http://127.0.0.1:{port}/json/list", timeout=4, allow_redirects=False)
            response.raise_for_status()
            targets = response.json()
        if not isinstance(targets, list):
            raise BrowserSessionError("调试端口未返回浏览器页面。")
        target = next((item for item in targets if isinstance(item, dict) and item.get("type") == "page"
                       and urlsplit(str(item.get("url", ""))).hostname in
                       {"lms.dgut.edu.cn", "application.dgut.edu.cn"}), None)
        if target is None:
            raise BrowserSessionError("该调试窗口没有学校页面，请先打开并完成登录。")
        address = urlsplit(str(target.get("webSocketDebuggerUrl", "")))
        if address.scheme != "ws" or address.hostname not in {"127.0.0.1", "localhost", "::1"} or address.port != port:
            raise BrowserSessionError("浏览器调试连接地址无效。")
        ws = create_connection(address.geturl(), timeout=8, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
            for _ in range(100):
                message = json.loads(ws.recv())
                if message.get("id") == 1:
                    if message.get("error"):
                        raise BrowserSessionError("浏览器未能提供登录信息。")
                    cookies = message.get("result", {}).get("cookies", [])
                    if not isinstance(cookies, list):
                        raise BrowserSessionError("浏览器未返回有效 Cookie 列表。")
                    return cookies
            raise BrowserSessionError("浏览器未及时返回登录信息。")
        finally:
            ws.close()

    def import_browser(self, port: int) -> dict[str, int | None]:
        self.clear()
        if isinstance(port, bool) or not str(port).isdigit() or not 1024 <= int(port) <= 65535:
            raise BrowserSessionError("调试端口必须是 1024–65535 的整数。")
        session = requests.Session()
        try:
            for cookie in self._browser_cookies(int(port)):
                domain = str(cookie.get("domain", ""))
                if domain.lstrip(".") == "dgut.edu.cn" or domain.lstrip(".").endswith(".dgut.edu.cn"):
                    session.cookies.set(cookie["name"], cookie["value"], domain=domain,
                                        path=cookie.get("path", "/"), secure=bool(cookie.get("secure")))
            tokens = list(dict.fromkeys(unquote(cookie.value) for cookie in session.cookies
                                        if cookie.name.upper() == "AUTHORIZATION" and cookie.value))
            if len(tokens) != 1:
                raise BrowserSessionError("未找到唯一登录凭据，请完成浏览器登录后重新导入。")
            session.headers.update({"Authorization": tokens[0], "User-Agent": USER_AGENT,
                                    "Accept": "application/json", "Origin": "https://lms.dgut.edu.cn",
                                    "Referer": "https://lms.dgut.edu.cn/"})
            response = session.post(IDENTITY_URL, json={}, timeout=12, allow_redirects=False)
            if response.status_code != 200:
                raise BrowserSessionError("登录身份验证失败，请重新登录后再试。")
            data = response.json()
            if not isinstance(data, dict) or isinstance(data.get("code"), bool) or data.get("code") not in (1, 200):
                raise BrowserSessionError("服务器未确认登录身份。")
            containers = [data] + [data[key] for key in ("result", "data", "userInfo", "user") if isinstance(data.get(key), dict)]
            identity = next((item for item in containers if "userId" in item or "userID" in item), {})
            user_id = identity.get("userId", identity.get("userID"))
            if isinstance(user_id, bool) or not str(user_id).isdigit() or int(user_id) <= 0:
                raise BrowserSessionError("身份接口未返回有效用户编号。")
            role = identity.get("roleId", identity.get("roleID", identity.get("role")))
            role = int(role) if not isinstance(role, bool) and str(role).isdigit() else None
            self._session = session
            return {"userId": int(user_id), "roleId": role}
        except BrowserSessionError:
            session.close()
            raise
        except (requests.RequestException, WebSocketException, OSError, ValueError, KeyError, TypeError):
            session.close()
            raise BrowserSessionError("无法导入登录状态，请检查端口、学校页面及登录状态。") from None
