"""课件导出的离线单元测试：全部使用合成或脱敏样本，不访问网络。"""

import json
import io
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit

import pytest
import requests

from dgutbot.domain.courseware_export import (
    CoursewareExportService, CoursewareLibrary, ExportAttachment, ExportPage,
    UA_BASE, html_to_markdown, parse_chapter, parse_directory, safe_name,
)
from dgutbot.domain.course_scan import AuthRequiredError
from dgutbot.domain.courseware_sources import export_directory, export_resources
from dgutbot.domain.yxy_backend import BrowserApiClient, BrowserLoginRedirectError, Course, SignBackend


@dataclass(frozen=True)
class FakeCourse:
    id: str = "42"
    name: str = "形势与政策"


def block(html_value="", *, kind=12, resources=None, questions=None, order=1):
    value = {"type": kind, "content": html_value, "orderIndex": order}
    if resources is not None:
        value["resourceDTOList"] = resources
    if questions is not None:
        value["questionDTOList"] = questions
    return value


def page(title, content_type=5, *, blocks=None, order=1):
    return {"contentType": content_type, "content": title, "orderindex": order,
            "coursepageDTOList": blocks if blocks is not None else [block(f"<p>{title} 正文</p>")]}


def chapter(*pages, item_order=1):
    return {"itemid": item_order, "orderindex": item_order, "wholepageDTOList": list(pages)}


def chapter_payload(*items):
    return {"chapterid": 1, "wholepageItemDTOList": list(items)}


def directory(chapters, course_name="形势与政策"):
    return {"coursename": course_name, "courseid": 12, "chapters": chapters}


def node(title="第一章", nodeid=101, order=1, hide=0):
    return {"nodetitle": title, "nodeid": nodeid, "orderindex": order, "hide": hide, "items": []}


def _fail(error):
    raise error


def wait(service, timeout=3.0):
    deadline = time.monotonic() + timeout
    while service.snapshot()["state"] in {"running", "cancelling"} and time.monotonic() < deadline:
        time.sleep(0.01)
    return service.snapshot()

# ----------------------------------------------------------------- Markdown
def test_html_to_markdown_keeps_paragraphs_headings_and_inline_marks():
    fragment = (
        "<h2>第一节</h2><p><span><font>第一段</font></span></p>"
        "<p>第二段 <strong>加粗</strong> 与 <em>斜体</em></p><ul><li>甲</li><li>乙</li></ul>"
    )
    markdown = html_to_markdown(fragment)
    assert "## 第一节" in markdown
    assert "第一段\n\n第二段 **加粗** 与 *斜体*" in markdown
    assert "- 甲\n- 乙" in markdown


def test_html_to_markdown_resolves_links_and_images():
    markdown = html_to_markdown(
        '<p><a href="/uaapi/help">帮助</a><img src="images/a.png" alt="示意图"></p>',
        base_url="https://ua.dgut.edu.cn/",
    )
    assert "[帮助](https://ua.dgut.edu.cn/uaapi/help)" in markdown
    assert "![示意图](https://ua.dgut.edu.cn/images/a.png)" in markdown


def test_html_to_markdown_degrades_for_empty_or_plain_text():
    assert html_to_markdown("") == ""
    assert html_to_markdown("<p>只有文字</p>") == "只有文字"


def test_safe_name_strips_reserved_characters_and_keeps_extension():
    assert safe_name('第3章: 热处理/工艺?.pptx') == "第3章_ 热处理_工艺_.pptx"
    long_name = "很长的标题" * 30 + ".pdf"
    assert len(safe_name(long_name)) <= 80 and safe_name(long_name).endswith(".pdf")
    assert safe_name("") == ""


# ----------------------------------------------------------------- 解析
def test_directory_skips_hidden_chapters_and_sorts_numeric_node_ids():
    payload = directory([node("第二章", nodeid=102, order=2), node("隐藏", nodeid=1, hide=1),
                         node("第一章", nodeid=101, order=1)])
    course_name, chapters = parse_directory(payload)
    assert course_name == "形势与政策"
    assert [chapter["nodeid"] for chapter in chapters] == ["101", "102"]


def test_chapter_exports_rich_text_and_document_pages_only():
    payload = chapter_payload(chapter(
        page("图文页", 5),
        page("文档页", 7, blocks=[block("<p>文档正文</p>"), block(kind=6, questions=[{"id": 1}])]),
        page("视频页", 1, blocks=[block()]),
    ))
    pages = parse_chapter(payload, chapter_title="第一章", chapter_index=1)
    assert [item.title for item in pages] == ["图文页", "文档页"]
    assert [item.content_type for item in pages] == [5, 7]
    assert pages[1].skipped_blocks == 1


def test_quiz_blocks_are_never_rendered_or_reported_as_attachments():
    payload = chapter_payload(chapter(page("带测验的页面", 7, blocks=[
        block("<p>正文</p>"),
        block("<p>题干文字</p>", kind=6, questions=[{"content": "题干文字"}]),
        block("", kind=7, questions=[{"content": "判断题"}], resources=[{"url": "https://x/y"}]),
    ])))
    page_value = parse_chapter(payload, chapter_title="第一章", chapter_index=1)[0]
    assert "题干文字" not in page_value.markdown
    assert "判断题" not in page_value.markdown
    assert page_value.attachments == ()
    assert page_value.skipped_blocks == 2


def test_page_without_exportable_content_is_omitted():
    payload = chapter_payload(chapter(
        page("空页", 5, blocks=[block("")]),
        page("只有测验", 1, blocks=[block(kind=6, questions=[{}])]),
    ))
    assert parse_chapter(payload, chapter_title="第一章", chapter_index=1) == []


def test_attachment_is_read_from_resource_entry_or_data_file_attribute():
    payload = chapter_payload(chapter(page("附件页", 7, blocks=[
        block("<p>正文</p>", resources=[{"docTitle": "讲义.pdf", "resourceFullurl": "https://ua.dgut.edu.cn/a.pdf",
                                        "resourceContentSize": 2048}]),
        block("<div class='file-doc' data-file='https%3A%2F%2Fua.dgut.edu.cn%2Fb.pptx'></div>"),
    ])))
    page_value = parse_chapter(payload, chapter_title="第一章", chapter_index=1)[0]
    assert [item.name for item in page_value.attachments] == ["讲义.pdf", "b.pptx"]
    assert page_value.attachments[0].size == 2048
    assert "正文" in page_value.markdown


def test_nested_document_viewer_iframe_is_exported_as_attachment():
    viewer = ("https://docs.ulearning.cn?time=1&ssl=1&furl="
              "https%3A%2F%2Fuobs.dgut.edu.cn%2Fview%2Fresources%2Fweb%2Fexample-document.docx")
    payload = chapter_payload(chapter(page("文档页", 7, blocks=[
        block(f'<iframe class="doc-iframe" src="{viewer}"></iframe>'),
    ])))
    result = parse_chapter(payload, chapter_title="第一章", chapter_index=1)[0]
    assert [item.url for item in result.attachments] == [
        "https://uobs.dgut.edu.cn/view/resources/web/example-document.docx",
    ]
    assert result.attachments[0].name == "example-document.docx"
    # 查看器离线不可用，导出 HTML 换成指向真实文件的链接。
    assert "docs.ulearning.cn" not in result.html
    assert "example-document.docx" in result.html


def test_resource_entry_pointing_at_viewer_uses_nested_file():
    viewer = ("https://docs.ulearning.cn?time=1&ssl=1&furl="
              "https%3A%2F%2Fuobs.dgut.edu.cn%2Fview%2Fresources%2Fweb%2Fexample-document.docx")
    payload = chapter_payload(chapter(page("文档页", 7, blocks=[
        block("<p>正文</p>", resources=[{"docTitle": "任务书.docx", "resourceFullurl": viewer}]),
    ])))
    result = parse_chapter(payload, chapter_title="第一章", chapter_index=1)[0]
    assert result.attachments[0].name == "任务书.docx"
    assert result.attachments[0].url.endswith("example-document.docx")


def test_text_after_self_closing_viewer_is_kept():
    viewer = ("https://docs.ulearning.cn?time=1&ssl=1&furl="
              "https%3A%2F%2Fuobs.dgut.edu.cn%2Fview%2Fresources%2Fweb%2Fexample-document.docx")
    payload = chapter_payload(chapter(page("文档页", 7, blocks=[
        block(f'<embed src="{viewer}"/><p>看完继续</p>'),
    ])))
    result = parse_chapter(payload, chapter_title="第一章", chapter_index=1)[0]
    assert "看完继续" in result.html
    assert "docs.ulearning.cn" not in result.html


def test_viewer_without_furl_is_not_exported():
    payload = chapter_payload(chapter(page("文档页", 7, blocks=[
        block('<iframe src="https://docs.ulearning.cn?time=1&ssl=1"></iframe>'),
    ])))
    result = parse_chapter(payload, chapter_title="第一章", chapter_index=1)[0]
    assert result.attachments == ()


def test_block_metadata_and_inline_data_file_are_one_attachment():
    # 真实形状：块本身带 resourceFullurl / docTitle / 大小，正文里又嵌一份 data-file。
    duplicated = {
        "type": 10, "orderIndex": 1,
        "docTitle": "示例讲义.pptx",
        "resourceContentSize": 4096,
        "resourceFullurl": "https://uobs.dgut.edu.cn/view/resources/example/example-slides.pptx",
        "content": ("<div class='oc-block'><div class='file-doc' data-file='"
                    "https%3A//lms.dgut.edu.cn/uobs/view/resources/example/example-slides.pptx"
                    "'></div></div>"),
    }
    payload = chapter_payload(chapter(page("课件", 5, blocks=[duplicated])))
    result = parse_chapter(payload, chapter_title="第二章", chapter_index=3)[0]
    assert len(result.attachments) == 1
    assert result.attachments[0].name == "示例讲义.pptx"
    assert result.attachments[0].size == 4096


@pytest.mark.parametrize("path", ["x/a.pptx", "uobs/view/a.pptx"])
def test_same_path_on_unrelated_hosts_is_not_merged(path):
    block_value = {
        "type": 12, "orderIndex": 1, "content": "",
        "resourceDTOList": [
            {"docTitle": "a.pptx", "resourceFullurl": f"https://files.example.com/{path}",
             "resourceContentSize": 10},
            {"docTitle": "b.pptx", "resourceFullurl": f"https://mirror.example.org/{path}",
             "resourceContentSize": 10},
        ],
    }
    payload = chapter_payload(chapter(page("课件", 5, blocks=[block_value])))
    result = parse_chapter(payload, chapter_title="第二章", chapter_index=3)[0]
    assert len(result.attachments) == 2


def test_media_only_pages_and_all_embedded_files_are_localized(tmp_path):
    payload = chapter_payload(chapter(page("媒体", 1, blocks=[
        block(resources=[{"resourceFullurl": "https://ua.dgut.edu.cn/clip.mp4", "title": "视频.mp4"}]),
        block('<img src="/image.png"><audio src="/voice.mp3"></audio>'
              '<div data-file="https%3A%2F%2Fua.dgut.edu.cn%2Fa.pdf"></div>'
              '<div data-file="https%3A%2F%2Fua.dgut.edu.cn%2Fb.pdf"></div>'),
        block('<img src="/quiz.png">', kind=6),
    ])))
    value = parse_chapter(payload, chapter_title="章", chapter_index=1)[0]
    assert [item.name for item in value.attachments] == ["视频.mp4", "image.png", "voice.mp3", "a.pdf", "b.pdf"]
    library = CoursewareLibrary(tmp_path)
    course_dir = library.course_dir("课", "1")

    def download(url, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"sample")
        return 6

    record = library.write_page(course_dir, value, download=download)
    html = (course_dir / record["html"]).read_text(encoding="utf-8")
    assert 'src="_files/' in html and "quiz.png" not in html
    assert "https://ua.dgut.edu.cn/image.png" not in html
    assert len(record["files"]) == 5


def test_video_address_on_block_itself_is_exported():
    payload = chapter_payload(chapter(page("视频页", 6, blocks=[{
        "type": 4, "resourceFullurl": "https://ua.dgut.edu.cn/sample.mp4", "resourceContentSize": 123,
        "srtDTO": {"title": "字幕.srt", "locationFullurl": "https://lms.dgut.edu.cn/uobs/view/subtitle.srt"},
    }])))
    value = parse_chapter(payload, chapter_title="专题一", chapter_index=1)[0]
    assert value.attachments == (ExportAttachment("sample.mp4", "https://ua.dgut.edu.cn/sample.mp4", 123),
                                 ExportAttachment("字幕.srt", "https://lms.dgut.edu.cn/uobs/view/subtitle.srt"))


@pytest.mark.parametrize("prefix", ["", "http://leicloud.ulearning.cn/", "http://leicloud.qiniudn.com/",
                                    "https://tskcloud.ulearning.cn/"])
def test_ua_storage_paths_follow_platform_resource_mapping(prefix):
    payload = chapter_payload(chapter(page("文档", 7, blocks=[{
        "type": 10, "resourceFullurl": prefix + "resources/handout.pdf",
    }])))
    value = parse_chapter(payload, chapter_title="章", chapter_index=1)[0]
    assert value.attachments[0].url == "https://lms.dgut.edu.cn/uobs/view/resources/handout.pdf"


def test_resource_area_paginates_recurses_and_excludes_questions():
    calls = []

    def fetch(url, **kwargs):
        params = kwargs["params"]
        calls.append((url, params))
        if url.endswith("/content/v2"):
            if params["parentId"] == "folder":
                return {"list": [{"id": "nested", "title": "音频", "type": 2}], "total": 1}
            if params["pn"] == 1:
                return {"list": [{"id": "folder", "title": "目录", "type": 0},
                                 {"id": "quiz", "title": "试题", "type": 11}] + [
                    {"id": str(i), "title": f"资料{i}", "type": 4} for i in range(48)], "total": 51}
            return {"list": [{"id": "last", "title": "外链", "type": 7}], "total": 51}
        if url.endswith("/last"):
            return {"type": 2, "contentType": 7, "location": "https://example.invalid/lecture"}
        return {"type": 1, "contentType": 2 if url.endswith("/nested") else 4,
                "title": "资料", "mimeType": "mp3" if url.endswith("/nested") else "pdf",
                "location": "resources/sample", "contentSize": 12}

    result = export_resources("42", fetch)
    assert len(result.pages) == 50 and not result.failures
    assert result.pages[0].chapter == "课程资源 / 目录"
    assert result.pages[0].attachments[0].url == "https://uobs.dgut.edu.cn/view/resources/sample"
    assert result.pages[0].attachments[0].name == "资料.mp3"
    assert result.pages[-1].attachments == ()
    assert "https://example.invalid/lecture" in result.pages[-1].markdown
    assert not any(url.endswith("/quiz") for url, _ in calls)
    assert any(params.get("pn") == 2 for _, params in calls)


def test_resource_details_failure_is_reported_and_other_files_continue():
    fetch = Mock(side_effect=[
        {"list": [{"id": 1, "title": "失败资料", "type": 4}, {"id": 2, "title": "图片", "type": 3}], "total": 2},
        RuntimeError("HTTP 500"),
        {"type": 1, "contentType": 3, "title": "图.png", "mimeType": "png", "location": "a.png"},
    ])
    result = export_resources("42", fetch)
    assert result.pages[0].resource_id == "2"
    assert result.failures == ({"resource": "失败资料", "reason": "HTTP 500"},)


def test_resource_pagination_uses_returned_page_size():
    detail = {"type": 1, "contentType": 4, "location": "a.pdf", "title": "讲义.pdf"}
    fetch = Mock(side_effect=[
        {"list": [{"id": 1, "type": 4}, {"id": 2, "type": 4}], "total": 3, "pageSize": 2},
        detail, detail,
        {"list": [{"id": 3, "type": 4}], "total": 3, "pageSize": 2}, detail,
    ])
    assert len(export_resources("42", fetch).pages) == 3
    assert fetch.call_args_list[3].kwargs["params"]["pn"] == 2


def test_course_list_fetches_later_pages():
    backend = SignBackend.__new__(SignBackend)
    backend.api = Mock()
    backend.api.json.side_effect = [{"courseList": [{"id": i} for i in range(50)]},
                                    {"courseList": [{"id": 50}]}]
    assert len(backend._fetch_courses()) == 51
    assert backend.api.json.call_args_list[-1].kwargs["params"]["pn"] == 2


def test_missing_node_id_payload_degrades_to_empty_plan():
    assert parse_directory({}) == ("", [])
    assert parse_directory(None) == ("", [])
    assert parse_chapter(None, chapter_title="x", chapter_index=1) == []


# ----------------------------------------------------------------- 资源库
def test_library_reuses_course_folder_and_suffixes_collisions():
    with tempfile.TemporaryDirectory() as directory_path:
        library = CoursewareLibrary(Path(directory_path))
        first = library.course_dir("形势与政策", "42")
        second = library.course_dir("形势与政策", "42")
        other = library.course_dir("形势与政策", "43")
        assert first == second and first.name == "形势与政策"
        assert other != first and other.name == "形势与政策 (43)"
        assert json.loads((first / ".dgutbot-courseware.json").read_text(encoding="utf-8"))["id"] == "42"


def test_library_never_takes_over_untracked_folder():
    with tempfile.TemporaryDirectory() as directory_path:
        root = Path(directory_path)
        (root / "形势与政策").mkdir()
        library = CoursewareLibrary(root)
        target = library.course_dir("形势与政策", "42")
        assert target.name == "形势与政策 (42)"
        assert not (root / "形势与政策" / ".dgutbot-courseware.json").exists()


def test_library_writes_markdown_html_and_attachments():
    with tempfile.TemporaryDirectory() as directory_path:
        root = Path(directory_path)
        library = CoursewareLibrary(root)
        course_dir = library.course_dir("形势与政策", "42")
        page_value = ExportPage(
            chapter="第一章", chapter_index=1, page_index=1, title="第一节", content_type=7,
            markdown="# 第一节", html="<h3>第一节</h3>",
            attachments=(ExportAttachment(name="讲义.pdf", url="https://ua.dgut.edu.cn/a.pdf"),),
        )
        written = []
        record = library.write_page(course_dir, page_value, download=lambda url, dest: written.append(dest) or 10)
        assert record["written"] == 2
        assert record["files"] == ["01_第一章/_files/01/讲义.pdf"]
        markdown = (course_dir / "01_第一章" / "01_第一节.md").read_text(encoding="utf-8")
        assert markdown.startswith("# 第一节\n")
        assert "[讲义.pdf](_files/01/讲义.pdf)" in markdown
        assert written and written[0].name == "讲义.pdf"


def test_duplicate_attachment_names_are_numbered():
    with tempfile.TemporaryDirectory() as directory_path:
        library = CoursewareLibrary(Path(directory_path))
        course_dir = library.course_dir("课", "1")
        attachment = ExportAttachment(name="a.pdf", url="https://ua.dgut.edu.cn/a.pdf")
        page_value = ExportPage(
            chapter="第一章", chapter_index=1, page_index=1, title="第一节", content_type=7,
            markdown="正文", html="<p>正文</p>", attachments=(attachment, attachment),
        )
        record = library.write_page(course_dir, page_value, download=lambda url, dest: 1)
        assert record["files"] == ["01_第一章/_files/01/a.pdf", "01_第一章/_files/01/a (2).pdf"]


def test_existing_attachment_with_matching_size_is_not_downloaded_again():
    with tempfile.TemporaryDirectory() as directory_path:
        library = CoursewareLibrary(Path(directory_path))
        course_dir = library.course_dir("课", "1")
        target = course_dir / "01_第一章" / "_files" / "01" / "a.pdf"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"12345")
        page_value = ExportPage(
            chapter="第一章", chapter_index=1, page_index=1, title="第一节", content_type=7,
            markdown="正文", html="<p>正文</p>",
            attachments=(ExportAttachment(name="a.pdf", url="https://ua.dgut.edu.cn/a.pdf", size=5),),
        )
        download = Mock(side_effect=AssertionError("不应重复下载"))
        record = library.write_page(course_dir, page_value, download=download)
        download.assert_not_called()
        assert record["files"] == ["01_第一章/_files/01/a.pdf"]


def test_index_escapes_spaces_and_parentheses_in_links():
    with tempfile.TemporaryDirectory() as directory_path:
        library = CoursewareLibrary(Path(directory_path))
        course_dir = library.course_dir("课", "1")
        library.write_index(course_dir, course_name="课", records=[{
            "chapter": "第一章（节选）", "title": "第一节 (试行)", "path": "01_第一章（节选）/01_第一节 (试行).md",
        }])
        index = (course_dir / "index.md").read_text(encoding="utf-8")
        assert "(01_第一章（节选）/01_第一节%20%28试行%29.md)" in index


def test_attachment_failure_keeps_page_and_records_reason():
    with tempfile.TemporaryDirectory() as directory_path:
        library = CoursewareLibrary(Path(directory_path))
        course_dir = library.course_dir("课", "1")
        page_value = ExportPage(
            chapter="第一章", chapter_index=1, page_index=1, title="第一节", content_type=7,
            markdown="正文", html="<p>正文</p>",
            attachments=(ExportAttachment(name="a.pdf", url="https://ua.dgut.edu.cn/a.pdf"),),
        )
        record = library.write_page(course_dir, page_value, download=Mock(side_effect=OSError("HTTP 403")))
        assert record["written"] == 2 and record["files"] == []
        assert record["failures"][0]["reason"] == "HTTP 403"
        assert (course_dir / "01_第一章" / "01_第一节.md").exists()


# ----------------------------------------------------------------- 服务
def service_for(root, chapters, pages, *, download=None, redact=str):
    return CoursewareExportService(
        root=root,
        directory=lambda course: directory(chapters),
        chapter=lambda node_id: pages[node_id],
        download=download,
        redact=redact,
    )


def test_export_writes_pages_manifest_and_index():
    with tempfile.TemporaryDirectory() as directory_path:
        root = Path(directory_path)
        pages = {"101": chapter_payload(chapter(page("第一节", 5)))}
        service = service_for(root, [node("第一章")], pages)
        service.start(FakeCourse())
        result = wait(service)
        assert result["state"] == "completed"
        assert (result["chapterDone"], result["chapterTotal"]) == (1, 1)
        assert result["pageTotal"] == 1 and result["pageWritten"] == 1
        course_dir = Path(result["folder"])
        manifest = json.loads((course_dir / "manifest.json").read_text(encoding="utf-8"))
        index = (course_dir / "index.md").read_text(encoding="utf-8")
        assert manifest["state"] == "completed" and manifest["pages"][0]["title"] == "第一节"
        assert "## 第一章" in index and "01_第一节.md" in index


def test_second_export_reports_unchanged_files():
    with tempfile.TemporaryDirectory() as directory_path:
        root = Path(directory_path)
        pages = {"101": chapter_payload(chapter(page("第一节", 5)))}
        first_service = service_for(root, [node("第一章")], pages)
        first_service.start(FakeCourse())
        first = wait(first_service)
        second_service = service_for(root, [node("第一章")], pages)
        second_service.start(FakeCourse())
        second = wait(second_service)
        assert (first["pageWritten"], first["pageUnchanged"]) == (1, 0)
        assert (second["pageWritten"], second["pageUnchanged"]) == (0, 1)


def test_chapter_failure_is_isolated_and_redacted():
    chapters = [node("第一章", nodeid=1, order=1), node("第二章", nodeid=2, order=2)]
    pages = {"1": chapter_payload(chapter(page("第一节", 5)))}
    service = service_for(
        Path(tempfile.mkdtemp()), chapters, pages,
        redact=lambda value: str(value).replace("secret", "[已隐藏]"),
    )
    service._chapter = lambda node_id: pages.get(node_id) or _fail(RuntimeError("Authorization: secret"))
    service.start(FakeCourse())
    result = wait(service)
    assert result["state"] == "completed"
    assert result["pageTotal"] == 1
    assert result["failures"][0]["reason"] == "Authorization: [已隐藏]"


def test_auth_failure_stops_export_with_explicit_state():
    service = service_for(Path(tempfile.mkdtemp()), [node("第一章", nodeid=1)], {})
    service._chapter = lambda node_id: _fail(AuthRequiredError("登录已失效"))
    service.start(FakeCourse())
    result = wait(service)
    assert result["state"] == "auth_required" and result["pageTotal"] == 0


def test_unexpected_failure_lands_in_error_state():
    service = service_for(Path(tempfile.mkdtemp()), [node("第一章", nodeid=1)], {"1": {}})
    with patch("dgutbot.domain.courseware_export.parse_chapter", side_effect=RuntimeError("boom")):
        service.start(FakeCourse())
        result = wait(service)
    assert result["state"] == "error" and "boom" in result["error"]


def test_missing_login_does_not_start_worker():
    service = CoursewareExportService(
        root=Path(tempfile.mkdtemp()), directory=lambda course: {}, chapter=lambda node_id: {},
        authenticated=lambda: False,
    )
    assert service.start(FakeCourse())["state"] == "auth_required"


def test_cancel_stops_before_next_chapter_and_keeps_written_pages():
    with tempfile.TemporaryDirectory() as directory_path:
        root = Path(directory_path)
        release = threading.Event()
        chapters = [node("第一章", nodeid=1, order=1), node("第二章", nodeid=2, order=2)]
        service = CoursewareExportService(
            root=root, directory=lambda course: directory(chapters),
            chapter=lambda node_id: release.wait(1) or chapter_payload(chapter(page("第一节", 5))),
        )
        service.start(FakeCourse())
        service.cancel()
        release.set()
        result = wait(service)
        assert result["state"] == "cancelled"
        assert result["pageTotal"] <= 1
        manifest = json.loads((Path(result["folder"]) / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["state"] == "cancelled"


def test_batch_runs_serially_and_keeps_each_course_result(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fetch(course):
        calls.append(course.id)
        if course.id == "1":
            entered.set()
            release.wait(3)
        return directory([node()], course.name)

    service = CoursewareExportService(root=tmp_path, directory=fetch,
        chapter=lambda _: chapter_payload(chapter(page("正文"))))
    service.start_many([FakeCourse("1", "甲"), FakeCourse("2", "乙")])
    assert entered.wait(3)
    progress = service.snapshot()
    assert progress["state"] == "running"
    assert [item["state"] for item in progress["results"]] == ["running", "queued"]
    assert calls == ["1"]
    # 重复启动不会替换运行中的队列。
    assert service.start(FakeCourse("3", "丙"))["courseTotal"] == 2
    release.set()
    result = wait(service)
    assert calls == ["1", "2"]
    assert result["state"] == "completed" and result["courseDone"] == 2
    assert [item["pageTotal"] for item in result["results"]] == [1, 1]
    assert all(Path(item["folder"], "manifest.json").exists() for item in result["results"])


@pytest.mark.parametrize("mode", ["failure", "auth", "cancel"])
def test_batch_continues_after_error_but_stops_on_auth_or_cancel(tmp_path, mode):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fetch(course):
        calls.append(course.id)
        if course.id == "1":
            if mode == "cancel":
                entered.set()
                release.wait(3)
            else:
                raise AuthRequiredError("登录失效") if mode == "auth" else RuntimeError("课程暂不可用")
        return directory([], course.name)

    service = CoursewareExportService(root=tmp_path, directory=fetch, chapter=lambda _: {})
    service.start_many([FakeCourse("1", "甲"), FakeCourse("2", "乙")])
    if mode == "cancel":
        assert entered.wait(3)
        assert service.cancel()["state"] == "cancelling"
        release.set()
    result = wait(service)
    expected = {"failure": ("completed", ["error", "completed"]),
                "auth": ("auth_required", ["auth_required", "skipped"]),
                "cancel": ("cancelled", ["cancelled", "skipped"])}
    assert (result["state"], [item["state"] for item in result["results"]]) == expected[mode]
    assert calls == (["1", "2"] if mode == "failure" else ["1"])


def test_backend_batch_validates_all_ids_and_keeps_sign_selection(tmp_path):
    backend = SignBackend(lambda *_: None, root=tmp_path)
    backend.courses = [Course(1, "同名"), Course(2, "同名")]
    backend.selected_course = backend.courses[1]
    backend.courseware_export = Mock()
    with pytest.raises(ValueError):
        backend.start_courseware_exports(["1", "404"])
    backend.courseware_export.start_many.assert_not_called()
    with pytest.raises(ValueError):
        backend.start_courseware_exports([])
    backend.start_courseware_exports(["1", "2", "1"])
    backend.courseware_export.start_many.assert_called_once_with(backend.courses)
    assert backend.selected_course is backend.courses[1]


# ----------------------------------------------------------------- 后端接线
def test_all_textbooks_and_resource_area_are_in_export_directory():
    fetch = Mock(side_effect=[
        [{"courseId": 900, "name": "教材甲"}, {"courseId": 901, "name": "教材乙"}],
        {"classId": 300}, directory([node("章甲", 111)]), directory([node("章乙", 222)]),
    ])
    payload = export_directory(Course(42, "课程"), fetch)
    _, chapters = parse_directory(payload)
    assert [item["nodeid"] for item in chapters] == ["resources:42", "111", "222"]
    assert [item["title"] for item in chapters] == ["课程资源", "教材甲 / 章甲", "教材乙 / 章乙"]
    assert fetch.call_args_list[2].kwargs == {"params": {"classId": "300"}, "ua": True}


def test_course_without_textbooks_still_exports_its_resources():
    fetch = Mock(return_value=[])
    payload = export_directory(Course(42, "课程"), fetch)
    assert parse_directory(payload)[1][0]["nodeid"] == "resources:42"
    assert not payload["failures"]
    assert fetch.call_count == 1


def test_textbook_list_failure_keeps_independent_resource_area():
    payload = export_directory(Course(42, "课程"), Mock(side_effect=RuntimeError("HTTP 500")))
    assert parse_directory(payload)[1][0]["nodeid"] == "resources:42"
    assert payload["failures"] == [{"chapter": "学习页教材列表", "reason": "HTTP 500"}]


def test_backend_chapter_request_sends_ua_authorization_header():
    class FakeWs:
        def __init__(self):
            self.calls = []

        def settimeout(self, _timeout):
            pass

        def send(self, raw):
            self.calls.append(json.loads(raw))

        def recv(self):
            call = self.calls[-1]
            value = {"status": 200, "text": "{}", "contentType": "application/json"}
            result = ({"objectId": "object-1"} if call["method"] == "Runtime.evaluate"
                      else {"value": value})
            return json.dumps({"id": call["id"], "result": {"result": result}})

        def close(self):
            pass

    with tempfile.TemporaryDirectory() as directory_path:
        backend = SignBackend(lambda *_: None, root=Path(directory_path))
        backend.token = "test-token"
        fake = FakeWs()
        with patch("dgutbot.domain.yxy_backend.create_connection", return_value=fake), \
                patch.object(BrowserApiClient, "_list_targets", return_value=[{
                    "type": "page", "url": "https://ua.dgut.edu.cn/x", "webSocketDebuggerUrl": "ws://x",
                }]):
            assert backend._fetch_courseware_chapter("101") == {}
        call = next(item for item in fake.calls if item["method"] == "Runtime.callFunctionOn")
        request = call["params"]["arguments"][0]["value"]
        assert request["url"] == f"{UA_BASE}/wholepage/chapter/stu/101"
        assert request["headers"]["ua-authorization"] == "test-token"


def test_backend_start_requires_course_in_current_list():
    with tempfile.TemporaryDirectory() as directory_path:
        backend = SignBackend(lambda *_: None, root=Path(directory_path))
        try:
            backend.start_courseware_export("404")
        except ValueError as error:
            assert "课程" in str(error)
        else:
            raise AssertionError("未知课程必须被拒绝")


# ----------------------------------------------------------------- 审查发现的行为回归
@pytest.mark.parametrize("size", [0, 5])
def test_cross_page_attachment_names_keep_both_contents(tmp_path, size):
    library = CoursewareLibrary(tmp_path)
    course_dir = library.course_dir("课程", "42")
    records = []

    def download(url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"first" if url.endswith("one.pdf") else b"other")
        return 5

    for index, url in enumerate(("https://ua.dgut.edu.cn/one.pdf", "https://ua.dgut.edu.cn/two.pdf"), 1):
        value = ExportPage("章", 1, index, f"页{index}", 7, "正文", "<p>正文</p>",
                           (ExportAttachment("讲义.pdf", url, size),))
        records.append(library.write_page(course_dir, value, download=download))
    first, second = (course_dir / record["files"][0] for record in records)
    assert first != second
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"other"


def test_downloaded_attachment_links_resolve_locally_in_both_formats(tmp_path):
    url = "https://ua.dgut.edu.cn/notes.pdf"
    value = parse_chapter(chapter_payload(chapter(page("页", 7, blocks=[
        block(f'<a href="{url}">讲义</a>', resources=[{"name": "讲义 (一).pdf", "url": url}]),
    ]))), chapter_title="章", chapter_index=1)[0]
    library = CoursewareLibrary(tmp_path)
    course_dir = library.course_dir("课程", "42")

    def download(_url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination.write_bytes(b"document")

    record = library.write_page(course_dir, value, download=download)
    target = course_dir / record["files"][0]
    local = target.relative_to((course_dir / record["path"]).parent).as_posix()
    for key in ("path", "html"):
        text = (course_dir / record[key]).read_text(encoding="utf-8")
        assert url not in text
        assert unquote(text).find(local) >= 0
    assert target.read_bytes() == b"document"


def test_attachment_errors_are_redacted_before_snapshot_and_manifest(tmp_path):
    service = service_for(tmp_path, [node()], {
        "101": chapter_payload(chapter(page("页", 7, blocks=[
            block("正文", resources=[{"url": "https://ua.dgut.edu.cn/a.pdf"}]),
        ]))),
    }, download=Mock(side_effect=RuntimeError("Authorization: synthetic-secret")),
        redact=lambda value: str(value).replace("synthetic-secret", "[已隐藏]"))
    service.start(FakeCourse())
    result = wait(service)
    manifest = (Path(result["folder"]) / "manifest.json").read_text(encoding="utf-8")
    assert "synthetic-secret" not in str(result["failures"])
    assert "synthetic-secret" not in manifest
    assert "[已隐藏]" in manifest


@pytest.mark.parametrize("error", [RuntimeError("HTTP 401"), BrowserLoginRedirectError("登录跳转")])
def test_identity_login_rejection_uses_auth_required(error):
    backend = SignBackend.__new__(SignBackend)
    backend.headers = {}
    backend.config = Mock(debug_port=9222)
    fake = Mock()
    fake.request.side_effect = error
    with patch("dgutbot.domain.yxy_backend.BrowserApiClient", return_value=fake):
        with pytest.raises(AuthRequiredError):
            backend._courseware_directory_payload(Course(42, "课程"))


@pytest.mark.parametrize("mode", ["failure", "auth", "cancel"])
def test_rerun_keeps_previous_catalog_for_unfinished_chapters(tmp_path, mode):
    chapters = [node("第一章", nodeid=1), node("第二章", nodeid=2, order=2)]
    pages = {str(index): chapter_payload(chapter(page(f"原有页{index}", 5))) for index in (1, 2)}
    first = service_for(tmp_path, chapters, pages)
    first.start(FakeCourse())
    assert wait(first)["state"] == "completed"
    entered = threading.Event()
    release = threading.Event()

    def fetch(node_id):
        if node_id == "1":
            return pages[node_id]
        if mode == "cancel":
            entered.set()
            release.wait(3)
            return pages[node_id]
        raise AuthRequiredError("登录失效") if mode == "auth" else RuntimeError("章节暂不可用")

    second = service_for(tmp_path, chapters, pages)
    second._chapter = fetch
    second.start(FakeCourse())
    if mode == "cancel":
        assert entered.wait(3)
        second.cancel()
        release.set()
    result = wait(second)
    course_dir = Path(result["folder"])
    manifest = json.loads((course_dir / "manifest.json").read_text(encoding="utf-8"))
    assert {record["title"] for record in manifest["pages"]} == {"原有页1", "原有页2"}
    assert "原有页2" in (course_dir / "index.md").read_text(encoding="utf-8")
    if mode == "failure":
        assert manifest["failures"][0]["reason"] == "章节暂不可用"
    else:
        assert manifest["state"] == ("auth_required" if mode == "auth" else "cancelled")


class DownloadAdapter(requests.adapters.BaseAdapter):
    """使用真正的 Requests 重定向流程，响应全部来自内存。"""

    def __init__(self, *, redirect=False, content=b"document"):
        self.calls = []
        self.redirect = redirect
        self.content = content

    def send(self, request, **kwargs):
        self.calls.append(request)
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = 302 if self.redirect and len(self.calls) == 1 else 200
        response.raw = io.BytesIO(self.content)
        if response.status_code == 302:
            response.headers["Location"] = "https://files.example.invalid/a.pdf"
        return response

    def close(self):
        pass


@pytest.mark.parametrize("url,trusted", [
    ("https://ua.dgut.edu.cn/a.pdf", True),
    ("https://evildgut.edu.cn/a.pdf", False),
    ("http://ua.dgut.edu.cn/a.pdf", False),
])
def test_download_only_attaches_credentials_to_trusted_https(tmp_path, url, trusted):
    backend = SignBackend.__new__(SignBackend)
    backend.token = "synthetic-review-token"
    backend.headers = {"Authorization": backend.token}
    adapter = DownloadAdapter()
    with patch.object(requests.Session, "get_adapter", return_value=adapter):
        backend._download_courseware_file(url, tmp_path / "a.pdf")
    headers = adapter.calls[0].headers
    assert ("Authorization" in headers) == trusted
    assert ("ua-authorization" in headers) == trusted
    assert (tmp_path / "a.pdf").read_bytes() == b"document"


def test_download_redirect_drops_both_credential_headers(tmp_path):
    backend = SignBackend.__new__(SignBackend)
    backend.token = "synthetic-review-token"
    backend.headers = {"Authorization": backend.token}
    adapter = DownloadAdapter(redirect=True)
    with patch.object(requests.Session, "get_adapter", return_value=adapter):
        backend._download_courseware_file("https://ua.dgut.edu.cn/a.pdf", tmp_path / "a.pdf")
    assert len(adapter.calls) == 2
    assert "Authorization" not in adapter.calls[1].headers
    assert "ua-authorization" not in adapter.calls[1].headers


@pytest.mark.parametrize("url,query_token", [
    ("https://lms.dgut.edu.cn/uobs/view/resources/a.mp4?quality=1", True),
    ("https://uobs.dgut.edu.cn/view/resources/a.pdf?token=old", True),
    ("https://cdn.example.invalid/a.mp4", False),
])
def test_school_resource_requests_use_platform_query_auth(tmp_path, url, query_token):
    from urllib.parse import parse_qs, urlsplit
    from dgutbot.domain.courseware_download import download_file
    adapter = DownloadAdapter()
    with patch.object(requests.Session, "get_adapter", return_value=adapter):
        download_file(url, tmp_path / "sample", token="test-token")
    query = parse_qs(urlsplit(adapter.calls[0].url).query)
    assert query.get("token") == (["test-token"] if query_token else None)
    if "quality=1" in url:
        assert query["quality"] == ["1"]


def test_http_200_file_error_is_not_saved_as_courseware(tmp_path):
    from dgutbot.domain.courseware_download import download_file
    adapter = DownloadAdapter(content=b'{"error":"get file error"}')
    target = tmp_path / "lecture.ppt"
    with patch.object(requests.Session, "get_adapter", return_value=adapter):
        with pytest.raises(RuntimeError, match="get file error"):
            download_file("https://uobs.dgut.edu.cn/view/a.ppt", target, token="")
    assert not target.exists() and not target.with_suffix(".ppt.part").exists()


def test_failed_attachment_refresh_keeps_previous_offline_link(tmp_path):
    from dgutbot.domain.courseware_download import download_file
    library = CoursewareLibrary(tmp_path)
    course_dir = library.course_dir("课程", "42")
    value = ExportPage("章", 1, 1, "视频", 6, "正文", "<p>正文</p>",
                       (ExportAttachment("clip.mp4", "https://uobs.dgut.edu.cn/view/clip.mp4", 100),))
    old = course_dir / value.folder / "_files" / "01" / "clip.mp4"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"previous-media")
    adapter = DownloadAdapter(content=b'{"error":"get file error"}')
    with patch.object(requests.Session, "get_adapter", return_value=adapter):
        record = library.write_page(course_dir, value, download=lambda url, dest: download_file(url, dest, token=""))
    assert old.read_bytes() == b"previous-media"
    assert record["files"] == [old.relative_to(course_dir).as_posix()]
    assert "_files/01/clip.mp4" in (course_dir / record["path"]).read_text(encoding="utf-8")
    assert "get file error" in record["failures"][0]["reason"]


def test_missing_original_video_uses_player_mp4_and_keeps_names_unique(tmp_path):
    from dgutbot.domain.courseware_download import download_file
    source = export_resources("42", Mock(side_effect=[
        {"list": [{"id": 1, "title": "clip.wmv", "type": 1}], "total": 1},
        {"type": 1, "contentType": 1, "title": "clip.wmv", "mimeType": "wmv", "location": "resources/clip.wmv"},
    ])).pages[0]
    value = replace(source, attachments=source.attachments + (
        ExportAttachment("clip.mp4", "https://uobs.dgut.edu.cn/view/resources/other.mp4"),
    ))

    class VideoAdapter(DownloadAdapter):
        def send(self, request, **kwargs):
            self.content = (b'{"error":"Document not found"}' if request.url.endswith('.wmv') else
                            b'playback' if request.url.endswith('/clip.mp4') else b'other-video')
            return super().send(request, **kwargs)

    library = CoursewareLibrary(tmp_path)
    course_dir = library.course_dir("课", "42")
    with patch.object(requests.Session, "get_adapter", return_value=VideoAdapter()):
        record = library.write_page(course_dir, value, download=lambda url, dest: download_file(url, dest, token=""))
    assert record["failures"] == []
    assert [Path(name).name for name in record["files"]] == ["clip.mp4", "clip (2).mp4"]
    assert [(course_dir / name).read_bytes() for name in record["files"]] == [b"playback", b"other-video"]



@pytest.mark.parametrize("segment_type", ["mpegts", "fmp4"])
def test_hls_download_remuxes_real_segments_and_localizes_external_hosts(tmp_path, segment_type):
    from dgutbot.domain.courseware_download import download_file
    if not shutil.which("ffmpeg"):
        pytest.skip("HLS 媒体验证需要 FFmpeg")
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=32x32:rate=4", "-t", "1", "-c:v", "libx264", "-threads", "1",
                    "-pix_fmt", "yuv420p", "-f", "hls", "-hls_segment_type", segment_type,
                    "-hls_time", "1", "clip.m3u8"], check=True, cwd=source)
    playlist = (source / "clip.m3u8").read_text()
    # 让分段走外部 CDN；不应继承校内鉴权。
    playlist = re.sub(r"(?m)^(?!#)(\S+)$", r"https://cdn.example.invalid/\1", playlist)
    if segment_type == "fmp4":
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=44100", "-t", "1", "-c:a", "aac",
                        "-f", "hls", "-hls_time", "1", "audio.m3u8"], check=True, cwd=source)

    class HlsAdapter(DownloadAdapter):
        def send(self, request, **kwargs):
            filename = Path(urlsplit(request.url).path).name
            self.content = (playlist.encode() if filename == "clip.m3u8" else
                            b'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",DEFAULT=YES,URI="audio.m3u8"\n'
                            b'#EXT-X-STREAM-INF:BANDWIDTH=1000,AUDIO="a"\nclip.m3u8\n'
                            if filename == "master.m3u8" else (source / filename).read_bytes())
            return super().send(request, **kwargs)

    adapter = HlsAdapter()
    target = tmp_path / "out.mp4"
    with patch.object(requests.Session, "get_adapter", return_value=adapter):
        entry = "master.m3u8" if segment_type == "fmp4" else "clip.m3u8"
        assert download_file(f"https://ua.dgut.edu.cn/{entry}", target, token="synthetic-token") > 0
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(target), "-f", "null", "-"], check=True)
    if segment_type == "fmp4":
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(target), "-map", "0:a", "-f", "null", "-"], check=True)
    assert any("cdn.example.invalid" in call.url for call in adapter.calls)
    for call in adapter.calls:
        if "cdn.example.invalid" in call.url:
            assert "Authorization" not in call.headers and "ua-authorization" not in call.headers
    assert not list(tmp_path.glob(".hls-*"))


def test_hls_selects_highest_variant_and_default_audio(tmp_path):
    from dgutbot.domain.courseware_download import download_file
    master = (b'#EXTM3U\n#EXT-X-MEDIA:GROUP-ID="a",URI="audio.m3u8",TYPE=AUDIO,DEFAULT=YES\n'
              b'#EXT-X-STREAM-INF:BANDWIDTH=100,AUDIO="a"\nlow.m3u8\n'
              b'#EXT-X-STREAM-INF:BANDWIDTH=200,AUDIO="a"\nhigh.m3u8\n')
    body = b'#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n'

    class HlsAdapter(DownloadAdapter):
        def send(self, request, **kwargs):
            self.content = master if request.url.endswith("master.m3u8") else body if request.url.endswith(".m3u8") else b"segment"
            return super().send(request, **kwargs)

    def remux(args, **kwargs):
        source = Path(args[args.index("-i") + 1])
        text = source.read_text()
        assert "#EXT-X-MEDIA:" in text and "URI=\"playlist-" in text
        assert "https://" not in text
        Path(args[-1]).write_bytes(b"merged")
        return Mock(returncode=0)

    adapter = HlsAdapter()
    with patch.object(requests.Session, "get_adapter", return_value=adapter), \
            patch("dgutbot.domain.courseware_download.shutil.which", return_value="ffmpeg"), \
            patch("dgutbot.domain.courseware_download.subprocess.run", side_effect=remux):
        assert download_file("https://ua.dgut.edu.cn/master.m3u8", tmp_path / "out.mp4", token="") == 6
    assert any(call.url.endswith("high.m3u8") for call in adapter.calls)
    assert any(call.url.endswith("audio.m3u8") for call in adapter.calls)
    assert not any(call.url.endswith("low.m3u8") for call in adapter.calls)


def test_unfinished_hls_keeps_previous_file(tmp_path):
    from dgutbot.domain.courseware_download import download_file
    target = tmp_path / "out.mp4"
    target.write_bytes(b"previous")
    with patch.object(requests.Session, "get_adapter", return_value=DownloadAdapter(content=b"#EXTM3U\n")), \
            patch("dgutbot.domain.courseware_download.shutil.which", return_value="ffmpeg"):
        with pytest.raises(RuntimeError, match="尚未完结"):
            download_file("https://ua.dgut.edu.cn/clip.m3u8", target, token="")
    assert target.read_bytes() == b"previous" and not (tmp_path / "out.mp4.part").exists()


def test_aliases_keep_offline_links_after_deduplication(tmp_path):
    payload = chapter_payload(chapter(page("别名附件", blocks=[block(
        '<a href="https://lms.dgut.edu.cn/uobs/view/a.pdf?ts=1">查看文件</a>',
        resources=[{"name": "讲义.pdf", "url": "https://uobs.dgut.edu.cn/view/a.pdf", "size": 1}],
    )])))
    value = parse_chapter(payload, chapter_title="章", chapter_index=1)[0]
    library = CoursewareLibrary(tmp_path)
    course_dir = library.course_dir("课", "1")
    def save(_url, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"a")
    record = library.write_page(course_dir, value, download=save)
    assert len(record["files"]) == 1
    for key in ("path", "html"):
        rendered = (course_dir / record[key]).read_text(encoding="utf-8")
        assert "lms.dgut.edu.cn" not in rendered and "uobs.dgut.edu.cn" not in rendered
