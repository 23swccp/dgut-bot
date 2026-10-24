"""诊断脚本临时会话的离线边界；应用不再创建独立登录模块。"""

import json
from unittest.mock import Mock, patch

import pytest
import requests

from backend.tools.browser_session import BrowserSession, BrowserSessionError
from dgutbot.app import commands


@pytest.mark.parametrize("command", ["get_independent_login_status", "list_independent_login_sources",
                                     "login_independent_account", "import_independent_login",
                                     "clear_independent_login", "clear_independent_account"])
def test_removed_login_commands_are_rejected(command):
    result = commands.handle(command, {})
    assert result == {"ok": False, "error": f"未知命令：{command}"}
    assert not hasattr(commands.backend, "independent_login")


def session():
    client = requests.Session()
    client.post = Mock(return_value=Mock(status_code=200, json=lambda: {"code": 1, "result": {"userId": 7, "roleId": 9}}))
    client.close = Mock()
    return client


def test_diagnostic_session_reads_school_cookie_verifies_identity_and_clears():
    client = session()
    login = BrowserSession()
    cookies = [{"name": "AUTHORIZATION", "value": "fixture-token", "domain": "lms.dgut.edu.cn"},
               {"name": "unrelated", "value": "private", "domain": "example.invalid"}]
    with patch("backend.tools.browser_session.requests.Session", return_value=client), \
         patch.object(BrowserSession, "_browser_cookies", return_value=cookies):
        result = login.import_browser(9222)
    assert result == {"userId": 7, "roleId": 9}
    assert client.headers["Authorization"] == "fixture-token" and "unrelated" not in client.cookies
    assert client.post.call_args.kwargs == {"json": {}, "timeout": 12, "allow_redirects": False}
    login.clear()
    assert login._session is None and "Authorization" not in client.headers and not client.cookies
    client.close.assert_called_once()


@pytest.mark.parametrize("cookies,data", [
    ([{"name": "AUTHORIZATION", "value": "bad", "domain": "notdgut.edu.cn"}], {"code": 1, "userId": 7}),
    ([{"name": "AUTHORIZATION", "value": "a", "domain": "lms.dgut.edu.cn"},
      {"name": "AUTHORIZATION", "value": "b", "domain": "application.dgut.edu.cn"}], {"code": 1, "userId": 7}),
    ([{"name": "AUTHORIZATION", "value": "a", "domain": "lms.dgut.edu.cn"}], {"code": 0}),
    ([{"name": "AUTHORIZATION", "value": "a", "domain": "lms.dgut.edu.cn"}], {"code": 1, "userId": True}),
])
def test_failed_identity_or_cookie_never_retains_session(cookies, data):
    client = session()
    client.post.return_value.json = lambda: data
    login = BrowserSession()
    with patch("backend.tools.browser_session.requests.Session", return_value=client), \
         patch.object(BrowserSession, "_browser_cookies", return_value=cookies):
        with pytest.raises(BrowserSessionError):
            login.import_browser(9222)
    assert login._session is None
    client.close.assert_called_once()


@pytest.mark.parametrize("address", ["ws://example.invalid:9222/devtools/page/a", "ws://127.0.0.1:9333/devtools/page/a"])
def test_debug_websocket_must_remain_on_same_loopback_port(address):
    local = Mock()
    local.get.return_value.json.return_value = [{"type": "page", "url": "https://lms.dgut.edu.cn/",
                                               "webSocketDebuggerUrl": address}]
    with patch("backend.tools.browser_session.requests.Session") as factory, \
         patch("backend.tools.browser_session.create_connection") as connect:
        factory.return_value.__enter__.return_value = local
        with pytest.raises(BrowserSessionError):
            BrowserSession._browser_cookies(9222)
    connect.assert_not_called()


def test_browser_cookie_reader_closes_websocket_and_ignores_unrelated_events():
    local = Mock()
    local.get.return_value.json.return_value = [{"type": "page", "url": "https://lms.dgut.edu.cn/",
                                               "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/a"}]
    ws = Mock()
    ws.recv.side_effect = [json.dumps({"method": "event"}), json.dumps({"id": 1, "result": {"cookies": []}})]
    with patch("backend.tools.browser_session.requests.Session") as factory, \
         patch("backend.tools.browser_session.create_connection", return_value=ws):
        factory.return_value.__enter__.return_value = local
        assert BrowserSession._browser_cookies(9222) == []
    ws.close.assert_called_once()
