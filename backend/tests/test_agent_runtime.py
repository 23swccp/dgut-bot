import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dgutbot.agent.agent_protocol import AgentError
from dgutbot.agent.agent_runtime import load_runtime, new_runtime, publish_runtime, remove_runtime, pid_is_running


class AgentRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows process-query regression")
    def test_windows_probe_never_uses_os_kill(self):
        with patch("agent_runtime.os.kill", side_effect=AssertionError("Unsafe process probe")):
            self.assertTrue(pid_is_running(os.getppid()))

    def test_publish_load_and_instance_safe_remove(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            info = new_runtime(8765)
            path = publish_runtime(info, root)
            self.assertEqual(load_runtime(root).instance_id, info.instance_id)
            remove_runtime("instance_other", root)
            self.assertTrue(path.exists())
            remove_runtime(info.instance_id, root)
            self.assertFalse(path.exists())

    def test_invalid_runtime_is_structured(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "agent-runtime.json"
            path.write_text(json.dumps({"pid": 1}), encoding="utf-8")
            with self.assertRaises(AgentError) as raised:
                load_runtime(Path(folder))
            self.assertEqual(raised.exception.code, "RUNTIME_FILE_INVALID")


if __name__ == "__main__":
    unittest.main()
