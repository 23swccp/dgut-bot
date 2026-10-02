import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from pathlib import Path

from dgutbot.agent.agent_runtime import new_runtime, publish_runtime
from dgutbot.app.backend_commands import configure_agent_registry
from dgutbot.app.web_server import LocalApiHandler, configure_agent_api
import dgutbot.agent.agent_cli as agent_cli


ROOT = Path(__file__).resolve().parents[1]
PYTHON = shutil.which("python") or sys.executable
CLI_COMMAND = [os.environ["DGUTCTL_TEST_EXE"]] if os.environ.get("DGUTCTL_TEST_EXE") else [PYTHON, "-m", "dgutbot.agent.agent_cli"]


class AgentCliTests(unittest.TestCase):
    def run_cli(self, args, *, data_dir, stdin=b""):
        env = os.environ.copy()
        env["YXY_DATA_DIR"] = str(data_dir)
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONIOENCODING"] = "gbk"
        return subprocess.run(
            [*CLI_COMMAND, *args],
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=10,
        )

    def test_service_not_running_is_one_json_document(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_cli(["call", "system.health"], data_dir=folder)
        value = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(value["error"]["code"], "SERVICE_NOT_RUNNING")

    def test_invalid_utf8_and_non_object_are_protocol_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            invalid = self.run_cli(["call", "system.health"], data_dir=folder, stdin=b"\xff")
            non_object = self.run_cli(["call", "system.health"], data_dir=folder, stdin=b"[]")
        self.assertEqual(json.loads(invalid.stdout)["error"]["code"], "INVALID_JSON")
        self.assertEqual(json.loads(non_object.stdout)["error"]["code"], "INVALID_JSON")
        self.assertEqual(invalid.returncode, 2)

    def test_invalid_json_oversized_and_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            for args, raw, code in [
                (["call", "system.health"], b"{", "INVALID_JSON"),
                (["call", "system.health"], b"{\"x\":NaN}", "INVALID_JSON"),
                (["call", "system.health"], b" " * (1048576 + 1), "INPUT_TOO_LARGE"),
                (["unknown"], b"", "TOOL_INPUT_INVALID"),
            ]:
                result = self.run_cli(args, data_dir=folder, stdin=raw)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)["error"]["code"], code)
                self.assertEqual(result.stderr, b"")

    def test_broken_stdout_is_not_written_twice(self):
        with patch("agent_cli.execute", return_value=({"ok": True}, 0)), patch("agent_cli._write_json", side_effect=BrokenPipeError) as write:
            self.assertEqual(agent_cli.run([]), 70)
            write.assert_called_once()

    def test_response_corruption_and_changed_instance(self):
        info = new_runtime(8765)
        def serve(value):
            response = Mock()
            response.read.return_value = json.dumps(value).encode("utf-8")
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            return patch("agent_cli.local_open", return_value=response)
        for value, code in [({"schemaVersion": 2}, "SCHEMA_VERSION_UNSUPPORTED"),
                            ({"schemaVersion": 1, "value": float("nan")}, "PROTOCOL_RESPONSE_INVALID"),
                            ({"schemaVersion": 1}, "PROTOCOL_RESPONSE_INVALID"),
                            ({"schemaVersion": 1, "ok": True, "requestId": "req_test", "tool": "system.version", "result": {}, "error": None,
                              "meta": {"instanceId": "instance_other"}}, "SERVICE_INSTANCE_CHANGED")]:
            with serve(value), self.assertRaises(agent_cli.CliFailure) as caught:
                agent_cli._request(info, "call", "system.version", {}, "req_test")
            self.assertEqual(caught.exception.code, code)

    def test_capabilities_and_call_use_authenticated_service(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            server = ThreadingHTTPServer(("127.0.0.1", 0), LocalApiHandler)
            port = server.server_address[1]
            info = new_runtime(port, pid=os.getpid())
            configure_agent_registry(info.instance_id)
            configure_agent_api(info.auth_token, info.instance_id)
            publish_runtime(info, root)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                capabilities = self.run_cli(["capabilities"], data_dir=root)
                call = self.run_cli(["call", "system.version"], data_dir=root, stdin=b"")
                unknown = self.run_cli(["call", "no.such.tool"], data_dir=root)
                with urlopen(f"http://127.0.0.1:{port}/api/health") as response:
                    self.assertEqual(json.loads(response.read()), {"ok": True})
                unauthenticated = Request(f"http://127.0.0.1:{port}/api/agent/call", data=b"{}", method="POST")
                with self.assertRaises(HTTPError) as denied:
                    urlopen(unauthenticated)
                body = json.loads(denied.exception.read())
                self.assertEqual(body["error"]["code"], "AGENT_AUTH_FAILED")
                self.assertEqual(body["meta"]["instanceId"], "")
                self.assertNotIn("Access-Control-Allow-Origin", denied.exception.headers)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        cap_value = json.loads(capabilities.stdout)
        call_value = json.loads(call.stdout)
        self.assertEqual(capabilities.returncode, 0)
        self.assertTrue(cap_value["ok"])
        self.assertTrue(cap_value["result"]["tools"])
        self.assertEqual(call.returncode, 0)
        self.assertEqual(call_value["result"]["instanceId"], info.instance_id)
        self.assertEqual(call.stderr, b"")
        self.assertIn("优学院助手".encode("utf-8"), call.stdout)
        self.assertEqual(unknown.returncode, 4)
        self.assertEqual(json.loads(unknown.stdout)["error"]["code"], "TOOL_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
