"""读取课程资源区与所有教材；保持导出服务的章节队列接口。"""

from __future__ import annotations

import html
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit

from dgutbot.domain.course_scan import AuthRequiredError
from dgutbot.domain.courseware_export import (
    UA_BASE, ExportAttachment, ExportPage, ExportSectionResult, embedded_resources, html_to_markdown,
    render_page_html,
)
from dgutbot.domain.sign_monitor import LMS_BASE

RESOURCE_BASE = "https://uobs.dgut.edu.cn/view"
RESOURCE_NODE = "resources:"


def resource_url(location: str) -> str:
    if location.startswith(("https://", "http://")):
        return location.replace("https://leicloud.ulearning.cn", RESOURCE_BASE, 1).replace(
            "http://leicloud.qiniudn.com", RESOURCE_BASE, 1,
        )
    return f"{RESOURCE_BASE}/{location.lstrip('/')}" if location else ""


def export_directory(course: Any, fetch: Callable[..., Any], *, include_courseware: bool = True) -> dict[str, Any]:
    """资源区总是独立扫描；没有教材的课程也可以导出。"""
    chapters = [{"nodeid": f"{RESOURCE_NODE}{course.id}", "nodetitle": "课程资源", "orderindex": 0}]
    failures = []
    if not include_courseware:
        return {"coursename": course.name, "chapters": chapters, "failures": failures}
    try:
        response = fetch(f"{LMS_BASE}/textbook/student/{course.id}/list", params={"lang": "zh"})
    except AuthRequiredError:
        raise
    except Exception as error:
        return {"coursename": course.name, "chapters": chapters,
                "failures": [{"chapter": "学习页教材列表", "reason": str(error)}]}
    textbooks = response if isinstance(response, list) else response.get("result", [])
    textbooks = [book for book in textbooks if book.get("courseId")]
    if textbooks:
        try:
            response = fetch(f"{LMS_BASE}/classes/information/student/{course.id}", params={"lang": "zh"})
        except AuthRequiredError:
            raise
        except Exception as error:
            return {"coursename": course.name, "chapters": chapters,
                    "failures": [{"chapter": "学习页班级信息", "reason": str(error)}]}
        class_info = response.get("result", response)
        class_id = str(class_info.get("classId") or "")
        if not class_id:
            failures.append({"chapter": "学习页", "reason": "平台未提供班级标识"})
        else:
            for book in textbooks:
                book_id = str(book["courseId"])
                book_name = str(book.get("courseName") or book.get("name") or book_id)
                try:
                    payload = fetch(f"{UA_BASE}/course/stu/{quote(book_id, safe='')}/directory",
                                    params={"classId": class_id}, ua=True)
                except AuthRequiredError:
                    raise
                except Exception as error:
                    failures.append({"chapter": book_name, "reason": str(error)})
                    continue
                for entry in payload.get("chapters") or []:
                    title = str(entry.get("nodetitle") or entry.get("title") or "未命名章节")
                    chapters.append({**entry, "nodetitle": f"{book_name} / {title}" if len(textbooks) > 1 else title,
                                     "orderindex": len(chapters)})
    return {"coursename": course.name, "chapters": chapters, "failures": failures}


def export_resources(course_id: str, fetch: Callable[..., Any]) -> ExportSectionResult:
    """按平台资源列表分页并递归文件夹，详情中的 type 与列表分类分别处理。"""
    pages: list[ExportPage] = []
    failures: list[dict[str, str]] = []
    source = f"https://lms.dgut.edu.cn/ulearning/index.html#/course/resource?courseId={quote(course_id)}"

    def collect(parent_id: str, folder: str) -> None:
        pn = 1
        while True:
            payload = fetch(f"{LMS_BASE}/course/student/content/v2", params={
                "ocId": course_id, "parentId": parent_id, "keyword": "", "pn": pn, "ps": 50,
                "version": 2, "orderBy": "createTime", "desc": 1,
            })
            entries = payload.get("list") or []
            for entry in entries:
                resource_id = str(entry["id"])
                title = str(entry.get("title") or resource_id)
                category = int(entry.get("type") or 0)
                if category in {11, 12}:  # 试题和试卷不读取详情。
                    continue
                try:
                    if category == 0:
                        collect(resource_id, f"{folder} / {title}")
                        continue
                    detail = fetch(f"{LMS_BASE}/course/content/{quote(resource_id, safe='')}", params={
                        "shareType": "" if entry.get("shareType") == 5 else entry.get("shareType", ""),
                        "resourceFlag": 2,
                    })
                    kind = int(detail.get("contentType", category))
                    if kind in {11, 12}:
                        continue
                    location = str(detail.get("location") or "")
                    attachment_type = int(detail.get("type") or 0)
                    description, embedded = embedded_resources(str(detail.get("description") or ""),
                                                               base_url=RESOURCE_BASE + "/")
                    fragments = [description]
                    attachments = tuple(embedded)
                    url = resource_url(location) if attachment_type == 1 else location
                    if attachment_type == 2 or kind == 7:
                        fragments.append(f'<p>外部链接：<a href="{html.escape(url, quote=True)}">'
                                         f'{html.escape(title)}</a></p>')
                    elif attachment_type == 1 and location:
                        if str(detail.get("remark3") or "0") == "1":
                            fragments.append("<p>平台未开放下载。</p>")
                        else:
                            name = str(detail.get("title") or title)
                            extension = str(detail.get("mimeType") or "").lstrip(".")
                            if extension and not name.lower().endswith(f".{extension.lower()}"):
                                name += f".{extension}"
                            playback_url = ""
                            parts = urlsplit(url)
                            suffix = parts.path.rsplit("/", 1)[-1].rsplit(".", 1)
                            if kind == 1 and len(suffix) == 2 and suffix[1].lower() not in {"mp4", "m3u8"}:
                                playback_url = urlunsplit(parts._replace(path=parts.path.rsplit(".", 1)[0] + ".mp4"))
                            attachments += (ExportAttachment(name, url, int(detail.get("contentSize") or 0), playback_url),)
                    elif kind != 8:
                        failures.append({"resource": title, "reason": f"资源类型 {kind} 尚无可下载地址"})
                    fragments.append(f'<p><a href="{html.escape(source, quote=True)}">来源课程资源区</a></p>')
                    pages.append(ExportPage(
                        chapter=folder, chapter_index=1, page_index=len(pages) + 1,
                        title=title, content_type=kind,
                        markdown="\n\n".join(html_to_markdown(part, base_url=RESOURCE_BASE + "/")
                                             for part in fragments if part),
                        html=render_page_html(chapter=folder, title=title, fragments=fragments),
                        attachments=attachments, source_url=source, resource_id=resource_id,
                    ))
                except AuthRequiredError:
                    raise
                except Exception as error:
                    failures.append({"resource": title, "reason": str(error)})
            if pn * int(payload.get("pageSize") or 50) >= int(payload.get("total") or 0) or not entries:
                break
            pn += 1

    collect("0", "课程资源")
    return ExportSectionResult(tuple(pages), tuple(failures))
