"""浏览器版启动器的本地回归测试。"""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import dgutbot.app.browser_launcher as browser_launcher
from dgutbot.app.app_paths import data_root, frontend_dist, is_frozen, resource_root
from dgutbot.app.browser_launcher import choose_available_port, choose_frontend_port, service_command
from dgutbot.app.web_server import (
    CLIENT_CLOSED_EVENT, LocalApiHandler, SHUTDOWN_EVENT, allowed_cors_origin,
    client_last_seen, reset_client_state,
)


class FrozenPathTests(unittest.TestCase):
    """冻结（PyInstaller onedir）模式下的服务启动与路径解析。"""

    def test_frozen_service_command_uses_current_executable(self):
        exe = r"C:\Release\dgut-bot.exe"
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", exe):
            self.assertTrue(is_frozen())
            self.assertEqual(
                service_command(8766, use_static=True, api_port=8766),
                [exe, "--service", "8766", "--api-port", "8766", "--static"],
            )

    def test_dev_service_command_runs_launcher_script_with_interpreter(self):
        self.assertFalse(is_frozen())
        command = service_command(1420, use_static=False, api_port=8766)
        self.assertEqual(command[3:], ["--service", "1420", "--api-port", "8766"])
        self.assertNotIn("--static", command)
        self.assertEqual(command[1:3], ["-m", "dgutbot.app.browser_launcher"])

    def test_frozen_mode_always_serves_static_frontend(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertTrue(browser_launcher.static_frontend_available())

    def test_frontend_dist_resolves_under_resource_root(self):
        self.assertEqual(frontend_dist(), resource_root() / "web" / "dist")

    def test_frozen_data_uses_stable_local_appdata(self):
        with patch.object(sys, "frozen", True, create=True), \
                patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\tester\AppData\Local"}, clear=False), \
                patch.dict(os.environ, {"YXY_DATA_DIR": ""}, clear=False):
            self.assertEqual(data_root(), Path(r"C:\Users\tester\AppData\Local\DgutBot\data"))

    def test_dev_resource_root_is_source_directory(self):
        self.assertEqual(resource_root(), Path(browser_launcher.__file__).resolve().parents[3])


class BrowserLauncherTests(unittest.TestCase):
    def test_explicit_frontend_close_exits_even_with_active_tasks(self):
        for browser_closed in (False, True):
            with self.subTest(browser_closed=browser_closed), ExitStack() as stack:
                for name in ('NamedMutex', 'new_runtime', 'configure_agent_registry', 'configure_agent_api',
                             'publish_runtime', 'remove_runtime', 'reset_client_state', 'stop_process',
                             'threading.Thread', 'update_manager', 'ThreadingHTTPServer'):
                    stack.enter_context(patch('browser_launcher.' + name))
                stop_tasks = stack.enter_context(patch('browser_launcher.stop_backend_tasks'))
                shutdown = stack.enter_context(patch('browser_launcher.SHUTDOWN_EVENT'))
                shutdown.is_set.return_value = False
                closed = stack.enter_context(patch('browser_launcher.CLIENT_CLOSED_EVENT'))
                closed.is_set.return_value = True
                watch = stack.enter_context(patch('browser_launcher.BrowserLifetime')).return_value
                watch.closed.return_value = browser_closed
                watch.present = None
                stack.enter_context(patch('browser_launcher.time.monotonic', side_effect=[10, 11]))
                stack.enter_context(patch('browser_launcher.client_last_seen', return_value=0))
                active = stack.enter_context(patch('browser_launcher.AGENT_SERVICE.active', return_value=True))
                self.assertEqual(browser_launcher.run_background_service(8765, True), 0)
                shutdown.wait.assert_not_called()
                active.assert_not_called()
                stop_tasks.assert_called_once()

    def setUp(self):
        # 离线回归不弹出会等待人工操作的系统窗口。
        picker = patch("browser_launcher.choose_browser_file", return_value="")
        self.browser_picker = picker.start()
        self.addCleanup(picker.stop)

    def frozen_launch_mocks(self, stack):
        stack.enter_context(patch.object(sys, "argv", ["dgut-bot.exe"]))
        stack.enter_context(patch("browser_launcher.is_frozen", return_value=True))
        stack.enter_context(patch("browser_launcher.prepare_launcher_console"))
        stack.enter_context(patch("browser_launcher.app_mutex_exists", return_value=False))
        stack.enter_context(patch("browser_launcher.choose_available_port", return_value=8765))
        stack.enter_context(patch("browser_launcher.wait_for_frontend"))
        service = stack.enter_context(patch("browser_launcher.start_background_service"))
        browser = stack.enter_context(patch("browser_launcher.backend.start_browser", return_value=True))
        default = stack.enter_context(patch("browser_launcher.os.startfile", create=True))
        return service, browser, default

    def test_frozen_missing_browser_cancels_without_starting_web_or_default_browser(self):
        with ExitStack() as stack:
            service, browser, default = self.frozen_launch_mocks(stack)
            stack.enter_context(patch("browser_launcher.backend.find_browser", return_value=(None, None)))
            stack.enter_context(patch("builtins.input", side_effect=EOFError))
            self.assertEqual(browser_launcher.main(), 0)
            service.assert_not_called()
            browser.assert_not_called()
            default.assert_not_called()

    def test_frozen_manual_setup_finishes_before_web_service_starts(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            service, browser, default = self.frozen_launch_mocks(stack)
            stack.enter_context(patch("browser_launcher.backend.find_browser", return_value=(None, None)))
            saved = stack.enter_context(patch("browser_launcher.backend.update_settings"))
            executable = Path(directory) / "msedge.exe"
            executable.touch()
            values = iter([str(Path(directory) / "missing.exe"), f'"{executable}"'])

            def read_path(_prompt):
                service.assert_not_called()
                browser.assert_not_called()
                return next(values)

            stack.enter_context(patch("builtins.input", side_effect=read_path))
            self.assertEqual(browser_launcher.main(), 0)
            saved.assert_called_once_with(browser_name="Microsoft Edge", browser_path=str(executable.resolve()))
            service.assert_called_once_with(8765, True, 8765)
            browser.assert_called_once_with("http://127.0.0.1:8765/(不要删favicon.ico页)")
            default.assert_not_called()

    def test_frozen_detected_browser_skips_manual_setup(self):
        with ExitStack() as stack:
            service, browser, default = self.frozen_launch_mocks(stack)
            stack.enter_context(patch("browser_launcher.open_frontend", return_value="debug"))
            prompt = stack.enter_context(patch("browser_launcher.prompt_for_browser"))
            self.assertEqual(browser_launcher.main(), 0)
            prompt.assert_not_called()
            service.assert_called_once_with(8765, True, 8765)
            browser.assert_called_once()
            default.assert_not_called()

    def test_background_service_does_not_create_interactive_console(self):
        with (
            patch.object(sys, "argv", ["dgut-bot.exe", "--service", "8765", "--static"]),
            patch("browser_launcher.prepare_launcher_console") as console,
            patch("browser_launcher.run_background_service", return_value=0) as service,
        ):
            self.assertEqual(browser_launcher.main(), 0)
            console.assert_not_called()
            service.assert_called_once_with(8765, True, 8765)

    @unittest.skipUnless(sys.platform == "win32", "Windows console")
    def test_terminal_encoding_supports_utf8_and_gbk_fallback(self):
        for ready, expected in ((True, "utf-8"), (False, "gbk")):
            with (
                self.subTest(encoding=expected),
                patch.dict(os.environ, {"YXY_CONSOLE_ENCODING": "utf-8"}),
                patch("ctypes.windll.kernel32") as kernel,
                patch.object(sys, "stdin") as stdin,
                patch.object(sys, "stdout") as stdout,
            ):
                kernel.SetConsoleOutputCP.return_value = ready
                kernel.SetConsoleCP.return_value = ready
                self.assertEqual(browser_launcher.configure_console_encoding(), expected)
                stdin.reconfigure.assert_called_once_with(encoding=expected, errors="replace")
                stdout.reconfigure.assert_called_once_with(encoding=expected, errors="replace")

    @unittest.skipUnless(sys.platform == "win32", "Windows console")
    def test_windowed_launcher_allocates_console_and_binds_input_output(self):
        with (
            patch("browser_launcher.is_frozen", return_value=True),
            patch("ctypes.windll.kernel32") as kernel,
            patch("builtins.open") as opened,
            patch.object(sys, "stdin"), patch.object(sys, "stdout"), patch.object(sys, "stderr"),
            patch("browser_launcher.configure_console_encoding") as encoding,
        ):
            kernel.GetConsoleWindow.return_value = 0
            kernel.AllocConsole.return_value = 1
            browser_launcher.prepare_launcher_console()
            kernel.AllocConsole.assert_called_once()
            self.assertEqual([call.args[0] for call in opened.call_args_list], ["CONIN$", "CONOUT$", "CONOUT$"])
            encoding.assert_called_once()

    def test_missing_heartbeat_stops_service_after_background_grace_period(self):
        self.assertFalse(browser_launcher.client_connection_expired(False, 0, now=100))
        timeout = browser_launcher.CLIENT_HEARTBEAT_TIMEOUT
        self.assertFalse(browser_launcher.client_connection_expired(True, 100 - timeout + 0.1, now=100))
        self.assertTrue(browser_launcher.client_connection_expired(True, 100 - timeout, now=100))

    @patch("browser_launcher.time.sleep")
    @patch("builtins.print")
    def test_duplicate_instance_notice_stays_visible(self, output, sleep):
        browser_launcher.show_already_running_notice()
        self.assertIn("请勿重复打开", output.call_args_list[0].args[0])
        self.assertIn("4 秒后", output.call_args_list[1].args[0])
        sleep.assert_called_once_with(browser_launcher.DUPLICATE_NOTICE_SECONDS)

    def test_cors_origin_requires_exact_loopback_host_and_allowed_port(self):
        self.assertEqual(allowed_cors_origin("http://localhost:1420"), "http://localhost:1420")
        self.assertEqual(allowed_cors_origin("http://127.0.0.1:8765"), "http://127.0.0.1:8765")
        self.assertEqual(allowed_cors_origin("http://127.0.0.1:8766"), "http://127.0.0.1:8766")
        self.assertEqual(allowed_cors_origin("http://127.0.0.1.evil.example:1420"), "http://127.0.0.1:1420")
        self.assertEqual(allowed_cors_origin("http://localhost.evil.example:1420"), "http://127.0.0.1:1420")
        self.assertEqual(allowed_cors_origin("http://localhost:9999"), "http://127.0.0.1:1420")

    @patch("builtins.print")
    def test_startup_help_precedes_two_separator_lines(self, output):
        browser_launcher.print_startup_help()
        lines = [call.args[0] for call in output.call_args_list]
        self.assertIn("Edge → Chrome → 其他 Chromium", lines[0])
        self.assertEqual(lines[-2], "-" * 72)
        self.assertEqual(lines[-1], "-" * 72)

    @patch("browser_launcher.backend.find_browser", return_value=(None, None))
    def test_frontend_requests_terminal_setup_when_detection_fails(self, _find_browser):
        self.assertEqual(browser_launcher.open_frontend("http://127.0.0.1:1420"), "manual")

    @patch("browser_launcher.backend.start_browser", return_value=True)
    @patch("browser_launcher.backend.update_settings")
    @patch("browser_launcher.backend.find_browser", return_value=(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "Microsoft Edge"))
    def test_frontend_reports_and_saves_detected_browser(self, _find_browser, update_settings, start_browser):
        self.assertEqual(browser_launcher.open_frontend("http://127.0.0.1:1420"), "debug")
        update_settings.assert_called_once_with(
            browser_name="Microsoft Edge",
            browser_path=r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        )
        start_browser.assert_called_once_with("http://127.0.0.1:1420")

    @patch("browser_launcher.backend.start_browser")
    @patch("browser_launcher.backend.update_settings")
    @patch("browser_launcher.backend.find_browser", return_value=(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "Microsoft Edge"))
    def test_browser_can_be_prepared_without_opening_a_page(self, _find_browser, _update_settings, start_browser):
        self.assertEqual(browser_launcher.open_frontend(""), "debug")
        start_browser.assert_not_called()

    @patch("browser_launcher.backend.update_settings")
    def test_terminal_accepts_a_quoted_chrome_executable(self, update_settings):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Chrome Folder" / "chrome.exe"
            executable.parent.mkdir()
            executable.touch()
            self.assertTrue(browser_launcher.configure_browser_path(f'"{executable}"'))
        update_settings.assert_called_once_with(browser_name="Google Chrome", browser_path=str(executable.resolve()))

    @patch("browser_launcher.backend.update_settings")
    def test_terminal_accepts_a_portable_chromium_executable(self, update_settings):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "PortableBrowser" / "browser.exe"
            executable.parent.mkdir()
            executable.touch()
            self.assertTrue(browser_launcher.configure_browser_path(str(executable)))
        update_settings.assert_called_once_with(browser_name="自定义浏览器", browser_path=str(executable.resolve()))

    @patch("browser_launcher.backend.update_settings")
    def test_terminal_setup_only_accepts_a_browser_executable_path(self, update_settings):
        self.assertFalse(browser_launcher.configure_browser_path("edge"))
        self.assertFalse(browser_launcher.configure_browser_path(r"Z:\\Browser Folder\\browser.exe"))
        update_settings.assert_not_called()

    @patch("browser_launcher.configure_browser_path", side_effect=[False, True])
    @patch("builtins.input", side_effect=['"Z:\\Browser Folder\\browser.exe"', '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"'])
    def test_terminal_prompt_accepts_quoted_paths_and_retries(self, _input, configure):
        self.assertTrue(browser_launcher.prompt_for_browser("http://127.0.0.1:1420"))
        self.assertEqual(configure.call_args_list[0].args[0], '"Z:\\Browser Folder\\browser.exe"')
        self.assertEqual(configure.call_args_list[1].args[0], '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"')

    def test_picker_selection_is_saved_before_starting_web(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            service, browser, default = self.frozen_launch_mocks(stack)
            stack.enter_context(patch("browser_launcher.backend.find_browser", return_value=(None, None)))
            saved = stack.enter_context(patch("browser_launcher.backend.update_settings"))
            executable = Path(directory) / "中文 Edge App" / "msedge.exe"
            executable.parent.mkdir()
            executable.touch()

            def select():
                service.assert_not_called()
                browser.assert_not_called()
                return str(executable)

            self.browser_picker.side_effect = select
            typed = stack.enter_context(patch("builtins.input"))
            self.assertEqual(browser_launcher.main(), 0)
            saved.assert_called_once_with(browser_name="Microsoft Edge", browser_path=str(executable.resolve()))
            typed.assert_not_called()
            service.assert_called_once()
            browser.assert_called_once()
            default.assert_not_called()

    def test_cancelled_picker_can_be_reopened_or_exit_without_changing_settings(self):
        with patch("builtins.input", side_effect=["", "q"]), patch("browser_launcher.backend.update_settings") as saved:
            self.assertFalse(browser_launcher.prompt_for_browser(""))
            self.assertEqual(self.browser_picker.call_count, 2)
            saved.assert_not_called()

    def test_picker_error_allows_manual_path_entry(self):
        self.browser_picker.side_effect = OSError("dialog unavailable")
        with patch("builtins.input", return_value="manual.exe"), patch("browser_launcher.configure_browser_path", return_value=True) as configure:
            self.assertTrue(browser_launcher.prompt_for_browser(""))
            configure.assert_called_once_with("manual.exe")

    def test_check_mode_reports_missing_browser_without_opening_picker(self):
        with ExitStack() as stack:
            service, browser, default = self.frozen_launch_mocks(stack)
            stack.enter_context(patch.object(sys, "argv", ["dgut-bot.exe", "--check"]))
            stack.enter_context(patch("browser_launcher.backend.find_browser", return_value=(None, None)))
            self.assertEqual(browser_launcher.main(), 1)
            self.browser_picker.assert_not_called()
            service.assert_not_called()
            browser.assert_not_called()
            default.assert_not_called()

    def test_chooses_another_port_when_first_port_is_busy(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            occupied_port = listener.getsockname()[1]
            selected = choose_frontend_port(occupied_port, attempts=20)
        self.assertNotEqual(selected, occupied_port)

    def test_service_port_moves_when_8765_is_busy(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            occupied_port = listener.getsockname()[1]
            selected = choose_available_port(occupied_port, attempts=20)
        self.assertNotEqual(selected, occupied_port)
        self.assertLess(selected, occupied_port + 20)

    def test_shutdown_command_sets_launcher_event_after_reply(self):
        SHUTDOWN_EVENT.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalApiHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        request = Request(
            f"http://127.0.0.1:{port}/api/command",
            data=json.dumps({"command": "shutdown_app", "payload": {}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=3) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.assertTrue(result["ok"])
            self.assertTrue(SHUTDOWN_EVENT.wait(1))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            SHUTDOWN_EVENT.clear()

    def test_heartbeat_recovers_from_page_refresh_close_signal(self):
        reset_client_state()
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalApiHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            closed = Request(f"http://127.0.0.1:{port}/api/client-closed", data=b"", method="POST")
            with urlopen(closed, timeout=3):
                pass
            self.assertTrue(CLIENT_CLOSED_EVENT.is_set())

            heartbeat = Request(f"http://127.0.0.1:{port}/api/heartbeat", data=b"", method="POST")
            with urlopen(heartbeat, timeout=3):
                pass
            self.assertFalse(CLIENT_CLOSED_EVENT.is_set())
            self.assertGreater(client_last_seen(), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            reset_client_state()


if __name__ == "__main__":
    unittest.main()
