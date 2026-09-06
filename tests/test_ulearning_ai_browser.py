from unittest.mock import patch

import pytest

from dgutbot.experimental.ulearning_ai import UlearningAiError
from dgutbot.experimental.ulearning_ai_browser import (
    BrowserAiAccess,
    _NoCourseAiAssistant,
    _decode_ai_frame,
    _decode_workbench_frame,
    _is_dgut_domain,
    discover_cached_access,
    discover_browser_access,
)


def test_decode_ai_frame_uses_path_id_and_required_query_values():
    assert _decode_ai_frame(
        "https://aijx.dgut.edu.cn/ai/1234?auth=memory-only&courseId=654321&theme=blue"
    ) == (
        "1234",
        "654321",
        "memory-only",
        "https://aijx.dgut.edu.cn/ai/1234?auth=memory-only&courseId=654321&theme=blue",
    )
    assert _decode_ai_frame("https://aijx.dgut.edu.cn/ai/agentPreview?path=x") is None


def test_decode_workbench_frame_uses_pre_conversation_context():
    assert _decode_workbench_frame(
        "https://aijx.dgut.edu.cn/ai/Workbench?auth=memory-only&ocId=654321&theme=blue"
    ) == (
        "654321", "memory-only",
        "https://aijx.dgut.edu.cn/ai/Workbench?auth=memory-only&ocId=654321&theme=blue",
    )
    assert _decode_workbench_frame("https://aijx.dgut.edu.cn/ai/Workbench?ocId=1") is None


def test_access_repr_redacts_authorization_and_cookies():
    access = BrowserAiAccess.__new__(BrowserAiAccess)
    object.__setattr__(access, "context", None)
    object.__setattr__(access, "authorization", "very-secret")
    object.__setattr__(access, "referer", "https://example.test/?auth=referer-secret")
    object.__setattr__(access, "user_agent", "secret-agent")
    object.__setattr__(access, "cookies", ({"name": "AUTH", "value": "cookie-secret"},))
    shown = repr(access)
    assert "very-secret" not in shown
    assert "cookie-secret" not in shown
    assert "referer-secret" not in shown
    assert "secret-agent" not in shown


def test_dgut_cookie_scope_rejects_lookalike_domains():
    assert _is_dgut_domain(".aijx.dgut.edu.cn")
    assert not _is_dgut_domain("dgut.edu.cn.example.test")


def test_discovery_does_not_guess_between_multiple_workbenches():
    targets = [
        {"id": "one", "type": "page", "url": "https://lms.dgut.edu.cn/#/course/workbench"},
        {"id": "two", "type": "page", "url": "https://lms.dgut.edu.cn/#/course/workbench"},
    ]
    with patch("dgutbot.experimental.ulearning_ai_browser._targets", return_value=targets):
        with pytest.raises(UlearningAiError, match="Multiple"):
            discover_browser_access()


def test_discovery_builds_context_from_workbench_without_entering_conversation():
    targets = [{
        "id": "one", "type": "page", "url": "https://lms.dgut.edu.cn/#/course/workbench",
        "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/one",
    }]

    class Connection:
        def __init__(self, _url):
            pass

        def close(self):
            pass

        def call(self, method, _params=None):
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {}, "childFrames": [{"frame": {
                    "url": "https://aijx.dgut.edu.cn/ai/Workbench?auth=memory-only&ocId=654321&theme=blue",
                }}]}}
            if method == "Network.getAllCookies":
                return {"cookies": []}
            if method == "Browser.getVersion":
                return {"userAgent": "test-agent"}
            return {}

    with patch("dgutbot.experimental.ulearning_ai_browser._targets", return_value=targets), \
            patch("dgutbot.experimental.ulearning_ai_browser._CdpConnection", Connection), \
            patch("dgutbot.experimental.ulearning_ai_browser._assistant_from_workbench", return_value="1234"):
        access = discover_browser_access()
    assert access.context.assistant_id == "1234"
    assert access.context.course_id == "654321"
    assert access.referer == "https://aijx.dgut.edu.cn/ai/1234?auth=memory-only&courseId=654321&theme=blue"


def test_cached_discovery_needs_no_browser_and_prefers_first_ai_course():
    observed = []

    def assistant(access):
        observed.append((access.context.course_id, access.referer))
        if access.context.course_id == "11":
            raise _NoCourseAiAssistant("missing")
        return "1234"

    with patch("dgutbot.experimental.ulearning_ai_browser._assistant_from_workbench", side_effect=assistant):
        access = discover_cached_access("memory-only", [11, 22, 22], user_agent="test-agent")
    assert [item[0] for item in observed] == ["11", "22"]
    assert "courseWeb=true" in observed[0][1]
    assert access.context.assistant_id == "1234"
    assert access.context.course_id == "22"
    assert access.referer == "https://aijx.dgut.edu.cn/ai/1234?auth=memory-only&courseId=22&theme=ai"
    assert "memory-only" not in repr(access)


def test_cached_discovery_rejects_missing_login_or_courses():
    with pytest.raises(UlearningAiError, match="login"):
        discover_cached_access("", [1])
    with pytest.raises(UlearningAiError, match="course"):
        discover_cached_access("memory-only", [])
