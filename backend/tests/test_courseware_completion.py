from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dgutbot.domain.course_scan import AuthRequiredError
from dgutbot.domain.courseware_export import CoursewareExportService
from dgutbot.domain.courseware_filters import export_options
from dgutbot.domain.yxy_backend import SignBackend


def run(service, courses):
    service.start_many(courses, options=export_options({"sources": ["resources"], "kinds": ["document"]}))
    service._thread.join(5)
    assert not service._thread.is_alive()
    return service.snapshot()


@pytest.mark.parametrize("count", [1, 2])
def test_queue_opens_completed_folder_once_after_all_manifests_are_written(tmp_path, count):
    courses = [SimpleNamespace(id=index, name=f"course-{index}") for index in range(count)]

    def opened(folder):
        assert all((tmp_path / course.name / "manifest.json").is_file() for course in courses)

    callback = Mock(side_effect=opened)
    service = CoursewareExportService(root=tmp_path, directory=lambda _: {"chapters": []}, chapter=Mock(), on_completed=callback)
    status = run(service, courses)
    expected = tmp_path / courses[0].name if count == 1 else tmp_path
    callback.assert_called_once_with(expected)
    assert service.output_folder() == expected and status["state"] == "completed"
    assert status["request"] == {"courseIds": [str(course.id) for course in courses],
                                 "options": {"sources": ["resources"], "kinds": ["document"], "chapters": {}}}
    for _ in range(3):
        service.snapshot()
    callback.assert_called_once()


@pytest.mark.parametrize("mode", ["error", "auth", "cancel"])
def test_unsuccessful_queues_do_not_open_explorer(tmp_path, mode):
    callback = Mock()

    def directory(_):
        if mode == "cancel":
            service.cancel()
            return {"chapters": []}
        raise AuthRequiredError("expired") if mode == "auth" else ValueError("failed")

    service = CoursewareExportService(root=tmp_path, directory=directory, chapter=Mock(), on_completed=callback)
    status = run(service, [SimpleNamespace(id=1, name="course")])
    assert status["state"] in {"error", "auth_required", "cancelled"}
    callback.assert_not_called()


def test_open_failure_is_reported_without_changing_export_success(tmp_path):
    callback = Mock(side_effect=OSError("explorer unavailable"))
    service = CoursewareExportService(root=tmp_path, directory=lambda _: {"chapters": []}, chapter=Mock(), on_completed=callback)
    status = run(service, [SimpleNamespace(id=1, name="course")])
    assert status["state"] == "completed" and status["error"] == ""
    assert status["folderOpenError"] == "explorer unavailable"
    callback.assert_called_once()


def test_multi_course_partial_success_opens_the_export_root(tmp_path):
    def directory(course):
        if course.id == 1:
            raise ValueError("unavailable")
        return {"chapters": []}

    callback = Mock()
    service = CoursewareExportService(root=tmp_path, directory=directory, chapter=Mock(), on_completed=callback)
    status = run(service, [SimpleNamespace(id=1, name="failed"), SimpleNamespace(id=2, name="saved")])
    assert status["state"] == "completed"
    callback.assert_called_once_with(tmp_path)


def test_folder_command_uses_only_existing_export_output(tmp_path):
    backend = SignBackend.__new__(SignBackend)
    backend.root = tmp_path
    folder = tmp_path / "courseware" / "course"
    folder.mkdir(parents=True)
    backend.courseware_export = Mock(output_folder=Mock(return_value=folder))
    with patch("dgutbot.domain.yxy_backend.os.startfile", create=True) as startfile, \
         patch("dgutbot.domain.yxy_backend.subprocess.Popen") as popen:
        assert backend.open_courseware_export_folder() == folder.resolve()
        assert startfile.call_count + popen.call_count == 1
        backend.courseware_export.output_folder.return_value = tmp_path
        with pytest.raises(ValueError):
            backend.open_courseware_export_folder()
        backend.courseware_export.output_folder.return_value = None
        with pytest.raises(ValueError):
            backend.open_courseware_export_folder()
        assert startfile.call_count + popen.call_count == 1
