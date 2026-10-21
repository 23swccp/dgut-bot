"""课件导出的离线单元测试：全部使用合成或脱敏样本，不访问网络。"""

import json
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch

from dgutbot.domain.courseware_export import (
    CoursewareExportService, CoursewareLibrary, ExportAttachment, ExportPage,
    UA_BASE, html_to_markdown, parse_chapter, parse_directory, safe_name,
)
from dgutbot.domain.course_scan import AuthRequiredError
from dgutbot.domain.yxy_backend import BrowserApiClient, Course, SignBackend


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


def node(title="第一章", nodeid=2602858, order=1, hide=0):
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
    payload = directory([node("第二章", nodeid=2602882, order=2), node("隐藏", nodeid=1, hide=1),
                         node("第一章", nodeid=2602858, order=1)])
    course_name, chapters = parse_directory(payload)
    assert course_name == "形势与政策"
    assert [chapter["nodeid"] for chapter in chapters] == ["2602858", "2602882"]


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
    assert "## 附件" in page_value.markdown


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
        assert record["files"] == ["01_第一章/_files/讲义.pdf"]
        assert (course_dir / "01_第一章" / "01_第一节.md").read_text(encoding="utf-8") == "# 第一节\n"
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
        assert record["files"] == ["01_第一章/_files/a.pdf", "01_第一章/_files/a (2).pdf"]


def test_existing_attachment_with_matching_size_is_not_downloaded_again():
    with tempfile.TemporaryDirectory() as directory_path:
        library = CoursewareLibrary(Path(directory_path))
        course_dir = library.course_dir("课", "1")
        target = course_dir / "01_第一章" / "_files" / "a.pdf"
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
        assert record["files"] == ["01_第一章/_files/a.pdf"]


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
        pages = {"2602858": chapter_payload(chapter(page("第一节", 5)))}
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
        pages = {"2602858": chapter_payload(chapter(page("第一节", 5)))}
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


# ----------------------------------------------------------------- 后端接线
def test_backend_resolves_ua_identity_from_lms_textbook_endpoints():
    with tempfile.TemporaryDirectory() as directory_path:
        backend = SignBackend(lambda *_: None, root=Path(directory_path))
        backend.api.request = Mock(side_effect=[
            Mock(**{"json.return_value": [{"courseId": 900}]}),
            Mock(**{"json.return_value": {"classId": 300}}),
        ])
        assert backend._ua_course_identity(Course(42, "课程")) == ("900", "300")
        urls = [call.args[1] for call in backend.api.request.call_args_list]
        assert urls[0].endswith("/textbook/student/42/list")
        assert urls[1].endswith("/classes/information/student/42")


def test_backend_rejects_course_without_ua_identity():
    with tempfile.TemporaryDirectory() as directory_path:
        backend = SignBackend(lambda *_: None, root=Path(directory_path))
        backend.api.request = Mock(side_effect=[
            Mock(**{"json.return_value": []}),
            Mock(**{"json.return_value": {"classId": 300}}),
        ])
        try:
            backend._ua_course_identity(Course(42, "课程"))
        except RuntimeError as error:
            assert "优学院标识" in str(error)
        else:
            raise AssertionError("缺少优学院标识时必须失败")


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
            assert backend._fetch_courseware_chapter("2602858") == {}
        call = next(item for item in fake.calls if item["method"] == "Runtime.callFunctionOn")
        request = call["params"]["arguments"][0]["value"]
        assert request["url"] == f"{UA_BASE}/wholepage/chapter/stu/2602858"
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
