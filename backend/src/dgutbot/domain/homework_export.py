"""将作业要求、本人提交与可见互评转换成课件资源库页面。"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from typing import Any, Callable

from dgutbot.domain.course_scan import AuthRequiredError
from dgutbot.domain.courseware_export import ExportPage, ExportSectionResult, parse_chapter, safe_name
from dgutbot.domain.courseware_sources import resource_url
from dgutbot.domain.homework_scan import homework_items
from dgutbot.domain.sign_monitor import LMS_BASE

HOMEWORK_NODE = "homework:"
HOMEWORK_API = "https://lms.dgut.edu.cn/homeworkapi"


def _result(payload: Any) -> Any:
    return payload.get("result", payload) if isinstance(payload, dict) else payload


def _records(payload: Any) -> list[dict[str, Any]]:
    value = _result(payload)
    if isinstance(value, dict):
        value = value.get("list") or value.get("records") or []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def append_homework_directory(payload: dict[str, Any], course: Any, fetch: Callable[..., Any]) -> dict[str, Any]:
    """作业作为独立章节追加；不受作业列表的本机隐藏标记影响。"""
    seen: set[str] = set()
    pn = 1
    try:
        while True:
            response = fetch(f"{LMS_BASE}/homeworks/student/v2", params={
                "ocId": course.id, "pn": pn, "ps": 50, "lang": "zh",
            })
            items = homework_items(course, response)
            added = 0
            for item in items:
                if not item.homeworkId:
                    payload.setdefault("failures", []).append({"chapter": item.title, "reason": "平台未提供作业标识"})
                    continue
                if item.homeworkId in seen:
                    continue
                seen.add(item.homeworkId)
                chapters = payload["chapters"]
                chapters.append({
                    "nodeid": f"{HOMEWORK_NODE}{course.id}:{item.homeworkId}",
                    "nodetitle": f"作业：{item.title} ({item.homeworkId})", "orderindex": len(chapters),
                })
                added += 1
            data = _result(response)
            meta = dict(response) if isinstance(response, dict) else {}
            if isinstance(data, dict):
                meta.update(data)
            total = meta.get("total")
            size = int(meta.get("pageSize") or 50)
            if not added or (total is not None and pn * size >= int(total)) or (total is None and len(items) < size):
                break
            pn += 1
    except AuthRequiredError:
        raise
    except Exception as error:
        payload.setdefault("failures", []).append({"chapter": "课程作业列表", "reason": str(error)})
    return payload


@dataclass(frozen=True)
class HomeworkPage(ExportPage):
    """每份提交有稳定独立目录，互评排序改变也不会复用别人的同名附件。"""

    assignment_folder: str = ""
    document_folder: str = ""

    @property
    def folder(self) -> str:
        return f"作业/{self.assignment_folder}/{self.document_folder}"

    @property
    def stem(self) -> str:
        return "正文"


def _text(label: str, value: Any) -> str:
    if value in (None, ""):
        return ""
    return f"<p>{html.escape(label)}：{html.escape(str(value))}</p>"


def _document(
    homework_id: str, title: str, label: str, key: str, fragments: list[str], files: Any,
    source: str, failures: list[dict[str, str]],
) -> HomeworkPage:
    entries = []
    try:
        values = json.loads(files) if isinstance(files, str) and files.strip() else files or []
        for entry in values:
            name = str(entry.get("fileName") or entry.get("name") or "附件")
            url = str(entry.get("filePath") or entry.get("url") or "")
            if not url or url.startswith(("data:", "blob:", "file:")):
                failures.append({"file": f"{label} / {name}", "reason": "附件没有可下载地址"})
                continue
            entries.append({"fileName": name, "url": resource_url(url), "size": entry.get("fileSize") or 0})
    except (TypeError, ValueError, AttributeError) as error:
        failures.append({"file": label, "reason": str(error)})
    body = "\n".join(fragment for fragment in fragments if fragment)
    body = body or "<p>当前没有正文或附件。</p>"
    body += f'<p><a href="{html.escape(source, quote=True)}">来源课程作业页</a></p>'
    # 复用课件解析器的图片、嵌套文档、附件去重与本地链接逻辑。
    page = parse_chapter({"wholepageItemDTOList": [{"wholepageDTOList": [{
        "content": label, "contentType": 5,
        "coursepageDTOList": [{"type": 12, "content": body, "resourceDTOList": entries}],
    }]}]}, chapter_title=f"作业：{title} ({homework_id})", chapter_index=1)[0]
    return HomeworkPage(
        **(vars(page) | {"source_url": source, "resource_id": f"{HOMEWORK_NODE}{homework_id}:{key}"}),
        assignment_folder=f"{safe_name(homework_id, limit=24)}_{safe_name(title, limit=40)}",
        document_folder=safe_name(key, limit=48),
    )


def export_homework(node_id: str, user_id: int | None, fetch: Callable[..., Any]) -> ExportSectionResult:
    """仅发 GET：按当前账号读取详情、本人提交、互评对象和评分规则。"""
    if user_id is None:
        raise AuthRequiredError("登录信息缺少用户标识，请重新登录")
    course_id, homework_id = node_id[len(HOMEWORK_NODE):].split(":", 1)
    detail = _result(fetch(f"{HOMEWORK_API}/stuHomework/homeworkDetail/{homework_id}/{user_id}/{course_id}"))
    activity = detail.get("activityHomework") or {}
    if not activity:
        raise ValueError("平台未提供作业详情")
    title = str(activity.get("homeworkTitle") or homework_id)
    source = f"https://lms.dgut.edu.cn/courseweb/ulearning/index.html#/course/homework?courseId={course_id}"
    pages: list[ExportPage] = []
    failures: list[dict[str, str]] = []

    def document(label: str, key: str, fragments: list[str], files: Any = None) -> None:
        pages.append(_document(homework_id, title, label, key, fragments, files, source, failures))

    document("题目与要求", "01_题目与要求", [
        str(activity.get("homeworkRequest") or ""),
        str(activity.get("homeworkContent") or ""),
        _text("开始时间", activity.get("startTime")), _text("截止时间", activity.get("endTime")),
        _text("互评截止", activity.get("peerReviewTime")), _text("满分", activity.get("grade")),
    ], activity.get("fileUpload"))
    mine = detail.get("studentHomework") or {}
    published = activity.get("gradePublish") not in (0, "0")
    own_fragments = [
        "<p>当前为草稿。</p>" if mine.get("draftState") in (1, "1") else "",
        str(mine.get("content") or ""), _text("更新时间", mine.get("updateTime")),
    ]
    if not mine:
        own_fragments.append("<p>当前没有本人提交。</p>")
    if published:
        own_fragments.extend([_text("成绩", mine.get("grade")), _text("教师评分", mine.get("teacherScore")),
                              _text("教师评语", mine.get("overallComment")), str(mine.get("contentComment") or "")])
    document("我的提交", "02_我的提交", own_fragments, mine.get("fileUpload"))

    if published:
        for index, feedback in enumerate(_records(detail.get("peerReviewHomeworkList")), 1):
            document(f"收到的互评 {index}", f"03_收到的互评_{feedback.get('peerReviewID') or index}", [
                _text("评分", feedback.get("score")), str(feedback.get("comment") or ""),
            ], feedback.get("fileUpload"))

    if activity.get("autoRemark") in (2, "2"):
        try:
            peers = _records(fetch(f"{HOMEWORK_API}/stuHomework/peerReviewHomeworkDatil/{homework_id}/{user_id}"))
            for index, peer in enumerate(peers, 1):
                identifier = str(peer.get("peerReviewID") or peer.get("id") or index)
                document(f"互评作业 {index}", f"04_互评作业_{identifier}", [
                    str(peer.get("content") or ""), _text("我的评分", peer.get("score")),
                    str(peer.get("comment") or ""),
                ], peer.get("fileUpload"))
        except AuthRequiredError:
            raise
        except Exception as error:
            failures.append({"file": "互评作业", "reason": str(error)})
        try:
            rules = _records(fetch(f"{HOMEWORK_API}/teaHomework/peerReviewHomeworkRule/{homework_id}"))
            document("评分规则", "05_评分规则", [
                _text(str(rule.get("scoringPoint") or rule.get("title") or "评分项"), rule.get("score"))
                for rule in rules
            ] or ["<p>平台未提供分项评分规则。</p>"])
        except AuthRequiredError:
            raise
        except Exception as error:
            failures.append({"file": "互评规则", "reason": str(error)})
    return ExportSectionResult(tuple(pages), tuple(failures))
