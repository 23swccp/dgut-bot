"""浏览器安装目录探测，使用临时目录而不启动真实浏览器。"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dgutbot.app.browser_paths import resolve_browser_path, scan_browser_directory
from dgutbot.domain.yxy_backend import SignBackend


class BrowserDiscoveryTests(unittest.TestCase):
    def test_extended_browsers_use_shared_candidates_and_keep_edge_chrome_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            locations = {
                "Microsoft Edge": "Programs/Microsoft/Edge/App/msedge.exe",
                "Google Chrome": "Programs/Google/Chrome/Application/chrome.exe",
                "360 极速浏览器": "Programs/360Chrome/Chrome/Application/360chrome.exe",
                "QQ 浏览器": "Programs/Tencent/QQBrowser/App/QQBrowser.exe",
                "搜狗高速浏览器": "Programs/SogouExplorer/13.0/Runtime/SogouExplorer.exe",
                "360 安全浏览器": "Roaming/360se6/Chrome-bin/360se.exe",
                "Opera GX": "Local/Programs/Opera GX/launcher.exe",
            }
            for relative in locations.values():
                browser = root / relative
                browser.parent.mkdir(parents=True)
                browser.touch()
            backend = SignBackend(lambda *_: None, root=root)
            with (
                patch.dict(os.environ, {
                    "PROGRAMFILES": str(root / "Programs"),
                    "LOCALAPPDATA": str(root / "Local"),
                    "APPDATA": str(root / "Roaming"),
                }),
                patch("browser_paths.registered_browser_paths", return_value=[]),
            ):
                candidates = [(name, [p for p in paths if Path(p).is_relative_to(root)])
                              for name, paths in backend.browser_candidates()]
                with patch.object(backend, "browser_candidates", return_value=candidates):
                    self.assertEqual(backend.find_browser(), (
                        str(root / locations["Microsoft Edge"]), "Microsoft Edge",
                    ))
                    detected = backend.detect_browsers()
            self.assertEqual([item["name"] for item in detected[:2]], ["Microsoft Edge", "Google Chrome"])
            self.assertEqual({item["name"]: item["path"] for item in detected}, {
                name: str(root / relative) for name, relative in locations.items()
            })
            for name, relative in locations.items():
                with self.subTest(browser=name):
                    self.assertEqual(resolve_browser_path(str((root / relative).parent)), str(root / relative))

    def test_app_directory_is_found_by_terminal_and_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser = root / "Programs/Microsoft/Edge/App/msedge.exe"
            browser.parent.mkdir(parents=True)
            browser.touch()
            backend = SignBackend(lambda *_: None, root=root)
            with (
                patch.dict(os.environ, {"PROGRAMFILES": str(root / "Programs")}),
                patch("browser_paths.registered_browser_paths", return_value=[]),
            ):
                # 保留真实的候选生成过程，但隔离本机的其他浏览器。
                candidates = [(name, [p for p in paths if p.startswith(str(root))])
                              for name, paths in backend.browser_candidates()]
                with patch.object(backend, "browser_candidates", return_value=candidates):
                    self.assertEqual(backend.find_browser(), (str(browser), "Microsoft Edge"))
                    self.assertEqual(backend.detect_browsers(), [
                        {"name": "Microsoft Edge", "path": str(browser.resolve())},
                    ])

    def test_unknown_subdirectory_is_scanned_and_manual_folder_survives_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "公司浏览器"
            browser = install / "Portable/Runtime/msedge.exe"
            browser.parent.mkdir(parents=True)
            browser.touch()
            backend = SignBackend(lambda *_: None, root=root)
            with patch.object(backend, "browser_candidates", return_value=[
                ("Microsoft Edge", [str(install / "Application/msedge.exe")]),
            ]):
                self.assertEqual(backend.find_browser(), (str(browser.resolve()), "Microsoft Edge"))
                self.assertEqual(backend.detect_browsers()[0]["path"], str(browser.resolve()))
            backend.update_settings(browser_path=f'"{install}"', browser_name="Microsoft Edge")
            restarted = SignBackend(lambda *_: None, root=root)
            self.assertEqual(restarted.find_browser()[0], str(browser.resolve()))
            self.assertIn("找到 Microsoft Edge", (root / "browser-detection.log").read_text(encoding="utf-8"))

    def test_timeout_is_reported_instead_of_claiming_scan_completed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = SignBackend(lambda *_: None, root=root)
            with (
                patch.object(backend, "browser_candidates", return_value=[("Microsoft Edge", [str(root / "msedge.exe")])]),
                patch("yxy_backend.time.monotonic", side_effect=[0, 11]),
            ):
                self.assertEqual(backend.find_browser(), (None, None))
            log = (root / "browser-detection.log").read_text(encoding="utf-8")
            self.assertIn("检测超时", log)
            self.assertNotIn("检测结束", log)

    def test_directory_depth_and_entry_limits_are_respected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser = root / "a/b/c/d/msedge.exe"
            browser.parent.mkdir(parents=True)
            browser.touch()
            self.assertEqual(resolve_browser_path(str(root)), "")
            reports = []
            self.assertEqual(list(scan_browser_directory(
                root, ("msedge.exe",), time.monotonic() + 10, reports.append, max_entries=1,
            )), [])
            self.assertTrue(any("数量上限" in message for message in reports))

    def test_permission_error_does_not_prevent_checking_other_installations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blocked = root / "blocked"
            blocked.mkdir()
            browser = root / "allowed/nested/msedge.exe"
            browser.parent.mkdir(parents=True)
            browser.touch()
            original = os.scandir

            def scandir(path):
                if Path(path) == blocked:
                    raise PermissionError("test denied")
                return original(path)

            reports = []
            with patch("browser_paths.os.scandir", side_effect=scandir):
                self.assertEqual(list(scan_browser_directory(
                    root, ("msedge.exe",), time.monotonic() + 10, reports.append,
                )), [str(browser.resolve())])
            self.assertTrue(any("PermissionError" in message for message in reports))

    def test_windows_junction_is_not_followed(self):
        from types import SimpleNamespace

        with patch("browser_paths.Path.lstat", return_value=SimpleNamespace(st_mode=0, st_file_attributes=0x400)):
            with patch("browser_paths.os.scandir") as scandir:
                reports = []
                self.assertEqual(list(scan_browser_directory(
                    Path("junction"), ("msedge.exe",), time.monotonic() + 10, reports.append,
                )), [])
                scandir.assert_not_called()
                self.assertIn("跳过链接目录", reports[0])
