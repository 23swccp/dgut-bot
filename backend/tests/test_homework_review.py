import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from dgutbot.domain.homework_review import (
    build_peer_review_payloads,
    normalize_homework_review,
)
from dgutbot.domain.yxy_backend import SignBackend


DETAIL = {"result": {"activityHomework": {"id": 99, "homeworkTitle": "课程论文", "grade": 100}}}
PEERS = {"result": [
    {
        "peerReviewID": 11,
        "userID": 22,
        "content": "<p>匿名正文 <b>一</b></p>",
        "fileUpload": '[{"fileName":"答案.pdf","url":"https://secret.example/file"}]',
        "score": None,
        "comment": None,
        "isPeerReviewTime": 2,
    },
    {
        "peerReviewID": 12,
        "userID": 23,
        "content": "<p>匿名正文二</p>",
        "score": 88,
        "comment": "已评",
        "isPeerReviewTime": 2,
    },
]}


def test_normalize_defaults_unscored_to_100_and_keeps_existing_score_anonymous():
    value = normalize_homework_review("item", DETAIL, PEERS, {"result": []})
    assert value["defaultScore"] == 100
    assert value["tasks"][0]["score"] == 100
    assert value["tasks"][1]["score"] == 88
    assert value["tasks"][1]["wasScored"] and value["tasks"][1]["editable"]
    assert value["tasks"][0]["label"] == "待评作业 1"
    assert value["tasks"][0]["content"] == "匿名正文\n一"
    assert value["tasks"][0]["attachments"] == ["答案.pdf"]
    serialized = str(value)
    assert "secret.example" not in serialized and "userID" not in serialized


def test_normalize_caps_default_at_assignment_full_score():
    detail = {"result": {"activityHomework": {"grade": 60}}}
    value = normalize_homework_review("item", detail, PEERS, {"result": []})
    assert value["fullScore"] == 60 and value["tasks"][0]["score"] == 60


def test_unscored_review_is_not_editable_before_peer_review_time():
    peers = {"result": [dict(PEERS["result"][0], isPeerReviewTime=1)]}
    value = normalize_homework_review("item", DETAIL, peers, {"result": []})
    assert not value["tasks"][0]["wasScored"]
    assert not value["tasks"][0]["editable"]
    with pytest.raises(ValueError, match="当前不可修改"):
        build_peer_review_payloads(
            DETAIL, peers, {"result": []},
            [{"key": value["tasks"][0]["key"], "score": 100, "comment": "", "ruleScores": {}}], 7,
        )


def test_builds_official_payload_and_allows_modifying_existing_score():
    view = normalize_homework_review("item", DETAIL, PEERS, {"result": []})
    drafts = [
        {"key": view["tasks"][0]["key"], "score": 100, "comment": "很好", "ruleScores": {}},
        {"key": view["tasks"][1]["key"], "score": 91.5, "comment": "修改", "ruleScores": {}},
    ]
    assert build_peer_review_payloads(DETAIL, PEERS, {"result": []}, drafts, 7) == [
        {"comment": "很好", "score": 100, "peerReviewID": 11, "ruleScoreList": []},
        {"comment": "修改", "score": 91.5, "peerReviewID": 12, "ruleScoreList": []},
    ]


def test_rule_scores_are_rebound_to_fresh_server_ids_and_must_match_total():
    rules = {"result": [
        {"id": 31, "acthomeworkId": 99, "scoringPoint": "内容", "score": 60},
        {"id": 32, "acthomeworkId": 99, "scoringPoint": "格式", "score": 40},
    ]}
    view = normalize_homework_review("item", DETAIL, PEERS, rules)
    task = view["tasks"][0]
    rule_scores = {rule["key"]: rule["score"] for rule in task["rules"]}
    payload = build_peer_review_payloads(
        DETAIL, PEERS, rules,
        [{"key": task["key"], "score": 100, "comment": "", "ruleScores": rule_scores}],
        7,
    )[0]
    assert payload["score"] == 100
    assert payload["ruleScoreList"] == [
        {"acthomeworkId": 99, "grade": 60, "id": 0, "ruleId": 31, "studentId": 22, "reviewerId": 7},
        {"acthomeworkId": 99, "grade": 40, "id": 0, "ruleId": 32, "studentId": 22, "reviewerId": 7},
    ]
    with pytest.raises(ValueError, match="必须等于"):
        build_peer_review_payloads(
            DETAIL, PEERS, rules,
            [{"key": task["key"], "score": 99, "comment": "", "ruleScores": rule_scores}],
            7,
        )


@pytest.mark.parametrize("draft, message", [
    ({"key": "forged", "score": 100}, "对象无效"),
    ({"score": 100, "comment": "x" * 2001}, "对象无效"),
])
def test_rejects_untrusted_review_drafts(draft, message):
    if "key" not in draft:
        view = normalize_homework_review("item", DETAIL, PEERS, {"result": []})
        draft["key"] = view["tasks"][0]["key"]
        message = "评语不能超过"
    with pytest.raises(ValueError, match=message):
        build_peer_review_payloads(DETAIL, PEERS, {"result": []}, [draft], 7)


def _seed_item(backend: SignBackend, *, end_time: str = "") -> str:
    item = {
        "id": "scan-item", "homeworkId": "99", "courseId": "42", "courseName": "课程",
        "teacherName": "老师", "title": "课程论文", "state": 1, "stateLabel": "未互评",
        "needsAction": True, "endTime": end_time, "url": "https://lms.dgut.edu.cn/",
    }
    backend.homework_scan._snapshot.update(
        state="completed", groups=[{"courseId": "42", "courseName": "课程", "items": [item]}],
    )
    return item["id"]


def test_backend_reads_three_verified_review_endpoints_and_closes_client():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        backend.user_id = 7
        responses = []
        for value in (DETAIL, PEERS, {"result": []}):
            response = Mock()
            response.json.return_value = value
            responses.append(response)
        client = Mock()
        client.request.side_effect = responses
        with patch("dgutbot.domain.yxy_backend.BrowserApiClient", return_value=client):
            values = backend._homework_review_payloads({"homeworkId": "99", "courseId": "42"})
        assert values == (DETAIL, PEERS, {"result": []})
        urls = [call.args[1] for call in client.request.call_args_list]
        assert urls[0].endswith("/homeworkDetail/99/7/42")
        assert urls[1].endswith("/peerReviewHomeworkDatil/99/7")
        assert urls[2].endswith("/peerReviewHomeworkRule/99")
        client.close.assert_called_once()


def test_backend_one_click_submit_posts_each_review_to_verified_endpoint():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        backend.user_id = 7
        item_id = _seed_item(backend)
        view = normalize_homework_review(item_id, DETAIL, PEERS, {"result": []})
        drafts = [{"key": task["key"], "score": 100 if not task["wasScored"] else 90, "comment": "", "ruleScores": {}}
                  for task in view["tasks"]]
        response = Mock()
        response.json.return_value = {"code": 200}
        client = Mock()
        client.request.return_value = response
        with (
            patch.object(backend, "_homework_review_payloads", return_value=(DETAIL, PEERS, {"result": []})),
            patch("dgutbot.domain.yxy_backend.BrowserApiClient", return_value=client),
        ):
            result = backend.submit_homework_reviews(item_id, drafts)
        assert result["submitted"] == 2 and result["total"] == 2
        assert all(call.args[:2] == ("POST", "https://lms.dgut.edu.cn/homeworkapi/stuHomework/savePeerReview")
                   for call in client.request.call_args_list)
        assert [call.kwargs["json"]["peerReviewID"] for call in client.request.call_args_list] == [11, 12]
        client.close.assert_called_once()


def test_bulk_100_submits_only_unscored_editable_reviews_before_deadline():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        backend.user_id = 7
        item_id = _seed_item(backend, end_time="2999-09-24T00:00:00+08:00")
        with (
            patch.object(backend, "_homework_review_payloads", return_value=(DETAIL, PEERS, {"result": []})),
            patch.object(backend, "_post_homework_review_payloads", return_value=1) as submit,
        ):
            result = backend.submit_pending_homework_reviews([item_id])
        assert result["candidateAssignments"] == 1
        assert result["submittedAssignments"] == 1 and result["submittedReviews"] == 1
        payloads = submit.call_args.args[0]
        assert payloads == [{"comment": "", "score": 100, "peerReviewID": 11, "ruleScoreList": []}]


def test_bulk_100_skips_expired_or_unknown_deadlines_without_reading_details():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        backend.user_id = 7
        item_id = _seed_item(backend, end_time="2000-01-01T00:00:00+08:00")
        with patch.object(backend, "_homework_review_payloads") as read:
            result = backend.submit_pending_homework_reviews([item_id])
        assert result["candidateAssignments"] == 0 and result["submittedReviews"] == 0
        read.assert_not_called()


def test_bulk_candidate_preflight_excludes_homework_before_peer_review_time():
    with tempfile.TemporaryDirectory() as directory:
        backend = SignBackend(lambda *_: None, root=Path(directory))
        backend.user_id = 7
        _seed_item(backend, end_time="2999-09-24T00:00:00+08:00")
        peers = {"result": [dict(PEERS["result"][0], isPeerReviewTime=1)]}
        with patch.object(backend, "_homework_review_payloads", return_value=(DETAIL, peers, {"result": []})):
            result = backend.pending_homework_review_candidates()
        assert result["items"] == [] and result["failures"] == []
