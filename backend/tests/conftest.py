"""All offline tests import the service against an isolated, empty data directory."""

import os
import importlib
import sys
import tempfile

_MODULE_ALIASES = {
    "agent_cli": "dgutbot.agent.agent_cli", "agent_leases": "dgutbot.agent.agent_leases",
    "agent_protocol": "dgutbot.agent.agent_protocol", "agent_runtime": "dgutbot.agent.agent_runtime",
    "agent_tasks": "dgutbot.agent.agent_tasks", "agent_tools": "dgutbot.agent.agent_tools",
    "browser_launcher": "dgutbot.app.browser_launcher", "browser_lifetime": "dgutbot.app.browser_lifetime",
    "browser_paths": "dgutbot.app.browser_paths", "backend_commands": "dgutbot.app.backend_commands",
    "web_server": "dgutbot.app.web_server", "velopack_updater": "dgutbot.app.velopack_updater",
    "yxy_backend": "dgutbot.domain.yxy_backend", "yxy_course": "dgutbot.course.yxy_course",
    "yxy_quiz": "dgutbot.course.yxy_quiz", "course_dialogs": "dgutbot.course.course_dialogs",
    "course_slides": "dgutbot.course.course_slides", "quiz_requests": "dgutbot.course.quiz_requests",
}
for _old_name, _new_name in _MODULE_ALIASES.items():
    sys.modules.setdefault(_old_name, importlib.import_module(_new_name))

_original_data_dir = os.environ.get("YXY_DATA_DIR")
_test_data = tempfile.TemporaryDirectory(prefix="dgut-offline-tests-")
os.environ["YXY_DATA_DIR"] = _test_data.name


def pytest_unconfigure(config):
    if _original_data_dir is None:
        os.environ.pop("YXY_DATA_DIR", None)
    else:
        os.environ["YXY_DATA_DIR"] = _original_data_dir
    _test_data.cleanup()
