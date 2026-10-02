"""独立登录会话：仅获取和验证身份，凭据只保留在后端内存。"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
from websocket import WebSocketException, create_connection


SCHOOL_HOSTS = ("lms.dgut.edu.cn", "application.dgut.edu.cn")
# 探测范围：Chromium 习惯使用的调试端口；小皮卡自己的端口会额外加入。
DISCOVERY_PORTS = tuple(range(9222, 9236))
# 勾选“记住账号密码”后保存上一次使用的账号，用于免去重复输入；明文只存本机。
SAVED_ACCOUNT_FILE = "independent_account.json"
LOGIN_URL = "https://lms.dgut.edu.cn/courseapi/users/login/v2"
IDENTITY_URL = "https://application.dgut.edu.cn/classroomapi/users/getUserInfo"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
LOGIN_ORIGIN = "https://application.dgut.edu.cn"
LOGIN_REFERER = "https://application.dgut.edu.cn/application"
# 只有达到凭据量级的值才参与脱敏。isClassroom、urlStyle 等开关类 Cookie 的值是
# "0"/"1"，把它们当秘密替换会把身份显示打碎成 ck[已隐藏]34[已隐藏]…。
MASK_MIN_LENGTH = 8
# 账号密码登录接口的实测结论（2026-09-29）：
#   lms.dgut.edu.cn/courseapi/users/login/v2  无效账号会下发 urlStyle/baseHost，说明路由有效
#   application.dgut.edu.cn/appapi/user/login/app  完全不下发 Cookie，已不再作为登录入口
# 前者与学校现用的油猴登录脚本使用同一接口和表单字段。
PASSWORD_REJECTED = ("账号密码登录未通过：服务器没有下发登录凭据。请确认账号密码；"
    "优学院注册账号、需要验证码或统一身份认证的账号，请改用浏览器导入。")


def login_headers():
    """账号密码登录请求头；独立登录与签到模块的账号重新登录共用同一份。"""
    return {"Content-Type": "application/x-www-form-urlencoded", "Origin": LOGIN_ORIGIN,
            "Referer": LOGIN_REFERER, "User-Agent": USER_AGENT}


class IndependentLoginError(ValueError):
    pass


class IndependentLogin:
    def __init__(self, session_factory=None, root=None):
        self._factory = session_factory or requests.Session
        self._root = Path(root) if root is not None else None
        self._lock = threading.RLock()
        self._generation = 0
        self._session = None
        self._status = self._empty()
        self._saved = self._read_saved()

    @staticmethod
    def _empty():
        return {"authenticated": False, "userId": None, "displayName": "", "accountName": "",
                "roleId": None, "source": "", "verifiedAt": "", "hasToken": False, "cookieNames": []}

    def _saved_path(self):
        return None if self._root is None else self._root / SAVED_ACCOUNT_FILE

    def _read_saved(self):
        path = self._saved_path()
        if path is None:
            return {}
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(values, dict):
            return {}
        username, password = values.get("username"), values.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
            return {}
        return {"username": username, "password": password}

    def remembered(self):
        """只回报账号与“是否已保存密码”，绝不回报密码本身。"""
        with self._lock:
            return {"username": self._saved.get("username", ""), "hasPassword": bool(self._saved.get("password"))}

    def _store_saved(self, username, password):
        path = self._saved_path()
        with self._lock:
            self._saved = {"username": username, "password": password}
            if path is None:
                return
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f"{path.name}.tmp")
                temporary.write_text(json.dumps(self._saved, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, path)
            except OSError:
                # 记不住不影响本次登录结果。
                pass

    def clear_saved_account(self):
        """只清除本机保存的账号密码，不影响当前会话。"""
        with self._lock:
            self._saved = {}
        path = self._saved_path()
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return self.remembered()

    def _saved_login(self, username, password):
        """密码留空时复用本机保存的凭据；账号不匹配则不猜。"""
        with self._lock:
            saved = dict(self._saved)
        if password or not saved.get("password"):
            return username, password
        if username and username != saved["username"]:
            return username, password
        return username or saved["username"], saved["password"]

    def status(self):
        with self._lock:
            return {**self._status, "cookieNames": list(self._status["cookieNames"]),
                    "remembered": self.remembered()}

    def clear(self):
        with self._lock:
            self._generation += 1
            if self._session is not None:
                self._session.cookies.clear()
                self._session.headers.pop("Authorization", None)
                self._session.close()
            self._session = None
            self._status = self._empty()
            return self.status()

    def _begin(self):
        with self._lock:
            self.clear()
            return self._generation

    @staticmethod
    def _tokens(session):
        """学校域下所有 AUTHORIZATION 候选值；重复值只保留一次。"""
        return list(dict.fromkeys(unquote(c.value) for c in session.cookies
            if c.name.upper() == "AUTHORIZATION" and c.value
            and (c.domain.lstrip(".") == "dgut.edu.cn" or c.domain.lstrip(".").endswith(".dgut.edu.cn"))))

    @classmethod
    def _token(cls, session):
        tokens = cls._tokens(session)
        if len(tokens) != 1:
            raise IndependentLoginError("未找到唯一登录凭据，请完成浏览器登录后重新导入。")
        return tokens[0]

    @staticmethod
    def _mask_values(token, password, cookies):
        """脱敏目标：登录凭据与密码始终参与；Cookie 只取长度达到凭据量级的值。"""
        values = [token, password]
        values.extend(c.value for c in cookies if isinstance(c.value, str) and len(c.value) >= MASK_MIN_LENGTH)
        return tuple(value for value in dict.fromkeys(values) if isinstance(value, str) and value)

    @staticmethod
    def _text(value, secrets):
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            return ""
        text = " ".join(str(value).split())[:80]
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[已隐藏]")
        return text

    def _verify(self, session, token, source, account="", password=""):
        session.headers.update({"Accept": "application/json", "Origin": "https://lms.dgut.edu.cn", "Referer": "https://lms.dgut.edu.cn/"})
        session.headers["Authorization"] = token
        response = session.post(IDENTITY_URL, json={}, timeout=12, allow_redirects=False)
        if response.status_code != 200:
            raise IndependentLoginError("登录身份验证失败，请重新登录后再试。")
        try:
            data = response.json()
        except ValueError:
            raise IndependentLoginError("身份接口未返回有效结果。") from None
        if not isinstance(data, dict) or isinstance(data.get("code"), bool) or data.get("code") not in (1, 200):
            raise IndependentLoginError("服务器未确认登录身份。")
        containers = [data] + [data[k] for k in ("result", "data", "userInfo", "user") if isinstance(data.get(k), dict)]
        identity = next((c for c in containers if "userId" in c or "userID" in c), {})
        raw_id = identity.get("userId", identity.get("userID"))
        if isinstance(raw_id, bool) or not str(raw_id).isdigit() or int(raw_id) <= 0:
            raise IndependentLoginError("身份接口未返回有效用户编号。")
        secrets = self._mask_values(token, password, session.cookies)
        def first(keys):
            return next((self._text(identity.get(k), secrets) for k in keys if self._text(identity.get(k), secrets)), "")
        role = identity.get("roleId", identity.get("roleID", identity.get("role")))
        role = int(role) if not isinstance(role, bool) and str(role).isdigit() else None
        return {"authenticated": True, "userId": int(raw_id),
            "displayName": first(("realName", "name", "userName", "studentName")),
            "accountName": first(("loginName", "username", "account")) or self._text(account, secrets),
            "roleId": role, "source": source, "verifiedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "hasToken": True, "cookieNames": sorted({c.name for c in session.cookies})}

    def _finish(self, session, status, generation):
        with self._lock:
            if generation != self._generation:
                raise IndependentLoginError("本次登录已取消，凭据未保留。")
            self._session = session
            self._status = status
            return self.status()

    def login(self, username, password, remember=False):
        generation = self._begin()
        if not isinstance(username, str) or not isinstance(password, str):
            raise IndependentLoginError("请输入账号和密码。")
        username, password = self._saved_login(username.strip(), password)
        if not username or not password or len(username) > 256 or len(password) > 1024:
            raise IndependentLoginError("请输入有效账号和密码。")
        session = self._factory()
        try:
            response = session.post(LOGIN_URL,
                data={"loginName": username, "password": password},
                headers=login_headers(), timeout=12, allow_redirects=False)
            if response.status_code not in (200, 302):
                raise IndependentLoginError(
                    f"账号密码登录被服务器拒绝（HTTP {response.status_code}），请确认账号密码或改用浏览器导入。")
            tokens = self._tokens(session)
            if len(tokens) != 1:
                raise IndependentLoginError(PASSWORD_REJECTED)
            status = self._verify(session, tokens[0], "password", username, password)
            self._finish(session, status, generation)
            # 先确认会话未被取消，再决定是否记住这次使用的凭据。
            if remember:
                self._store_saved(username, password)
            else:
                self.clear_saved_account()
            return self.status()
        except IndependentLoginError:
            session.close()
            raise
        except requests.RequestException:
            session.close()
            raise IndependentLoginError("无法完成账号登录，请检查网络或改用浏览器导入。") from None

    @classmethod
    def _school_target(cls, port, timeout=4):
        """读取该端口的页面列表，返回第一个学校页面及其已校验的 CDP 地址。

        只读取 /json/list 的元数据，不请求任何 Cookie。
        """
        with requests.Session() as local:
            local.trust_env = False
            response = local.get(f"http://127.0.0.1:{port}/json/list", timeout=timeout, allow_redirects=False)
            response.raise_for_status()
            targets = response.json()
        if not isinstance(targets, list):
            raise IndependentLoginError("调试端口未返回浏览器页面。")
        target = next((t for t in targets if isinstance(t, dict) and t.get("type") == "page"
            and urlsplit(str(t.get("url", ""))).hostname in SCHOOL_HOSTS), None)
        if target is None:
            raise IndependentLoginError("该调试窗口没有学校页面，请先打开并完成登录。")
        address = urlsplit(str(target.get("webSocketDebuggerUrl", "")))
        if address.scheme != "ws" or address.hostname not in ("127.0.0.1", "localhost", "::1") or address.port != port:
            raise IndependentLoginError("浏览器调试连接地址无效。")
        return target, address

    @classmethod
    def _probe(cls, port):
        """探测单个端口是否可导入，只读页面元数据；不可导入时返回 None。"""
        try:
            target, _address = cls._school_target(port, timeout=0.6)
        except (IndependentLoginError, requests.RequestException, OSError, ValueError):
            return None
        parsed = urlsplit(str(target.get("url", "")))
        return {"port": port, "title": cls._text(target.get("title"), ()),
                "host": parsed.hostname or "", "route": parsed.path or "/"}

    @classmethod
    def discover(cls, extra_ports=()):
        """列出本机开着调试端口且带学校页面的浏览器实例，供前端选择。"""
        ports = [port for port in dict.fromkeys((*extra_ports, *DISCOVERY_PORTS))
                 if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535]
        if not ports:
            return []
        with ThreadPoolExecutor(max_workers=8) as pool:
            found = [item for item in pool.map(cls._probe, ports) if item]
        return sorted(found, key=lambda item: item["port"])

    @classmethod
    def _browser_cookies(cls, port):
        target, address = cls._school_target(port)
        ws = create_connection(address.geturl(), timeout=8, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
            for _ in range(100):
                message = json.loads(ws.recv())
                if message.get("id") == 1:
                    if message.get("error"):
                        raise IndependentLoginError("浏览器未能提供登录信息。")
                    return message.get("result", {}).get("cookies", [])
            raise IndependentLoginError("浏览器未及时返回登录信息。")
        finally:
            ws.close()

    def import_browser(self, port):
        generation = self._begin()
        if isinstance(port, bool) or not str(port).isdigit() or not 1024 <= int(port) <= 65535:
            raise IndependentLoginError("调试端口必须是 1024–65535 的整数。")
        session = self._factory()
        try:
            for cookie in self._browser_cookies(int(port)):
                domain = str(cookie.get("domain", ""))
                if domain.lstrip(".") != "dgut.edu.cn" and not domain.lstrip(".").endswith(".dgut.edu.cn"):
                    continue
                session.cookies.set(cookie["name"], cookie["value"], domain=domain,
                    path=cookie.get("path", "/"), secure=bool(cookie.get("secure")))
            token = self._token(session)
            return self._finish(session, self._verify(session, token, "browser"), generation)
        except IndependentLoginError:
            session.close()
            raise
        except (requests.RequestException, WebSocketException, OSError, ValueError, KeyError, TypeError):
            session.close()
            raise IndependentLoginError("无法导入登录状态，请检查端口、学校页面及登录状态。") from None
