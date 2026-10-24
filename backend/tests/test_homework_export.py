import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import unquote

import pytest

from dgutbot.domain.course_scan import AuthRequiredError
from dgutbot.domain.courseware_export import CoursewareExportService, CoursewareLibrary, ExportSectionResult
from dgutbot.domain.homework_export import HOMEWORK_API, append_homework_directory, export_homework
from dgutbot.domain.yxy_backend import SignBackend


COURSE = SimpleNamespace(id="42", name="示例课程", teacher_name="示例教师")


def fixture():
    return {
        "activityHomework": {
            "id": 99, "homeworkTitle": "第一次作业", "homeworkRequest": '<p>完成分析<img src="/pictures/question.png"></p>',
            "homeworkContent": "<p>补充要求</p>", "autoRemark": 2, "gradePublish": 1, "grade": 100,
            "fileUpload": json.dumps([{"fileName": "要求.pdf", "filePath": "/homework/require.pdf", "fileSize": 8}]),
        },
        "studentHomework": {
            "id": 21, "content": "<p>本人正文</p>", "grade": 90, "teacherScore": 91, "overallComment": "教师反馈",
            "fileUpload": [{"fileName": "答案.docx", "filePath": "https://leicloud.ulearning.cn/homework/mine.docx", "fileSize": 8}],
        },
        "peerReviewHomeworkList": [{"peerReviewID": 8, "score": 89, "comment": "收到的反馈", "name": "不应导出的姓名"}],
    }


def peers():
    return [{
        "peerReviewID": number, "userID": number + 1000, "name": "不应导出的姓名",
        "content": f"<p>他人正文 {number}</p>", "score": 80 if number == 11 else None,
        "comment": "我给出的评语", "isPeerReviewTime": 1,
        "fileUpload": json.dumps([{"fileName": "答案.docx", "filePath": f"/homework/peer-{number}.docx", "fileSize": 8}]),
    } for number in (11, 12)]


def fetcher(*, detail=None, peer_values=None):
    responses = {
        f"{HOMEWORK_API}/stuHomework/homeworkDetail/99/7/42": {"result": fixture() if detail is None else detail},
        f"{HOMEWORK_API}/stuHomework/peerReviewHomeworkDatil/99/7": {"result": peers() if peer_values is None else peer_values},
        f"{HOMEWORK_API}/teaHomework/peerReviewHomeworkRule/99": {"result": [{"scoringPoint": "分析", "score": 100}]},
    }
    return Mock(side_effect=lambda url, **_: responses[url])


def export(**kwargs):
    return export_homework("homework:42:99", 7, fetcher(**kwargs))


def test_directory_appends_all_pages_and_same_named_assignments_without_local_archive_filter():
    payload = {"coursename": COURSE.name, "chapters": [{"nodeid": "resources:42", "orderindex": 0}], "failures": []}
    fetch = Mock(side_effect=[
        {"result": {"list": [{"id": 99, "title": "作业", "state": 4}, {"id": 100, "title": "作业", "state": 0}], "total": 3, "pageSize": 2}},
        {"result": {"list": [{"id": 101, "title": "另一作业", "state": 2}], "total": 3, "pageSize": 2}},
    ])
    result = append_homework_directory(payload, COURSE, fetch)
    assert [chapter["nodeid"] for chapter in result["chapters"]] == ["resources:42", "homework:42:99", "homework:42:100", "homework:42:101"]
    assert [call.kwargs["params"]["pn"] for call in fetch.call_args_list] == [1, 2]
    assert result["chapters"][1]["nodetitle"] != result["chapters"][2]["nodetitle"]


def test_list_failure_preserves_courseware_directory_and_auth_stops():
    payload = {"chapters": [{"nodeid": "chapter-1"}]}
    result = append_homework_directory(payload, COURSE, Mock(side_effect=RuntimeError("读取失败")))
    assert result["chapters"] == [{"nodeid": "chapter-1"}]
    assert result["failures"] == [{"chapter": "课程作业列表", "reason": "读取失败"}]
    with pytest.raises(AuthRequiredError):
        append_homework_directory(payload, COURSE, Mock(side_effect=AuthRequiredError("登录失效")))


def test_exports_requirements_own_submission_feedback_peers_and_rules_with_attachments():
    result = export()
    assert not result.failures and len(result.pages) == 6
    requirement, mine, feedback, first, second, rules = result.pages
    assert "补充要求" in requirement.markdown
    assert {item.name for item in requirement.attachments} == {"question.png", "要求.pdf"}
    assert "本人正文" in mine.markdown and "教师反馈" in mine.markdown
    assert mine.attachments[0].size == 8
    assert mine.attachments[0].url == "https://uobs.dgut.edu.cn/view/homework/mine.docx"
    assert "收到的反馈" in feedback.markdown and "89" in feedback.markdown
    assert "他人正文 11" in first.markdown and "我给出的评语" in first.markdown
    assert first.folder != second.folder != mine.folder
    assert "分析" in rules.markdown
    assert "不应导出的姓名" not in str(result.pages) and "userID" not in str(result.pages)


def test_peer_folders_are_stable_when_the_platform_reorders_assignments():
    before = {p.resource_id: p.folder for p in export().pages}
    after = {p.resource_id: p.folder for p in export(peer_values=list(reversed(peers()))).pages}
    assert before == after


def test_empty_submission_and_draft_have_explicit_descriptions_and_unpublished_grades_are_omitted():
    detail = fixture()
    detail["studentHomework"] = None
    detail["activityHomework"]["autoRemark"] = 1
    fetch = fetcher(detail=detail)
    result = export_homework("homework:42:99", 7, fetch)
    assert "当前没有本人提交" in result.pages[1].markdown
    assert fetch.call_count == 1
    detail = fixture()
    detail["activityHomework"]["gradePublish"] = 0
    detail["studentHomework"]["draftState"] = 1
    result = export(detail=detail)
    assert "当前为草稿" in result.pages[1].markdown
    assert "教师反馈" not in result.pages[1].markdown
    assert all(page.title != "收到的互评 1" for page in result.pages)


def test_missing_and_malformed_file_metadata_is_reported_without_losing_body():
    detail = fixture()
    detail["activityHomework"]["fileUpload"] = [{"fileName": "丢失.pdf"}]
    detail["studentHomework"]["fileUpload"] = "not-json"
    result = export(detail=detail)
    assert len(result.failures) == 2
    assert "本人正文" in result.pages[1].markdown
    assert "丢失.pdf" in result.failures[0]["file"]


def test_peer_read_failure_preserves_own_pages_and_propagates_auth_error():
    fetch = fetcher()
    normal_fetch = fetch.side_effect

    def read(url, **kwargs):
        if "/peerReviewHomeworkDatil/" in url:
            raise RuntimeError("互评入口暂不可用")
        return normal_fetch(url, **kwargs)

    result = export_homework("homework:42:99", 7, read)
    assert "本人正文" in result.pages[1].markdown
    assert result.failures == ({"file": "互评作业", "reason": "互评入口暂不可用"},)
    with pytest.raises(AuthRequiredError):
        export_homework("homework:42:99", None, fetch)
    with pytest.raises(AuthRequiredError):
        export_homework("homework:42:99", 7, Mock(side_effect=AuthRequiredError("登录失效")))


def test_library_downloads_every_role_separately_and_rewrites_markdown_and_html(tmp_path):
    library = CoursewareLibrary(tmp_path)
    directory = library.course_dir(COURSE.name, COURSE.id)

    def download(url, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"document")
        return 8

    records = [library.write_page(directory, page, download=download) for page in export().pages]
    assert sum(len(record["files"]) for record in records) == 5
    assert len({path for record in records for path in record["files"]}) == 5
    for record in records:
        for path in record["files"]:
            target = directory / path
            assert target.read_bytes() == b"document"
            relative = target.relative_to((directory / record["path"]).parent).as_posix()
            assert relative in unquote((directory / record["path"]).read_text(encoding="utf-8"))
            assert relative in unquote((directory / record["html"]).read_text(encoding="utf-8"))


def run(service):
    service.start(COURSE)
    service._thread.join(5)
    assert not service._thread.is_alive()
    return service.snapshot()


def test_course_queue_indexes_homework_and_keeps_old_peer_files_when_refresh_fails(tmp_path):
    def download(url, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"document")
        return 8

    directory = lambda _: {"coursename": COURSE.name, "chapters": [{"nodeid": "homework:42:99", "nodetitle": "作业"}]}
    first = CoursewareExportService(root=tmp_path, directory=directory, chapter=lambda _: export(), download=download)
    status = run(first)
    assert status["state"] == "completed" and status["fileWritten"] == 5
    folder = Path(status["folder"])
    assert "互评作业" in (folder / "index.md").read_text(encoding="utf-8")
    partial = ExportSectionResult(export().pages[:2], ({"resource": "互评", "reason": "暂不可用"},))
    second = CoursewareExportService(root=tmp_path, directory=directory, chapter=lambda _: partial, download=download)
    assert run(second)["failures"]
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["pages"]) == 6
    assert all((folder / path).is_file() for page in manifest["pages"] for path in page["files"])


def test_backend_uses_only_get_for_directory_and_homework_and_closes_each_client():
    backend = SignBackend.__new__(SignBackend)
    backend.user_id = 7
    backend.token = "test-token"
    backend.headers = {"Authorization": backend.token}
    backend.config = SimpleNamespace(debug_port=9222)
    values = [[], {"list": [{"id": 99, "homeworkTitle": "作业"}], "total": 1},
              {"result": fixture()}, {"result": peers()}, {"result": []}]
    client = Mock()
    client.request.side_effect = [Mock(json=Mock(return_value=value)) for value in values]
    with patch("dgutbot.domain.yxy_backend.BrowserApiClient", return_value=client):
        directory = backend._courseware_directory_payload(COURSE)
        result = backend._fetch_courseware_chapter(directory["chapters"][-1]["nodeid"])
    assert isinstance(result, ExportSectionResult)
    assert all(call.args[0] == "GET" for call in client.request.call_args_list)
    assert client.close.call_count == 2


def test_cancellation_retains_completed_material_and_does_not_download_peer_submissions(tmp_path):
    calls = []

    def download(url, target):
        calls.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"document")
        service.cancel()
        return 8

    service = CoursewareExportService(
        root=tmp_path, directory=lambda _: {"chapters": [{"nodeid": "homework:42:99", "nodetitle": "作业"}]},
        chapter=lambda _: export(), download=download,
    )
    status = run(service)
    assert status["state"] == "cancelled"
    assert calls and not any("peer-" in url or "mine.docx" in url for url in calls)
    manifest = json.loads((Path(status["folder"]) / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["pages"]) == 1
    assert manifest["pages"][0]["title"] == "题目与要求"


def test_failed_download_is_reported_alongside_saved_own_and_peer_work(tmp_path):
    library = CoursewareLibrary(tmp_path)
    directory = library.course_dir(COURSE.name, COURSE.id)

    def download(url, target):
        if "peer-11" in url:
            raise RuntimeError("附件暂不可用")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"document")
        return 8

    records = [library.write_page(directory, page, download=download) for page in export().pages]
    assert sum(len(record["files"]) for record in records) == 4
    failed = next(record for record in records if record["failures"])
    assert failed["failures"][0]["reason"] == "附件暂不可用"
    assert "他人正文 11" in (directory / failed["path"]).read_text(encoding="utf-8")


def test_pagination_reads_outer_total_with_nested_list_and_stops_repeated_ids():
    fetch = Mock(side_effect=[
        {"total": 4, "pageSize": 2, "result": {"list": [{"id": 99}, {"id": 100}]}},
        {"total": 4, "pageSize": 2, "result": {"list": [{"id": 99}, {"id": 100}]}},
    ])
    result = append_homework_directory({"chapters": []}, COURSE, fetch)
    assert fetch.call_count == 2 and len(result["chapters"]) == 2
