from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dgutbot.app.velopack_updater import UpdateManager, select_targets_to_close


class FakeAsset:
    def __init__(self, version: str = "0.3.0", size: int = 1000, notes: str = "修复更新流程") -> None:
        self.Version = version
        self.Size = size
        self.NotesMarkdown = notes


class FakeUpdate:
    def __init__(self, asset: FakeAsset | None = None) -> None:
        self.TargetFullRelease = asset or FakeAsset()


class FakeVelopackManager:
    def __init__(self, *, available=True, pending=None, check_error: Exception | None = None,
                 download_error: Exception | None = None) -> None:
        self.available = available
        self.pending = pending
        self.check_error = check_error
        self.download_error = download_error
        self.update = FakeUpdate()
        self.download_calls = 0
        self.apply_calls = 0

    def get_update_pending_restart(self):
        return self.pending

    def check_for_updates(self):
        if self.check_error:
            raise self.check_error
        return self.update if self.available else None

    def download_updates(self, update, callback):
        self.download_calls += 1
        if self.download_error:
            raise self.download_error
        callback(25)
        callback(100)
        self.pending = update.TargetFullRelease

    def wait_exit_then_apply_updates(self, update, silent=False, restart=True):
        self.apply_calls += 1


def wait_state(manager: UpdateManager, expected: str, timeout: float = 3) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = manager.snapshot()
        if snapshot["state"] == expected:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"状态未变为 {expected}：{manager.snapshot()}")


class VelopackAdapterTests(unittest.TestCase):
    def make_manager(self, root: Path, fake: FakeVelopackManager) -> UpdateManager:
        return UpdateManager(
            root,
            version="0.2.6",
            repository="owner/repo",
            manager_factory=lambda: fake,
        )

    def test_check_download_and_handoff_use_velopack(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeVelopackManager()
            manager = self.make_manager(Path(directory), fake)
            result = manager.check()
            self.assertTrue(result["updateAvailable"])
            snapshot = wait_state(manager, "ready_to_install")
            self.assertEqual(snapshot["latestVersion"], "0.3.0")
            self.assertEqual(snapshot["percent"], 100)
            self.assertTrue(snapshot["canInstall"])
            self.assertEqual(fake.download_calls, 1)

            self.assertTrue(manager.install()["ok"])
            snapshot = wait_state(manager, "waiting_for_exit")
            self.assertTrue(snapshot["readyForExit"])
            self.assertEqual(fake.apply_calls, 1)
            manager.stop()

    def test_current_version_never_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeVelopackManager(available=False)
            manager = self.make_manager(Path(directory), fake)
            result = manager.check(manual=True)
            self.assertFalse(result["updateAvailable"])
            self.assertEqual(manager.snapshot()["state"], "idle")
            self.assertEqual(fake.download_calls, 0)

    def test_pending_package_restores_without_downloading_again(self):
        with tempfile.TemporaryDirectory() as directory:
            asset = FakeAsset(version="0.3.1", size=2048)
            fake = FakeVelopackManager(pending=asset)
            manager = self.make_manager(Path(directory), fake)
            manager.restore()
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["state"], "ready_to_install")
            self.assertEqual(snapshot["latestVersion"], "0.3.1")
            self.assertEqual(snapshot["downloaded"], 2048)
            self.assertEqual(fake.download_calls, 0)

    def test_download_failure_can_retry_through_same_sdk_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeVelopackManager(download_error=RuntimeError("network down"))
            manager = self.make_manager(Path(directory), fake)
            manager.check()
            snapshot = wait_state(manager, "download_failed")
            self.assertIn("network down", snapshot["error"])
            self.assertIn("网络加速器", snapshot["error"])
            self.assertTrue(snapshot["canRetryDownload"])
            fake.download_error = None
            manager.start_download()
            self.assertEqual(wait_state(manager, "ready_to_install")["percent"], 100)
            self.assertEqual(fake.download_calls, 2)

    def test_check_error_does_not_create_fake_update(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeVelopackManager(check_error=RuntimeError("not installed"))
            manager = self.make_manager(Path(directory), fake)
            result = manager.check()
            self.assertFalse(result["ok"])
            self.assertEqual(manager.snapshot()["state"], "idle")
            self.assertIn("网络加速器", manager.snapshot()["error"])
            self.assertFalse(manager.snapshot()["canInstall"])

    def test_check_timeout_discards_late_update_and_allows_retry_after_sdk_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeVelopackManager()
            manager = self.make_manager(Path(directory), fake)
            release = threading.Event()
            entered = threading.Event()

            def blocked_check():
                entered.set()
                release.wait(3)
                return fake.update

            try:
                with patch.object(fake, "check_for_updates", side_effect=blocked_check) as check, patch(
                    "velopack_updater.CHECK_TIMEOUT_SECONDS", 0.05
                ):
                    result = manager.check(manual=True)
                    self.assertTrue(entered.is_set())
                    self.assertFalse(result["ok"])
                    snapshot = manager.snapshot()
                    self.assertEqual(snapshot["state"], "idle")
                    self.assertIn("超时", snapshot["error"])
                    self.assertEqual(snapshot["messages"][0]["title"], "检查更新失败")
                    self.assertFalse(manager.check(manual=True)["ok"])
                    self.assertFalse(manager.start_download()["ok"])
                    self.assertEqual(check.call_count, 1)
                    release.set()
                    manager._check_thread.join(1)
                    self.assertFalse(manager._check_thread.is_alive())
                    self.assertEqual(manager.snapshot()["state"], "idle")
                    self.assertEqual(fake.download_calls, 0)
                    self.assertIsNone(manager._pending)
                fake.available = False
                self.assertFalse(manager.check(manual=True)["updateAvailable"])
                self.assertEqual(manager.snapshot()["error"], "")
            finally:
                release.set()
                manager._check_thread.join(1)

    def test_slow_sdk_initialization_does_not_block_timeout_or_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeVelopackManager()
            manager = self.make_manager(Path(directory), fake)
            release = threading.Event()

            def blocked_factory():
                release.wait(3)
                return fake

            manager._manager_factory = blocked_factory
            started = time.monotonic()
            try:
                with patch("velopack_updater.CHECK_TIMEOUT_SECONDS", 0.05):
                    self.assertFalse(manager.check()["ok"])
                self.assertEqual(manager.snapshot()["state"], "idle")
                self.assertLess(time.monotonic() - started, 1)
            finally:
                release.set()
                manager._check_thread.join(1)

    def test_overlapping_checks_and_download_do_not_borrow_busy_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeVelopackManager(available=False)
            manager = self.make_manager(Path(directory), fake)
            release = threading.Event()
            entered = threading.Event()

            def blocked_check():
                entered.set()
                release.wait(3)
                return None

            with patch.object(fake, "check_for_updates", side_effect=blocked_check) as check:
                worker = threading.Thread(target=manager.check)
                worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(manager.check()["skipped"])
                    self.assertTrue(manager.start_download()["skipped"])
                    self.assertEqual(manager.snapshot()["state"], "checking")
                    self.assertEqual(check.call_count, 1)
                finally:
                    release.set()
                    worker.join(1)
                self.assertEqual(manager.snapshot()["state"], "idle")

    def test_shutdown_waits_for_sdk_handoff_and_closes_browser_last(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeVelopackManager(pending=FakeAsset())
            manager = self.make_manager(Path(directory), fake)
            manager.restore()
            manager.install()
            wait_state(manager, "waiting_for_exit")
            order: list[str] = []
            with patch("velopack_updater.close_assistant_tabs", side_effect=lambda *_args: order.append("browser") or 1):
                result = manager.shutdown_for_update(lambda: order.append("backend"))
            self.assertTrue(result["ok"])
            self.assertEqual(order, ["backend", "browser"])
            manager.stop()

    def test_watchdog_requests_real_service_exit_when_frontend_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(Path(directory), FakeVelopackManager())
            manager._set("waiting_for_exit")
            order: list[str] = []
            manager.set_exit_callback(lambda: order.append("exit"), lambda: order.append("backend"))
            with (
                patch.object(manager._stop, "wait", return_value=False),
                patch("velopack_updater.close_assistant_tabs", side_effect=lambda *_args: order.append("browser") or 1),
            ):
                manager._exit_watchdog()
            self.assertEqual(order, ["backend", "browser", "exit"])

    def test_messages_survive_service_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.make_manager(root, FakeVelopackManager(available=False))
            first.check(manual=True)
            second = self.make_manager(root, FakeVelopackManager(available=False))
            second.restore()
            self.assertEqual(second.snapshot()["messages"][0]["title"], "已是最新版本 v0.2.6")


class BrowserTargetTests(unittest.TestCase):
    def test_only_exact_assistant_origin_is_selected(self):
        targets = [
            {"id": "assistant", "type": "page", "url": "http://127.0.0.1:8765/learning"},
            {"id": "other-port", "type": "page", "url": "http://127.0.0.1:9999/"},
            {"id": "lookalike", "type": "page", "url": "http://127.0.0.1.evil.test:8765/"},
            {"id": "worker", "type": "worker", "url": "http://127.0.0.1:8765/worker"},
        ]
        selected = select_targets_to_close(targets, "http://127.0.0.1:8765")
        self.assertEqual([target["id"] for target in selected], ["assistant"])


if __name__ == "__main__":
    unittest.main()
