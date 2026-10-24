import json
from unittest.mock import Mock, patch

import pytest

from backend.tools.digit_code_api import collect, read_code, wait_for_activity


def reply(data):
    return Mock(json=lambda: data)


@pytest.mark.parametrize("code", ["0078", 78])
def test_reads_only_teacher_detail_and_preserves_leading_zero(code):
    session = Mock()
    session.get.return_value = reply({"attendanceCode": code, "type": 2, "finish": 1,
                                      "refreshCode": 1, "refreshCodeTime": 5, "users": ["private"]})
    value, metadata = read_code(session, 12)
    assert value == "0078" and "users" not in metadata
    assert "getNewAttendanceDetail/12/4/1" in session.get.call_args.args[0]
    session.post.assert_not_called()


def test_denied_response_stops_without_trying_generator_endpoint():
    session = Mock()
    session.get.return_value = reply({"msg": False, "status": "false"})
    with pytest.raises(ValueError, match="未通过"):
        read_code(session, 12)
    session.get.assert_called_once()
    session.post.assert_not_called()


def test_samples_are_saved_incrementally_and_end_stops_sampling(tmp_path):
    session = Mock()
    session.get.side_effect = [reply({"attendanceCode": "0078", "type": 2, "finish": 1}),
                               reply({"attendanceCode": "0078", "type": 2, "finish": 0})]
    path = tmp_path / "samples.json"
    with patch("backend.tools.digit_code_api.time.sleep"):
        result = collect(session, 12, 3, path)
    assert len(result["observations"]) == 1 and result["stopReason"] == "activity-ended"
    assert json.loads(path.read_text(encoding="utf-8"))["observations"][0]["code"] == "0078"
    assert session.get.call_count == 2
    session.post.assert_not_called()


def test_waits_for_new_active_teacher_activity_without_reusing_ended_one():
    session = Mock()
    session.get.side_effect = [
        reply({"list": [{"relationId": 12, "relationType": 1, "status": 2}]}),
        reply({"list": [{"relationId": 13, "relationType": 1, "status": 0}]}),
        reply({"attendanceCode": "0078", "type": 2, "finish": 1, "refreshCode": 1, "refreshCodeTime": 5}),
    ]
    with patch("backend.tools.digit_code_api.time.sleep"):
        assert wait_for_activity(session, 10, 300) == 13
    assert session.get.call_count == 3
    session.post.assert_not_called()
