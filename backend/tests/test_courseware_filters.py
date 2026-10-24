from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dgutbot.domain.courseware_export import (
    CoursewareExportService, CoursewareLibrary, ExportAttachment, ExportPage, parse_chapter,
)
from dgutbot.domain.courseware_filters import attachment_kind, export_options
from dgutbot.domain.yxy_backend import SignBackend


def payload():
    return {"wholepageItemDTOList": [{"wholepageDTOList": [{
        "content": "page", "coursepageDTOList": [{"type": 12, "content": "<p>body</p>", "resourceDTOList": [
            {"name": "slides.pptx", "url": "https://example.invalid/a.pptx", "size": 4},
            {"name": "video.mp4", "url": "https://example.invalid/a.mp4", "size": 4},
        ]}],
    }]}]}


@pytest.mark.parametrize("value", [[], {"sources": []}, {"kinds": []}, {"kinds": ["unknown"]},
                                 {"sources": "courseware"}, {"chapters": []}, {"chapters": {"1": "c1"}}, {"other": True}])
def test_invalid_options_are_rejected(value):
    with pytest.raises(ValueError):
        export_options(value)


@pytest.mark.parametrize("name,url,kind", [("PPT", "https://example.invalid/a.PPTX?token=x", "document"),
                                          ("字幕.srt", "https://example.invalid/id", "subtitle"),
                                          ("直播", "https://example.invalid/a.m3u8", "video"),
                                          ("资料.zip", "https://example.invalid/id", "other")])
def test_attachment_types_use_name_or_url(name, url, kind):
    assert attachment_kind(name, url) == kind


def test_only_selected_chapter_and_document_are_read_and_downloaded(tmp_path):
    chapters = [{"nodeid": node, "nodetitle": node, "orderindex": index}
                for index, node in enumerate(["resources:1", "c1", "c2", "homework:1:99"])]
    read = Mock(return_value=payload())
    downloads = []

    def download(url, path):
        downloads.append(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.write_bytes(b"data")

    service = CoursewareExportService(root=tmp_path, directory=lambda _: {"chapters": chapters}, chapter=read, download=download)
    options = export_options({"sources": ["courseware"], "kinds": ["document"], "chapters": {"1": ["c2"]}})
    service.start(SimpleNamespace(id=1, name="course"), options=options)
    service._thread.join(5)
    status = service.snapshot()
    assert not service._thread.is_alive() and status["state"] == "completed"
    read.assert_called_once_with("c2")
    assert downloads == ["https://example.invalid/a.pptx"]
    folder = Path(status["folder"]) / "03_c2"
    text = (folder / "01_page.md").read_text(encoding="utf-8")
    assert "body" not in text and "mp4" not in text and "pptx" in text


def test_stale_chapter_selection_stops_before_fetching_content(tmp_path):
    read = Mock()
    service = CoursewareExportService(root=tmp_path, directory=lambda _: {"chapters": []}, chapter=read)
    service.start(SimpleNamespace(id=1, name="course"), options=export_options({"chapters": {"1": ["missing"]}}))
    service._thread.join(5)
    assert service.snapshot()["state"] == "error"
    assert "目录已变化" in service.snapshot()["error"]
    read.assert_not_called()


def test_explicit_empty_selection_does_not_fetch_any_content(tmp_path):
    read = Mock()
    service = CoursewareExportService(root=tmp_path, directory=lambda _: {"chapters": [{"nodeid": "c1"}]}, chapter=read)
    service.start(SimpleNamespace(id=1, name="course"), options=export_options({"chapters": {"1": []}}))
    service._thread.join(5)
    assert service.snapshot()["pageTotal"] == 0
    read.assert_not_called()


def test_catalog_reads_only_directory_and_reports_per_course_errors():
    backend = SignBackend.__new__(SignBackend)
    backend.token = "synthetic"
    backend.courses = [SimpleNamespace(id=1, name="course"), SimpleNamespace(id=2, name="failed")]
    backend._redact = str
    backend._courseware_directory_payload = Mock(side_effect=[
        {"chapters": [{"nodeid": "resources:1"}, {"nodeid": "c1"}, {"nodeid": "homework:1:99"}]}, RuntimeError("read failed"),
    ])
    result = backend.courseware_export_catalog([1, 2])
    assert [chapter["source"] for chapter in result[0]["chapters"]] == ["resources", "courseware", "homework"]
    assert result[1]["error"] == "read failed" and result[1]["chapters"] == []
    with pytest.raises(ValueError):
        backend.courseware_export_catalog([404])


def test_resources_only_export_does_not_query_textbooks_or_homework():
    from unittest.mock import patch
    backend = SignBackend.__new__(SignBackend)
    backend.headers = {}
    backend.config = SimpleNamespace(debug_port=9222)
    api = Mock()
    with patch("dgutbot.domain.yxy_backend.BrowserApiClient", return_value=api):
        result = backend._courseware_directory_payload(SimpleNamespace(id=1, name="course"),
                                                       export_options({"sources": ["resources"]}))
    assert [item["nodeid"] for item in result["chapters"]] == ["resources:1"]
    api.request.assert_not_called()
    api.close.assert_called_once()


def test_query_parameters_identifying_distinct_attachments_are_preserved():
    data = payload()
    data["wholepageItemDTOList"][0]["wholepageDTOList"][0]["coursepageDTOList"][0] = {
        "type": 12, "content": '<a download href="https://example.invalid/download?id=1">one</a>'
                              '<a download href="https://example.invalid/download?id=2">two</a>',
    }
    page = parse_chapter(data, chapter_title="chapter", chapter_index=1)[0]
    assert len(page.attachments) == 2


def test_changed_attachment_identity_never_reuses_same_name_and_size(tmp_path):
    library = CoursewareLibrary(tmp_path)
    folder = library.course_dir("course", "1")
    page = ExportPage("chapter", 1, 1, "page", 5, "body", "<p>body</p>",
                      (ExportAttachment("a.pdf", "https://example.invalid/a", 4),))

    def download(url, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.write_bytes(b"AAAA" if url.endswith("/a") else b"BBBB")

    library.write_page(folder, page, download=download)
    new_page = replace(page, attachments=(ExportAttachment("a.pdf", "https://example.invalid/b", 4),))
    result = library.write_page(folder, new_page, download=download)
    assert (folder / result["files"][0]).read_bytes() == b"BBBB"
    third = replace(page, attachments=(ExportAttachment("a.pdf", "https://example.invalid/c", 4),))
    result = library.write_page(folder, third, download=Mock(side_effect=RuntimeError("failed")))
    assert result["files"] == [] and result["failures"]
