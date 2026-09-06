"""不访问学校接口的基础回归测试。运行：python -m unittest -v test_backend.py"""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dgutbot.domain.yxy_backend import Activity, AppConfig, BrowserApiClient, Course, MonitorState, SignBackend
from dgutbot.app.backend_commands import EventBuffer
from dgutbot.app.browser_paths import registered_browser_paths


class EventBufferTests(unittest.TestCase):
    def test_structured_fields_and_monotonic_sequence(self):
        events = EventBuffer(maxlen=10)
        first = events.emit_event("SESSION_STARTED", "success", "session", "开始")
        second = events.emit_event("PAGE_ENTERED", "info", "navigation", "进入", page={"id": "2"})
        self.assertLess(first["seq"], second["seq"])
        for field in ("seq", "time", "sessionId", "code", "level", "category", "message", "page", "data"):
            self.assertIn(field, first)

    def test_cursor_does_not_consume_or_repeat_events(self):
        events = EventBuffer(maxlen=10)
        one = events.emit_event("A", "info", "test", "one")
        two = events.emit_event("B", "info", "test", "two")
        self.assertEqual([item["seq"] for item in events.get_events(0)["events"]], [one["seq"], two["seq"]])
        self.assertEqual([item["seq"] for item in events.get_events(one["seq"])["events"]], [two["seq"]])
        self.assertEqual(events.get_events(two["seq"])["events"], [])

    def test_ring_buffer_is_bounded_without_resetting_latest_sequence(self):
        events = EventBuffer(maxlen=2)
        for index in range(4):
            events.emit_event("TEST", "info", "test", str(index))
        result = events.get_events(0)
        self.assertEqual([item["seq"] for item in result["events"]], [3, 4])
        self.assertEqual(result["latestSeq"], 4)


class BackendTests(unittest.TestCase):
    def make_backend(self, root: Path) -> SignBackend:
        return SignBackend(lambda _text, _kind: None, root=root)

    def test_config_accepts_known_values_only(self):
        config = AppConfig.from_mapping({"poll_interval": 8, "unexpected": "ignored"}, Path("."))
        self.assertEqual(config.poll_interval, 8)
        self.assertFalse(hasattr(config, "unexpected"))
        self.assertFalse(config.course_quiz_auto_answer)

    def test_quiz_auto_answer_is_session_only_and_starts_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(
                json.dumps({"course_quiz_auto_answer": True}), encoding="utf-8",
            )
            backend = self.make_backend(root)
            self.assertFalse(backend.config.course_quiz_auto_answer)

            backend.update_settings(course_quiz_auto_answer=True)
            self.assertTrue(backend.config.course_quiz_auto_answer)
            saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertFalse(saved["course_quiz_auto_answer"])

    def test_config_normalizes_invalid_values_without_user_interaction(self):
        config = AppConfig.from_mapping(
            {
                "debug_port": "invalid",
                "poll_interval": 0,
                "save_log": "false",
                "lat": float("nan"),
                "lng": -1000,
                "course_playback_rate": 99,
                "course_quiz_auto_answer": "false",
                "course_quiz_choice_enabled": "false",
                "course_ai_model_id": 0,
            },
            Path("."),
        )
        self.assertEqual(config.debug_port, 9222)
        self.assertEqual(config.poll_interval, 2)
        self.assertFalse(config.save_log)
        self.assertEqual(config.lat, 23.0432)
        self.assertEqual(config.lng, -180)
        self.assertEqual(config.course_playback_rate, 16)
        self.assertFalse(config.course_quiz_auto_answer)
        self.assertFalse(config.course_quiz_choice_enabled)
        self.assertEqual(config.course_ai_model_id, 1)
        self.assertTrue(config.course_quiz_judgment_enabled)
        self.assertTrue(config.course_quiz_blank_enabled)

    def test_config_disables_quiz_auto_answer_when_every_question_type_is_off(self):
        config = AppConfig.from_mapping(
            {
                "course_quiz_auto_answer": True,
                "course_quiz_choice_enabled": False,
                "course_quiz_judgment_enabled": False,
                "course_quiz_blank_enabled": False,
            },
            Path("."),
        )
        self.assertFalse(config.course_quiz_auto_answer)

    def test_browser_api_uses_same_origin_page_and_passes_request_as_cdp_argument(self):
        sent = []

        class FakeWebSocket:
            def settimeout(self, _timeout):
                pass

            def send(self, raw):
                sent.append(json.loads(raw))

            def recv(self):
                message = sent[-1]
                if message["method"] == "Runtime.evaluate":
                    result = {"result": {"objectId": "window-1"}}
                else:
                    result = {"result": {"value": {
                        "status": 200, "text": '{"status":200}', "contentType": "application/json",
                    }}}
                return json.dumps({"id": message["id"], "result": result})

            def close(self):
                pass

        discovery = Mock()
        discovery.raise_for_status.return_value = None
        discovery.json.return_value = [
            {
                "id": "lms-1", "type": "page", "url": "https://lms.dgut.edu.cn/home",
                "webSocketDebuggerUrl": "ws://lms-1",
            },
            {
                "id": "application-1", "type": "page", "url": "https://application.dgut.edu.cn/home",
                "webSocketDebuggerUrl": "ws://application-1",
            },
        ]
        client = BrowserApiClient(lambda: 9222, {"Authorization": "secret-token"})
        with patch("yxy_backend.requests.get", return_value=discovery), patch(
            "yxy_backend.create_connection", return_value=FakeWebSocket(),
        ) as connect:
            response = client.request(
                "POST", "https://application.dgut.edu.cn/classroomapi/sign",
                json={"attendanceID": 12},
            )

        self.assertEqual(response.json(), {"status": 200})
        connect.assert_called_once_with("ws://application-1", timeout=15, enable_multithread=True)
        call = next(item for item in sent if item["method"] == "Runtime.callFunctionOn")
        request = call["params"]["arguments"][0]["value"]
        self.assertEqual(request["headers"]["Authorization"], "secret-token")
        self.assertEqual(json.loads(request["body"]), {"attendanceID": 12})
        self.assertNotIn("secret-token", call["params"]["functionDeclaration"])

    def test_browser_api_requires_an_lms_origin_page(self):
        discovery = Mock()
        discovery.raise_for_status.return_value = None
        discovery.json.return_value = [{
            "id": "other", "type": "page", "url": "https://example.com/",
            "webSocketDebuggerUrl": "ws://other",
        }]
        client = BrowserApiClient(lambda: 9222, {}, auto_create=False)
        with patch("yxy_backend.requests.get", return_value=discovery):
            with self.assertRaisesRegex(RuntimeError, "lms.dgut.edu.cn"):
                client.request("GET", "https://lms.dgut.edu.cn/courseapi/courses/students")

    def test_browser_api_never_sends_credentials_to_an_unapproved_origin(self):
        client = BrowserApiClient(lambda: 9222, {"Authorization": "secret-token"})
        with patch("yxy_backend.requests.get") as discovery:
            with self.assertRaisesRegex(ValueError, "学校域名"):
                client.request("POST", "https://example.com/collect", json={"value": 1})
        discovery.assert_not_called()

    def test_browser_api_creates_missing_origin_in_background_and_owns_only_that_target(self):
        empty, version, created, closed = Mock(), Mock(), Mock(), Mock()
        for response in (empty, version, created, closed):
            response.raise_for_status.return_value = None
        empty.json.return_value = []
        version.json.return_value = {"webSocketDebuggerUrl": "ws://browser"}
        created.json.return_value = [{
            "id": "owned-1", "type": "page", "url": "https://lms.dgut.edu.cn/favicon.ico",
            "webSocketDebuggerUrl": "ws://owned-1",
        }]

        class BrowserSocket:
            def __init__(self):
                self.last = None
                self.sent = []

            def send(self, raw):
                self.last = json.loads(raw)
                self.sent.append(self.last)

            def recv(self):
                result = {"targetId": "owned-1"} if self.last["method"] == "Target.createTarget" else {}
                return json.dumps({"id": self.last["id"], "result": result})

            def close(self):
                pass

        responses = iter((empty, version, created, closed))
        socket = BrowserSocket()
        client = BrowserApiClient(lambda: 9222, {"Authorization": "secret-token"})
        with patch("yxy_backend.requests.get", side_effect=lambda *_args, **_kwargs: next(responses)) as get, patch(
            "yxy_backend.create_connection", return_value=socket,
        ) as connect:
            target = client._discover_target("https://lms.dgut.edu.cn")
            client.close()

        self.assertEqual(target["id"], "owned-1")
        connect.assert_called_once_with("ws://browser", timeout=10, enable_multithread=True)
        self.assertEqual([item["method"] for item in socket.sent], ["Storage.setCookies", "Target.createTarget"])
        cookie = socket.sent[0]["params"]["cookies"][0]
        self.assertEqual(cookie["name"], "AUTHORIZATION")
        self.assertEqual(cookie["domain"], ".dgut.edu.cn")
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httpOnly"])
        self.assertIn("/json/close/owned-1", get.call_args_list[-1].args[0])

    def test_course_selection_accepts_one_exact_course_only(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self.make_backend(Path(directory))
            backend.courses = [Course(101, "数据结构"), Course(202, "决策分析")]
            selected = backend.select_course("101")
            self.assertEqual(selected.id, 101)
            self.assertIsNone(backend.select_course("101，决策"))
            backend.clear_selected_course()
            self.assertIsNone(backend.selected_course)

    def test_log_uses_one_append_only_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self.make_backend(root)
            log_path = root / "签到记录.md"
            backend.update_settings(log_path=str(log_path))
            backend._write_sign_log("测试课程", "一键签到", ["HTTP/status: 200"])
            backend._write_sign_log("测试课程", "数字码签到", ["HTTP/status: 201"])
            content = log_path.read_text(encoding="utf-8")
            self.assertEqual(content.count("测试课程"), 2)
            self.assertIn("HTTP/status: 200", content)

    def test_relative_log_path_is_resolved_from_application_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self.make_backend(root)
            backend.update_settings(log_path="logs/签到记录.md")
            backend._write_sign_log("测试课程", "一键签到", ["HTTP/status: exception"])
            self.assertTrue((root / "logs" / "签到记录.md").is_file())

    def test_log_redacts_common_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self.make_backend(root)
            log_path = root / "签到记录.md"
            backend.update_settings(log_path=str(log_path))
            backend._write_sign_log("测试课程", "一键签到", ["Authorization: secret-token", "password=hunter2"])
            content = log_path.read_text(encoding="utf-8")
            self.assertNotIn("secret-token", content)
            self.assertNotIn("hunter2", content)
            self.assertGreaterEqual(content.count("[已隐藏]"), 2)

    def test_config_writes_atomically_without_leaving_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self.make_backend(root)
            backend.update_settings(poll_interval=8)
            saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(AppConfig.from_mapping(saved, root).poll_interval, 8)
            self.assertEqual(list(root.glob(".config.json.*.tmp")), [])

    def test_open_log_creates_file_and_uses_default_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self.make_backend(root)
            with patch("yxy_backend.os.startfile", create=True) as startfile:
                path = backend.open_log_file("logs/签到记录.md")
            self.assertEqual(path, (root / "logs" / "签到记录.md").resolve())
            self.assertTrue(path.is_file())
            startfile.assert_called_once_with(str(path))

    def test_activity_model_keeps_raw_extra_fields(self):
        activity = Activity.from_api({"relationId": 1, "scoreType": 3, "custom": "kept"})
        self.assertEqual(activity.score_type, 3)
        self.assertEqual(activity.raw["custom"], "kept")

    def test_browser_launch_uses_debug_mode_and_opens_requested_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = root / "msedge.exe"
            browser.touch()
            backend = self.make_backend(root)
            backend.config.browser_name = "Microsoft Edge"
            backend.config.browser_path = str(browser)
            url = "http://127.0.0.1:1420"

            with patch.object(backend, "_open_debug_tab", return_value=False), patch("yxy_backend.subprocess.Popen") as popen:
                self.assertTrue(backend.start_browser(url))

            command = popen.call_args.args[0]
            self.assertIn("--remote-debugging-port=9222", command)
            self.assertIn(url, command)

    def test_automatic_login_probe_is_quiet_and_non_blocking_while_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            messages = []
            backend = SignBackend(lambda text, kind: messages.append((text, kind)), root=Path(directory))
            messages.clear()
            with patch.object(backend, "_get_ws_url", return_value=None), patch("yxy_backend.time.sleep") as sleep:
                self.assertFalse(backend.load_session_and_courses(wait_seconds=1, automatic=True))
            sleep.assert_not_called()
            self.assertEqual(messages, [])

    def test_automatic_login_probe_loads_courses_after_cookie_appears(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self.make_backend(Path(directory))
            cookies = [
                {"domain": ".dgut.edu.cn", "name": "AUTHORIZATION", "value": "test-token"},
                {"domain": ".dgut.edu.cn", "name": "userid", "value": "123"},
            ]
            with (
                patch.object(backend, "_get_ws_url", return_value="ws://test"),
                patch.object(backend, "_cookies", return_value=cookies),
                patch.object(backend, "_fetch_courses", return_value=[Course(101, "数据结构")]),
            ):
                self.assertTrue(backend.load_session_and_courses(wait_seconds=1, automatic=True))
            self.assertEqual(backend.token, "test-token")
            self.assertEqual(backend.user_id, 123)
            self.assertEqual([course.id for course in backend.courses], [101])

    def test_browser_login_extracts_user_id_from_encoded_userinfo_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self.make_backend(Path(directory))
            cookies = [
                {"domain": ".dgut.edu.cn", "name": "authorization", "value": "test-token"},
                {"domain": ".dgut.edu.cn", "name": "USERINFO", "value": "%7B%22userId%22%3A456%7D"},
            ]
            with (
                patch.object(backend, "_get_ws_url", return_value="ws://test"),
                patch.object(backend, "_cookies", return_value=cookies),
                patch.object(backend, "_fetch_courses", return_value=[Course(101, "数据结构")]),
            ):
                self.assertTrue(backend.load_session_and_courses(wait_seconds=1, automatic=True))
            self.assertEqual(backend.token, "test-token")
            self.assertEqual(backend.user_id, 456)

    def test_browser_detection_reports_paths_and_prefers_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self.make_backend(Path(directory))
            backend.config.browser_name = ""
            backend.config.browser_path = ""
            checked = []

            def exists(path):
                return str(path).lower().endswith(r"microsoft\edge\application\msedge.exe")

            with patch("yxy_backend.Path.is_file", new=exists):
                path, name = backend.find_browser(progress=checked.append)

            self.assertEqual(name, "Microsoft Edge")
            self.assertTrue(path.lower().endswith(r"microsoft\edge\application\msedge.exe"))
            self.assertEqual(checked[0], path)

    def test_browser_detection_returns_every_installed_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            edge = root / "msedge.exe"
            chrome = root / "chrome.exe"
            edge.touch()
            chrome.touch()
            backend = self.make_backend(root)
            with patch.object(backend, "browser_candidates", return_value=[
                ("Microsoft Edge", [str(edge)]),
                ("Google Chrome", [str(chrome)]),
                ("Brave", [str(root / "missing-brave.exe")]),
            ]):
                detected = backend.detect_browsers()
            self.assertEqual(detected, [
                {"name": "Microsoft Edge", "path": str(edge.resolve())},
                {"name": "Google Chrome", "path": str(chrome.resolve())},
            ])

    def test_explicit_custom_browser_path_takes_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            custom = root / "portable-browser.exe"
            custom.touch()
            backend = self.make_backend(root)
            backend.config.browser_name = "自定义浏览器"
            backend.config.browser_path = str(custom)
            path, name = backend.find_browser()
            self.assertEqual((path, name), (str(custom.resolve()), "自定义浏览器"))

    def test_copied_browser_path_survives_save_restart_and_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = root / "中文 Browser" / "msedge.exe"
            browser.parent.mkdir()
            browser.touch()
            backend = self.make_backend(root)
            with patch.dict(os.environ, {"DGUT_TEST_BROWSER": str(browser.parent)}):
                backend.update_settings(browser_path=' \u202a"%DGUT_TEST_BROWSER%/msedge.exe"\u202c ', browser_name="自定义浏览器")
            restarted = self.make_backend(root)
            self.assertEqual(restarted.config.browser_path, str(browser.resolve()))
            with patch("yxy_backend.subprocess.Popen") as popen:
                self.assertTrue(restarted.start_browser())
            self.assertEqual(popen.call_args.args[0][0], str(browser.resolve()))

    def test_legacy_quoted_path_and_application_folder_are_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = root / "msedge.exe"
            browser.touch()
            (root / "config.json").write_text(json.dumps({"browser_path": f'"{browser}"'}), encoding="utf-8")
            backend = self.make_backend(root)
            self.assertEqual(backend.find_browser()[0], str(browser.resolve()))
            backend.update_settings(browser_path=str(root))
            self.assertEqual(self.make_backend(root).find_browser()[0], str(browser.resolve()))

    def test_invalid_browser_path_does_not_overwrite_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self.make_backend(root)
            backend.update_settings(poll_interval=8)
            previous = (root / "config.json").read_bytes()
            for path in (root / "missing.exe", root / "note.txt", root):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "浏览器路径无效"):
                    backend.update_settings(browser_path=str(path), poll_interval=12)
                self.assertEqual(backend.config.poll_interval, 8)
                self.assertEqual((root / "config.json").read_bytes(), previous)

    def test_detects_default_edge_without_program_files_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self.make_backend(Path(directory))
            expected = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
            with (
                patch.dict(os.environ, {"SystemDrive": "C:"}, clear=True),
                patch("browser_paths.registered_browser_paths", return_value=[]),
                patch("yxy_backend.Path.is_file", new=lambda path: path == expected),
            ):
                self.assertEqual(backend.find_browser(), (str(expected), "Microsoft Edge"))
                self.assertEqual(backend.detect_browsers(), [{"name": "Microsoft Edge", "path": str(expected.resolve())}])

    def test_detects_registered_custom_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = root / "公司 软件" / "msedge.exe"
            browser.parent.mkdir()
            browser.touch()
            backend = self.make_backend(root)
            with (
                patch("browser_paths.registered_browser_paths", side_effect=lambda exe: [str(browser)] if exe == "msedge.exe" else []),
                patch("yxy_backend.Path.is_file", new=lambda path: path == browser),
            ):
                self.assertEqual(backend.find_browser(), (str(browser), "Microsoft Edge"))
                self.assertEqual(backend.detect_browsers(), [{"name": "Microsoft Edge", "path": str(browser.resolve())}])

    @unittest.skipUnless(os.name == "nt", "Windows App Paths")
    def test_registry_search_reads_both_hives_and_views_after_missing_keys(self):
        import winreg
        from unittest.mock import MagicMock
        key = MagicMock()
        with (
            patch("winreg.OpenKey", side_effect=[FileNotFoundError(), PermissionError(), FileNotFoundError(), key]) as opened,
            patch("winreg.QueryValueEx", return_value=('"D:\\公司 软件\\msedge.exe"', winreg.REG_SZ)),
        ):
            self.assertEqual(registered_browser_paths("msedge.exe"), [r"D:\公司 软件\msedge.exe"])
        self.assertEqual(opened.call_count, 4)
        self.assertEqual({call.args[0] for call in opened.call_args_list}, {winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE})
        self.assertEqual({call.args[3] for call in opened.call_args_list}, {winreg.KEY_READ | winreg.KEY_WOW64_64KEY, winreg.KEY_READ | winreg.KEY_WOW64_32KEY})

    def test_optional_account_login_is_saved_separately_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self.make_backend(root)
            self.assertFalse(backend.update_account_login("", "", True))
            self.assertTrue(backend.update_account_login("20260001", "test-password", True))
            self.assertEqual(backend.account_login_status(), {"enabled": True, "username": "20260001", "has_password": True})
            self.assertTrue((root / "account.json").is_file())
            self.assertTrue(backend.update_account_login("", "", True))
            self.assertTrue(backend.update_account_login("", "", False))
            self.assertFalse((root / "account.json").exists())

    def test_monitor_is_single_instance_and_returns_to_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self.make_backend(Path(directory))
            backend.selected_course = Course(101, "数据结构")
            entered = threading.Event()

            def poll_once(_checked):
                entered.set()
                backend.stop_event.wait(1)

            with patch.object(backend, "_poll_once", side_effect=poll_once) as poll:
                self.assertTrue(backend.start_monitor())
                self.assertTrue(entered.wait(1))
                first_thread = backend.monitor_thread
                self.assertFalse(backend.start_monitor())
                self.assertIs(backend.monitor_thread, first_thread)
                backend.stop_monitor()
                first_thread.join(1)

            self.assertEqual(poll.call_count, 1)
            self.assertEqual(backend.monitor_state, MonitorState.IDLE)
            self.assertIsNone(backend.monitor_thread)


if __name__ == "__main__":
    unittest.main()
