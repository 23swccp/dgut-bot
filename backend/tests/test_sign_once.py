"""单次实验脚本的离线回归，禁止请求真实平台。"""

from unittest.mock import Mock, patch

import pytest

from backend.tools.sign_once import run


def response(value):
    return Mock(json=lambda: value)


@pytest.fixture
def login():
    login = Mock()
    login.import_browser.return_value = {"userId": 7, "roleId": 9}
    login._session.headers = {"Authorization": "fixture-token"}
    login._session.get.side_effect = [
        response({"code": 1, "result": {"list": [
            {"relationId": 12, "relationType": 1, "scoreType": 2, "status": 0},
        ]}}),
        response({"status": 0}),
        response({"status": 1}),
    ]
    with patch("backend.tools.sign_once.BrowserSession", return_value=login):
        yield login


@pytest.mark.parametrize("score_type", [1, 2])
def test_nonempty_code_is_sent_once_with_verified_identity_and_status_rechecked(login, score_type):
    login._session.get.side_effect = [
        response({"code": 1, "result": {"list": [
            {"relationId": 12, "relationType": 1, "scoreType": score_type, "status": 0},
        ]}}), response({"status": 0}), response({"status": 1}),
    ]
    with patch("backend.tools.sign_once.SignMonitor.direct_sign_request", return_value=Mock(
        status_code=200, text='{"status":200,"newStatus":1}',
    )) as submit:
        result = run(9222, 11, 12, "1234", "0,0", "", score_type)
    submit.assert_called_once()
    assert submit.call_args.args[0]["userID"] == 7
    assert submit.call_args.args[0]["attendanceCode"] == "1234"
    assert result["before"] == 0 and result["after"] == 1
    assert login._session.get.call_count == 3
    login.clear.assert_called_once()


def test_empty_code_fails_before_reading_credentials():
    with patch("backend.tools.sign_once.BrowserSession") as login:
        with pytest.raises(ValueError, match="不能为空"):
            run(9222, 11, 12, " ", "0,0", "")
    login.assert_not_called()


def test_submission_exception_never_retries(login):
    with patch("backend.tools.sign_once.SignMonitor.direct_sign_request", side_effect=RuntimeError("response lost")) as submit:
        with pytest.raises(RuntimeError, match="response lost"):
            run(9222, 11, 12, "1234", "0,0", "")
    submit.assert_called_once()
    assert login._session.get.call_count == 2
    login.clear.assert_called_once()
