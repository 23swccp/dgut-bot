import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from dgutbot.domain.homework_archive import HomeworkArchive


def test_archive_roundtrip_and_account_isolation():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = HomeworkArchive(root)
        assert archive.sets(7) == (set(), set())

        archive.set_archived(7, "course", "161799", True)
        archive.set_archived(7, "item", "abc123", True)
        assert archive.sets(7) == ({"161799"}, {"abc123"})
        assert archive.sets(8) == (set(), set())

        reloaded = HomeworkArchive(root)
        assert reloaded.sets(7) == ({"161799"}, {"abc123"})

        reloaded.set_archived(7, "course", "161799", False)
        assert reloaded.sets(7) == (set(), {"abc123"})


def test_archive_tolerates_missing_or_invalid_file():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "homework-archive.json").write_text("not-json", encoding="utf-8")
        archive = HomeworkArchive(root)
        assert archive.sets(7) == (set(), set())
        archive.set_archived(7, "item", "x", True)
        assert json.loads((root / "homework-archive.json").read_text(encoding="utf-8"))["version"] == 1


def test_archive_rejects_invalid_kind_or_target():
    with tempfile.TemporaryDirectory() as directory:
        archive = HomeworkArchive(Path(directory))
        with pytest.raises(ValueError):
            archive.set_archived(7, "unknown", "x", True)
        with pytest.raises(ValueError):
            archive.set_archived(7, "item", "   ", True)
        with pytest.raises(ValueError):
            archive.set_archived(7, "item", "x" * 201, True)


@pytest.mark.parametrize("kind", ["course", "item"])
@pytest.mark.parametrize("archived", [True, False])
@pytest.mark.parametrize("failure_point", ["pathlib.Path.write_text", "dgutbot.domain.homework_archive.os.replace"])
def test_failed_save_restores_previous_state_and_does_not_leak_into_next_save(tmp_path, kind, archived, failure_point):
    archive = HomeworkArchive(tmp_path)
    archive.set_archived(7, "course", "keep-course", True)
    archive.set_archived(7, "item", "keep-item", True)
    archive.set_archived(8, "item", "other-user", True)
    archive.set_archived(7, kind, "target", not archived)
    before = archive.sets(7)
    before_disk = archive.path.read_bytes()

    with patch(failure_point, side_effect=OSError("simulated save failure")):
        with pytest.raises(OSError, match="simulated save failure"):
            archive.set_archived(7, kind, "target", archived)

    assert archive.sets(7) == before
    assert archive.sets(8) == (set(), {"other-user"})
    assert archive.path.read_bytes() == before_disk
    assert HomeworkArchive(tmp_path).sets(7) == before

    # 下一次成功保存不能夹带前一次失败的改动。
    archive.set_archived(8, "item", "later-save", True)
    assert HomeworkArchive(tmp_path).sets(7) == before

    # 原操作可正常重试，内存与重载后的硬盘记录一致。
    expected = (set(before[0]), set(before[1]))
    targets = expected[0 if kind == "course" else 1]
    if archived:
        targets.add("target")
    else:
        targets.discard("target")
    assert archive.set_archived(7, kind, "target", archived) == expected
    assert HomeworkArchive(tmp_path).sets(7) == expected


def test_failed_first_save_does_not_leave_a_new_account_entry(tmp_path):
    archive = HomeworkArchive(tmp_path)
    with patch("dgutbot.domain.homework_archive.os.replace", side_effect=OSError("simulated save failure")):
        with pytest.raises(OSError):
            archive.set_archived(7, "item", "target", True)
    assert archive.sets(7) == (set(), set())
    assert not archive.path.exists()

    archive.set_archived(8, "item", "other-user", True)
    saved = json.loads(archive.path.read_text(encoding="utf-8"))
    assert set(saved["users"]) == {"8"}
