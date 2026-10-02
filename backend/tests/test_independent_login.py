"""独立登录的隔离、凭据保护与取消行为；全部离线。"""
import json
import threading
from unittest.mock import Mock, patch

import pytest
import requests

from dgutbot.domain.independent_login import (
    IDENTITY_URL, LOGIN_ORIGIN, LOGIN_REFERER, LOGIN_URL, USER_AGENT, IndependentLogin, IndependentLoginError,
)
from dgutbot.domain.yxy_backend import SignBackend
from dgutbot.app import backend_commands

TOKEN = "test-independent-token-1234"
PASSWORD = "test-private-password-5678"


def response(data=None, status=200):
    result = Mock()
    result.status_code = status
    result.json.return_value = data
    return result


def client():
    session = Mock(spec=requests.Session)
    session.headers = {}
    session.cookies = requests.cookies.RequestsCookieJar()
    session.cookies.set("AUTHORIZATION", TOKEN, domain="application.dgut.edu.cn", path="/")
    session.post.side_effect = [response(), response({"code": 1, "result": {"userId": 123, "roleId": 81, "userName": "测试教师"}})]
    return session


def test_password_login_verifies_identity_before_retaining_credentials():
    session = client()
    service = IndependentLogin(lambda: session)
    status = service.login(" test-account ", PASSWORD)
    assert status["authenticated"] and status["userId"] == 123 and status["roleId"] == 81
    assert status["hasToken"] and status["cookieNames"] == ["AUTHORIZATION"]
    assert session.post.call_args_list[0].args == (LOGIN_URL,)
    assert LOGIN_URL == "https://lms.dgut.edu.cn/courseapi/users/login/v2"
    assert session.post.call_args_list[0].kwargs["data"] == {"loginName": "test-account", "password": PASSWORD}
    assert session.post.call_args_list[1].args == (IDENTITY_URL,)
    assert all(c.kwargs["allow_redirects"] is False for c in session.post.call_args_list)
    assert session.post.call_args_list[0].kwargs["headers"] == {
        "Content-Type": "application/x-www-form-urlencoded", "Origin": LOGIN_ORIGIN,
        "Referer": LOGIN_REFERER, "User-Agent": USER_AGENT}
    assert TOKEN not in json.dumps(status) and PASSWORD not in json.dumps(status)


def test_rejected_password_login_reports_actionable_message():
    session = Mock(spec=requests.Session)
    session.headers = {}
    session.cookies = requests.cookies.RequestsCookieJar()
    session.post.return_value = response(status=302)  # 学校拒绝：只重定向，不下发 AUTHORIZATION
    service = IndependentLogin(lambda: session)
    with pytest.raises(IndependentLoginError) as error:
        service.login("test-account", PASSWORD)
    message = str(error.value)
    assert "账号密码登录未通过" in message and "浏览器导入" in message
    assert PASSWORD not in message and not service.status()["authenticated"]
    session.close.assert_called_once()


def test_unexpected_login_http_status_is_reported_without_echoing_credentials():
    session = Mock(spec=requests.Session)
    session.headers = {}
    session.cookies = requests.cookies.RequestsCookieJar()
    session.post.return_value = response(status=500)
    with pytest.raises(IndependentLoginError) as error:
        IndependentLogin(lambda: session).login("test-account", PASSWORD)
    assert "500" in str(error.value) and PASSWORD not in str(error.value)
    session.close.assert_called_once()


def test_switching_cookies_never_mangle_identity_display():
    """开关类 Cookie 的值只有 1 个字符，参与脱敏会把账号显示打碎。"""
    session = client()
    session.cookies.set("isClassroom", "1", domain=".dgut.edu.cn", path="/")
    session.cookies.set("urlStyle", "1", domain=".dgut.edu.cn", path="/")
    session.cookies.set("recordState", "1", domain=".dgut.edu.cn", path="/")
    session.post.side_effect = [response(), response(
        {"code": 1, "userId": 123, "loginName": "ck1234560574@163.com", "name": "测试教师"})]
    status = IndependentLogin(lambda: session).login("test-account", PASSWORD)
    assert status["accountName"] == "ck1234560574@163.com"
    assert status["displayName"] == "测试教师"
    assert "[已隐藏]" not in json.dumps(status)


@pytest.mark.parametrize("payload", [{"code": 0, "userId": 123}, {"code": True, "userId": 123},
    {"code": 1}, {"code": 1, "userId": True}, {"code": 1, "userId": 0}, ["invalid"]])
def test_failed_identity_never_retains_candidate(payload):
    session = client()
    session.post.side_effect = [response(), response(payload)]
    service = IndependentLogin(lambda: session)
    with pytest.raises(IndependentLoginError):
        service.login("test-account", PASSWORD)
    assert not service.status()["authenticated"] and service._session is None
    session.close.assert_called_once()


def test_bad_password_response_cannot_echo_secrets_to_frontend():
    session = client()
    session.post.side_effect = requests.ConnectionError(f"password={PASSWORD}; Authorization={TOKEN}")
    service = IndependentLogin(lambda: session)
    with pytest.raises(IndependentLoginError) as exc:
        service.login("test-account", PASSWORD)
    assert TOKEN not in str(exc.value) and PASSWORD not in str(exc.value)


def test_identity_display_fields_cannot_echo_credentials():
    session = client()
    session.post.side_effect = [response(), response({"code": 200, "userId": 123, "name": PASSWORD, "loginName": TOKEN})]
    result = IndependentLogin(lambda: session).login("test-account", PASSWORD)
    assert PASSWORD not in json.dumps(result) and TOKEN not in json.dumps(result)


def test_clear_discards_cookie_and_header_and_status_copy_is_independent():
    session = client()
    service = IndependentLogin(lambda: session)
    status = service.login("test-account", PASSWORD)
    status["cookieNames"].append("mutated")
    assert "mutated" not in service.status()["cookieNames"]
    service.clear()
    assert not session.cookies and "Authorization" not in session.headers
    assert not service.status()["authenticated"]
    session.close.assert_called_once()


def test_clear_while_login_inflight_prevents_session_resurrection():
    entered, release = threading.Event(), threading.Event()
    session = client()
    errors = []
    def post(url, **kwargs):
        if url == LOGIN_URL:
            return response()
        entered.set()
        assert release.wait(3)
        return response({"code": 1, "userId": 123})
    session.post.side_effect = post
    service = IndependentLogin(lambda: session)
    def login():
        try:
            service.login("test-account", PASSWORD)
        except IndependentLoginError as error:
            errors.append(str(error))
    worker = threading.Thread(target=login)
    worker.start()
    assert entered.wait(3)
    service.clear()
    release.set()
    worker.join(3)
    assert not worker.is_alive() and errors and not service.status()["authenticated"]
    session.close.assert_called_once()


def test_browser_import_uses_only_school_cookies_and_verifies_without_password():
    session = client()
    session.cookies.clear()
    session.post.side_effect = [response({"code": 1, "userId": 456, "roleId": 81})]
    service = IndependentLogin(lambda: session)
    cookies = [{"name": "AUTHORIZATION", "value": TOKEN, "domain": ".dgut.edu.cn", "path": "/", "secure": True},
        {"name": "unrelated", "value": "other-secret", "domain": ".example.com"}]
    with patch.object(service, "_browser_cookies", return_value=cookies):
        result = service.import_browser(9223)
    assert result["source"] == "browser" and result["userId"] == 456
    assert result["cookieNames"] == ["AUTHORIZATION"]
    session.post.assert_called_once_with(IDENTITY_URL, json={}, timeout=12, allow_redirects=False)
    assert TOKEN not in json.dumps(result) and "other-secret" not in json.dumps(result)


@pytest.mark.parametrize("port", [True, 1023, 65536, "9223.5", "http://example.com", None])
def test_invalid_debug_ports_never_open_network(port):
    service = IndependentLogin()
    with patch.object(service, "_browser_cookies") as reader:
        with pytest.raises(IndependentLoginError):
            service.import_browser(port)
        reader.assert_not_called()


def test_conflicting_browser_tokens_do_not_guess_identity():
    session = client()
    session.cookies.set("AUTHORIZATION", "conflicting-token", domain="lms.dgut.edu.cn", path="/")
    service = IndependentLogin(lambda: session)
    with pytest.raises(IndependentLoginError):
        service.login("test-account", PASSWORD)
    session.post.assert_called_once()


def test_command_login_and_clear_leave_primary_session_and_files_untouched(tmp_path):
    backend = SignBackend(lambda *_: None, root=tmp_path)
    backend.token = "primary-token"
    backend.headers["Authorization"] = "primary-token"
    backend.user_id = 999
    backend.independent_login = IndependentLogin(lambda: client())
    with patch.object(backend_commands, "backend", backend):
        result = backend_commands.handle("login_independent_account", {"username": "test-account", "password": PASSWORD})
        assert result["ok"] and result["independentLogin"]["userId"] == 123
        assert TOKEN not in json.dumps(result) and PASSWORD not in json.dumps(result)
        assert backend_commands.handle("clear_independent_login", {})["ok"]
    assert backend.token == "primary-token" and backend.headers["Authorization"] == "primary-token" and backend.user_id == 999
    assert list(tmp_path.iterdir()) == []


def test_browser_reader_uses_local_cdp_and_closes_connection():
    local = Mock()
    local.__enter__ = Mock(return_value=local)
    local.__exit__ = Mock(return_value=False)
    local.get.return_value = response([{"type": "page", "url": "https://lms.dgut.edu.cn/",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/test"}])
    ws = Mock()
    ws.recv.side_effect = [json.dumps({"method": "unrelated"}), json.dumps({"id": 1, "result": {"cookies": [{"name": "AUTHORIZATION", "value": TOKEN}]}})]
    with patch("dgutbot.domain.independent_login.requests.Session", return_value=local), \
         patch("dgutbot.domain.independent_login.create_connection", return_value=ws) as connect:
        cookies = IndependentLogin._browser_cookies(9223)
    assert cookies[0]["value"] == TOKEN
    assert local.trust_env is False
    local.get.assert_called_once_with("http://127.0.0.1:9223/json/list", timeout=4, allow_redirects=False)
    connect.assert_called_once_with("ws://127.0.0.1:9223/devtools/page/test", timeout=8, suppress_origin=True)
    assert json.loads(ws.send.call_args.args[0])["method"] == "Network.getAllCookies"
    ws.close.assert_called_once()


@pytest.mark.parametrize("address", ["ws://example.com:9223/page", "ws://127.0.0.1:9999/page", "wss://127.0.0.1:9223/page"])
def test_browser_reader_rejects_nonlocal_or_wrong_port_debug_addresses(address):
    local = Mock()
    local.__enter__ = Mock(return_value=local)
    local.__exit__ = Mock(return_value=False)
    local.get.return_value = response([{"type": "page", "url": "https://lms.dgut.edu.cn/", "webSocketDebuggerUrl": address}])
    with patch("dgutbot.domain.independent_login.requests.Session", return_value=local), \
         patch("dgutbot.domain.independent_login.create_connection") as connect:
        with pytest.raises(IndependentLoginError):
            IndependentLogin._browser_cookies(9223)
        connect.assert_not_called()


def test_probe_reports_page_metadata_without_requesting_cookies():
    local = Mock()
    local.__enter__ = Mock(return_value=local)
    local.__exit__ = Mock(return_value=False)
    local.get.return_value = response([
        {"type": "page", "url": "https://example.com/", "title": "其他页面",
         "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/other"},
        {"type": "page", "url": "https://lms.dgut.edu.cn/courseweb/ulearning/index.html", "title": "我的首页",
         "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/school"}])
    with patch("dgutbot.domain.independent_login.requests.Session", return_value=local), \
         patch("dgutbot.domain.independent_login.create_connection") as connect:
        source = IndependentLogin._probe(9222)
    assert source == {"port": 9222, "title": "我的首页", "host": "lms.dgut.edu.cn",
                      "route": "/courseweb/ulearning/index.html"}
    connect.assert_not_called()
    assert local.get.call_args.kwargs == {"timeout": 0.6, "allow_redirects": False}


@pytest.mark.parametrize("payload,effect", [
    ([{"type": "page", "url": "https://example.com/", "title": "x",
       "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/x"}], None),
    ([{"type": "page", "url": "https://lms.dgut.edu.cn/", "title": "x",
       "webSocketDebuggerUrl": "ws://example.com:9222/devtools/page/x"}], None),
    (None, requests.ConnectionError("refused")),
])
def test_probe_skips_ports_that_are_not_importable(payload, effect):
    local = Mock()
    local.__enter__ = Mock(return_value=local)
    local.__exit__ = Mock(return_value=False)
    if effect is None:
        local.get.return_value = response(payload)
    else:
        local.get.side_effect = effect
    with patch("dgutbot.domain.independent_login.requests.Session", return_value=local), \
         patch("dgutbot.domain.independent_login.create_connection") as connect:
        assert IndependentLogin._probe(9222) is None
    connect.assert_not_called()


def test_discovery_sorts_importable_browsers_and_scans_configured_port():
    def probe(port):
        if port in (9223, 9222):
            return {"port": port, "title": f"窗口 {port}", "host": "lms.dgut.edu.cn", "route": "/"}
        return None
    with patch.object(IndependentLogin, "_probe", side_effect=probe) as probe_mock, \
         patch("dgutbot.domain.independent_login.create_connection") as connect:
        sources = IndependentLogin.discover([9333])
    assert [source["port"] for source in sources] == [9222, 9223]
    assert 9333 in [call.args[0] for call in probe_mock.call_args_list]
    assert 9222 in [call.args[0] for call in probe_mock.call_args_list]
    assert 9235 in [call.args[0] for call in probe_mock.call_args_list]
    assert 9236 not in [call.args[0] for call in probe_mock.call_args_list]
    connect.assert_not_called()


def test_command_lists_sources_with_configured_debug_port(tmp_path):
    backend = SignBackend(lambda *_: None, root=tmp_path)
    found = [{"port": 9222, "title": "我的首页", "host": "lms.dgut.edu.cn", "route": "/"}]
    with patch.object(backend.independent_login, "discover", return_value=found) as discover, \
         patch.object(backend_commands, "backend", backend):
        result = backend_commands.handle("list_independent_login_sources", {})
    assert result["ok"] and result["sources"] == found
    assert result["configuredPort"] == int(backend.config.debug_port)
    assert discover.call_args.args[0] == [int(backend.config.debug_port)]


def test_remembered_account_is_saved_without_exposing_password(tmp_path):
    service = IndependentLogin(lambda: client(), root=tmp_path)
    status = service.login("test-account", PASSWORD, remember=True)
    assert status["remembered"] == {"username": "test-account", "hasPassword": True}
    assert PASSWORD not in json.dumps(status)
    saved = json.loads((tmp_path / "independent_account.json").read_text(encoding="utf-8"))
    assert saved == {"username": "test-account", "password": PASSWORD}
    assert not (tmp_path / "independent_account.json.tmp").exists()
    # 重新载入：账号带出，密码留空时复用保存值，且不再要求前端提交密码。
    reloaded = IndependentLogin(lambda: client(), root=tmp_path)
    assert reloaded.remembered() == {"username": "test-account", "hasPassword": True}
    session = client()
    reused = IndependentLogin(lambda: session, root=tmp_path)
    reused.login("", "")
    assert session.post.call_args_list[0].kwargs["data"] == {"loginName": "test-account", "password": PASSWORD}
    session.post.assert_any_call(IDENTITY_URL, json={}, timeout=12, allow_redirects=False)


def test_saved_password_is_not_reused_for_another_account(tmp_path):
    IndependentLogin(lambda: client(), root=tmp_path).login("test-account", PASSWORD, remember=True)
    session = client()
    service = IndependentLogin(lambda: session, root=tmp_path)
    with pytest.raises(IndependentLoginError):
        service.login("another-account", "")
    session.post.assert_not_called()


def test_clearing_session_keeps_saved_account_and_clearing_account_keeps_session(tmp_path):
    session = client()
    service = IndependentLogin(lambda: session, root=tmp_path)
    service.login("test-account", PASSWORD, remember=True)
    service.clear()
    assert not service.status()["authenticated"]
    assert service.remembered()["hasPassword"] and (tmp_path / "independent_account.json").is_file()
    assert IndependentLogin(lambda: client(), root=tmp_path).login("", "")["authenticated"]
    service.clear_saved_account()
    assert service.remembered() == {"username": "", "hasPassword": False}
    assert not (tmp_path / "independent_account.json").exists()


def test_login_without_remember_clears_previous_saved_account(tmp_path):
    IndependentLogin(lambda: client(), root=tmp_path).login("test-account", PASSWORD, remember=True)
    service = IndependentLogin(lambda: client(), root=tmp_path)
    status = service.login("test-account", PASSWORD, remember=False)
    assert status["remembered"] == {"username": "", "hasPassword": False}
    assert not (tmp_path / "independent_account.json").exists()


def test_failed_login_does_not_touch_saved_account(tmp_path):
    IndependentLogin(lambda: client(), root=tmp_path).login("test-account", PASSWORD, remember=True)
    rejected = Mock(spec=requests.Session)
    rejected.headers = {}
    rejected.cookies = requests.cookies.RequestsCookieJar()
    rejected.post.return_value = response(status=302)
    service = IndependentLogin(lambda: rejected, root=tmp_path)
    with pytest.raises(IndependentLoginError):
        service.login("test-account", PASSWORD, remember=False)
    assert service.remembered() == {"username": "test-account", "hasPassword": True}
    assert (tmp_path / "independent_account.json").is_file()


def test_sessions_without_root_never_touch_saved_account_file():
    service = IndependentLogin(lambda: client())
    assert service.remembered() == {"username": "", "hasPassword": False}
    assert service.login("test-account", PASSWORD, remember=True)["authenticated"]
    assert service.remembered() == {"username": "test-account", "hasPassword": True}
    assert service.clear_saved_account() == {"username": "", "hasPassword": False}


def test_command_remember_clear_and_password_never_returned(tmp_path):
    backend = SignBackend(lambda *_: None, root=tmp_path)
    backend.independent_login = IndependentLogin(lambda: client(), root=tmp_path)
    with patch.object(backend_commands, "backend", backend):
        result = backend_commands.handle("login_independent_account",
            {"username": "test-account", "password": PASSWORD, "remember": True})
        assert result["ok"] and result["independentLogin"]["remembered"]["hasPassword"]
        assert PASSWORD not in json.dumps(result)
        cleared = backend_commands.handle("clear_independent_account", {})
        assert cleared["ok"] and cleared["independentLogin"]["remembered"] == {"username": "", "hasPassword": False}
        assert cleared["independentLogin"]["authenticated"] is True
    assert not (tmp_path / "independent_account.json").exists()
