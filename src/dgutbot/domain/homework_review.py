"""优学院互评详情的匿名展示适配与提交载荷校验。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any


def _result(value: Any) -> Any:
    return value.get("result", value) if isinstance(value, dict) else value


def _key(prefix: str, value: Any) -> str:
    return hashlib.sha256(f"{prefix}|{value}".encode("utf-8")).hexdigest()[:24]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = " ".join(str(data).split())
        if text:
            self.parts.append(text)


def plain_text(value: Any, *, limit: int = 20_000) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except (TypeError, ValueError):
        return str(value or "")[:limit]
    return "\n".join(parser.parts)[:limit]


def _attachments(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    values = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        name = str(item.get("fileName") or item.get("name") or "附件").strip()
        if name and name not in names:
            names.append(name[:200])
    return names


def _number(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return result if result.is_finite() else default


def _mapped_rules(rules_payload: Any) -> list[dict[str, Any]]:
    values = _result(rules_payload)
    if isinstance(values, dict):
        values = values.get("list") or values.get("records") or []
    if not isinstance(values, list):
        return []
    rules: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict) or value.get("id") in (None, ""):
            continue
        maximum = max(Decimal("0"), _number(value.get("score")))
        rules.append({
            "key": _key("rule", value["id"]),
            "title": str(value.get("scoringPoint") or value.get("title") or "评分项")[:500],
            "maxScore": float(maximum),
            "_raw": value,
        })
    return rules


def normalize_homework_review(
    item_id: str,
    detail_payload: Any,
    peer_payload: Any,
    rules_payload: Any,
) -> dict[str, Any]:
    detail = _result(detail_payload)
    detail = detail if isinstance(detail, dict) else {}
    activity = detail.get("activityHomework") if isinstance(detail.get("activityHomework"), dict) else {}
    peers = _result(peer_payload)
    if isinstance(peers, dict):
        peers = peers.get("list") or peers.get("records") or []
    peers = peers if isinstance(peers, list) else []
    rules = _mapped_rules(rules_payload)
    full_score = max(Decimal("0"), _number(activity.get("grade"), Decimal("100")))
    if not full_score:
        full_score = Decimal("100")
    default_score = min(Decimal("100"), full_score)
    tasks: list[dict[str, Any]] = []
    for index, peer in enumerate(peers):
        if not isinstance(peer, dict) or peer.get("peerReviewID") in (None, ""):
            continue
        existing = peer.get("score") not in (None, "")
        score = _number(peer.get("score"), default_score) if existing else default_score
        existing_rules = peer.get("ruleScoreList") if isinstance(peer.get("ruleScoreList"), list) else []
        existing_by_id = {
            str(value.get("ruleId")): value.get("grade")
            for value in existing_rules if isinstance(value, dict) and value.get("ruleId") not in (None, "")
        }
        task_rules = [{
            "key": rule["key"], "title": rule["title"], "maxScore": rule["maxScore"],
            "score": existing_by_id.get(str(rule["_raw"]["id"]), rule["maxScore"] if not existing else ""),
        } for rule in rules]
        tasks.append({
            "key": _key("review", peer["peerReviewID"]),
            "label": f"待评作业 {index + 1}",
            "content": plain_text(peer.get("content")),
            "attachments": _attachments(peer.get("fileUpload")),
            "score": float(score),
            "comment": str(peer.get("comment") or "")[:2000],
            "wasScored": existing,
            # 实页样本中 2 表示当前处于互评时间；缺失或其他值一律按不可编辑处理。
            "editable": peer.get("isPeerReviewTime") in (2, "2"),
            "rules": task_rules,
        })
    return {
        "itemId": str(item_id),
        "title": str(activity.get("homeworkTitle") or "互评作业"),
        "fullScore": float(full_score),
        "defaultScore": float(default_score),
        "tasks": tasks,
    }


def _validated_score(value: Any, maximum: Decimal, label: str, *, integer: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{label}不是有效分数") from error
    if not number.is_finite() or number < 0 or number > maximum:
        raise ValueError(f"{label}必须在 0 到 {maximum:g} 分之间")
    if integer and number != number.to_integral_value():
        raise ValueError(f"{label}只支持整数")
    if not integer and number.as_tuple().exponent < -1:
        raise ValueError(f"{label}最多保留一位小数")
    return number


def build_peer_review_payloads(
    detail_payload: Any,
    peer_payload: Any,
    rules_payload: Any,
    drafts: Any,
    reviewer_id: int,
) -> list[dict[str, Any]]:
    """重新以服务端数据绑定 ID，拒绝客户端伪造或越权的互评对象。"""
    if not isinstance(drafts, list) or not drafts:
        raise ValueError("没有可提交的互评内容")
    detail = _result(detail_payload)
    detail = detail if isinstance(detail, dict) else {}
    activity = detail.get("activityHomework") if isinstance(detail.get("activityHomework"), dict) else {}
    maximum = max(Decimal("0"), _number(activity.get("grade"), Decimal("100"))) or Decimal("100")
    peers = _result(peer_payload)
    if isinstance(peers, dict):
        peers = peers.get("list") or peers.get("records") or []
    peers = peers if isinstance(peers, list) else []
    peer_by_key = {
        _key("review", peer.get("peerReviewID")): peer
        for peer in peers if isinstance(peer, dict) and peer.get("peerReviewID") not in (None, "")
    }
    rules = _mapped_rules(rules_payload)
    rule_by_key = {rule["key"]: rule for rule in rules}
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, draft in enumerate(drafts, 1):
        if not isinstance(draft, dict):
            raise ValueError(f"第 {position} 份互评数据无效")
        review_key = str(draft.get("key") or "")
        if review_key in seen or review_key not in peer_by_key:
            raise ValueError(f"第 {position} 份互评对象无效或重复")
        seen.add(review_key)
        peer = peer_by_key[review_key]
        if peer.get("isPeerReviewTime") not in (2, "2"):
            raise ValueError(f"第 {position} 份互评当前不可修改")
        score = _validated_score(draft.get("score"), maximum, f"第 {position} 份总分")
        comment = str(draft.get("comment") or "")
        if len(comment) > 2000:
            raise ValueError(f"第 {position} 份评语不能超过 2000 字")
        rule_scores = draft.get("ruleScores") if isinstance(draft.get("ruleScores"), dict) else {}
        mapped_rule_scores: list[dict[str, Any]] = []
        rule_total = Decimal("0")
        if rules:
            if set(rule_scores) != set(rule_by_key):
                raise ValueError(f"第 {position} 份互评需要填写全部评分项")
            for key, rule in rule_by_key.items():
                grade = _validated_score(rule_scores[key], _number(rule["maxScore"]), f"第 {position} 份“{rule['title']}”", integer=True)
                rule_total += grade
                raw = rule["_raw"]
                mapped_rule_scores.append({
                    "acthomeworkId": raw.get("acthomeworkId") or activity.get("id"),
                    "grade": int(grade), "id": 0, "ruleId": raw["id"],
                    "studentId": peer.get("userID") or peer.get("userId"),
                    "reviewerId": int(reviewer_id),
                })
            if rule_total != score:
                raise ValueError(f"第 {position} 份总分必须等于各评分项之和")
        numeric_score: int | float = int(score) if score == score.to_integral_value() else float(score)
        payloads.append({
            "comment": comment,
            "score": numeric_score,
            "peerReviewID": peer["peerReviewID"],
            "ruleScoreList": mapped_rule_scores,
        })
    return payloads
