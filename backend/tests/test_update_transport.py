"""真实 SDK 穿过 Python 传输层，所有网络均来自回环夹具。"""
import json
import threading
import time
import urllib.request
from unittest.mock import patch

import pytest
import requests

from dgutbot.app.update_transport import UpdateTransport
from dgutbot.app.updater import UpdateManager
from tools.update_lab import FeedServer, LabManager, LabTransport, prepare


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_windows_system_proxy_without_proxy_environment_transports_feed_and_package(tmp_path, monkeypatch):
    class ProxiedManager(LabManager):
        def _feed_url(self):
            return "http://unresolvable.invalid/releases/latest/download/releases.win.json"

        def _package_url(self, asset):
            return f"http://unresolvable.invalid/releases/download/v{asset.Version}/{asset.FileName}"

    root = tmp_path / "install"
    prepare(root)
    # Codex/CI 的环境代理曾掩盖桌面启动的故障：本例只提供 Windows 注册表代理。
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    # Windows 实现优先环境变量，再读取注册表；跨平台 CI 在这里模拟同一入口。
    with FeedServer("success") as proxy, patch("urllib.request.getproxies_environment", return_value={}), patch(
        "urllib.request.getproxies_registry", return_value={"http": proxy.origin}, create=True
    ), patch("urllib.request.getproxies", side_effect=lambda: urllib.request.getproxies_environment()
             or urllib.request.getproxies_registry()), patch(
        "urllib.request.proxy_bypass", return_value=False
    ):
        manager = ProxiedManager(root, proxy.origin)
        assert manager.check()["updateAvailable"]
        manager.start_download()
        manager._worker.join(3)
        assert manager.snapshot()["canInstall"], manager.snapshot()
        assert any(r["path"].endswith(".nupkg") for r in proxy.requests)


@pytest.mark.parametrize("current,expected", [("1.0.0", True), ("1.0.1", False), ("1.0.2", False)])
def test_source_checkout_checks_with_sdk_but_cannot_download_or_install(tmp_path, current, expected):
    class PreviewManager(UpdateManager):
        def _native_manager(self, source):
            raise RuntimeError("Could not auto-locate app manifest")

        def _feed_url(self):
            return feed.origin + "/releases/latest/download/releases.win.json"

    with FeedServer("success") as feed:
        manager = PreviewManager(tmp_path, version=current, repository="local/test", transport=LabTransport())
        result = manager.check()
        assert result["ok"], result
        assert result["updateAvailable"] == expected
        if expected:
            manager.start_download()
            manager._worker.join(3)
            assert manager.snapshot()["state"] == "download_failed"
            assert "不能安装" in manager.snapshot()["error"]
            assert not manager.install()["ok"]
        assert not list(tmp_path.rglob("*.nupkg"))
        assert not any(r["path"].endswith(".nupkg") for r in feed.requests)


def test_slow_or_rate_limited_notes_do_not_block_detection_and_can_refresh(tmp_path):
    root = tmp_path / "install"
    prepare(root)
    entered, release = threading.Event(), threading.Event()

    def limited(*args):
        entered.set()
        release.wait(3)
        raise RuntimeError("HTTP 403")

    with FeedServer("success") as feed:
        manager = LabManager(root, feed.origin)
        manager._transport.release_notes = limited
        try:
            assert manager.check()["updateAvailable"]
            assert entered.wait(1)
            assert manager.snapshot()["state"] == "available"
            assert manager.snapshot()["changelog"] == "本地合成更新包"
        finally:
            release.set()
        wait_for(lambda: not manager._notes_fetching)
        assert manager.snapshot()["error"] == ""
        manager._transport.release_notes = lambda repo, version: "维护者编辑后的正文"
        assert manager.check(manual=True)["ok"]
        wait_for(lambda: not manager._notes_fetching)
        assert manager.snapshot()["changelog"] == "维护者编辑后的正文"
        saved = json.loads(manager._notes_path.read_text(encoding="utf-8"))
        assert saved["notes"]["1.0.1"] == "维护者编辑后的正文"
        # API 失败时保留上次成功正文，且下载后手动检查仍能刷新正文。
        manager._transport.release_notes = limited
        assert manager.check()["ok"]
        wait_for(lambda: not manager._notes_fetching)
        assert manager.snapshot()["changelog"] == "维护者编辑后的正文"
        manager.start_download()
        manager._worker.join(3)
        assert manager.snapshot()["canInstall"]
        manager._transport.release_notes = lambda *args: "第二次编辑"
        assert manager.check(manual=True)["skipped"]
        wait_for(lambda: not manager._notes_fetching)
        assert manager.snapshot()["changelog"] == "第二次编辑"
        restarted = LabManager(root, feed.origin)
        restarted.restore()
        assert restarted.snapshot()["changelog"] == "第二次编辑"


def test_notes_are_fetched_by_exact_tag_and_mismatched_release_is_rejected():
    transport = UpdateTransport()
    with patch.object(transport, "read", return_value=b'{"tag_name":"v1.0.1","body":"edited"}') as read:
        assert transport.release_notes("owner/repo", "1.0.1") == "edited"
        assert read.call_args.args[0].endswith("/releases/tags/v1.0.1")
    with patch.object(transport, "read", return_value=b'{"tag_name":"v1.0.2","body":"wrong"}'):
        with pytest.raises(ValueError, match="版本不匹配"):
            transport.release_notes("owner/repo", "1.0.1")


def test_native_local_source_is_unaffected_by_invalid_proxy_environment(tmp_path, monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    root = tmp_path / "install"
    prepare(root)
    with FeedServer("success") as feed:
        manager = LabManager(root, feed.origin)
        assert manager.check()["updateAvailable"]
        manager.start_download()
        manager._worker.join(3)
        assert manager.snapshot()["canInstall"], manager.snapshot()


def test_package_path_cannot_escape_download_directory(tmp_path):
    from types import SimpleNamespace
    root = tmp_path / "install"
    prepare(root)
    with FeedServer("success") as feed:
        manager = LabManager(root, feed.origin)
        update = SimpleNamespace(TargetFullRelease=SimpleNamespace(FileName="../escape.nupkg"))
        with pytest.raises(ValueError, match="文件名"):
            manager._download_update(update, lambda value: None)
        assert not feed.requests


def test_proxy_authentication_is_not_exposed_in_request_errors():
    transport = UpdateTransport()
    with patch("requests.Session.get", side_effect=requests.exceptions.ProxyError("http://user:secret@example.com")):
        with pytest.raises(requests.ConnectionError) as error:
            transport.read("https://github.com/owner/repo")
    assert "secret" not in str(error.value)
    assert "user" not in str(error.value)
