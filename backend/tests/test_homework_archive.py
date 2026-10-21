import json
import tempfile
from pathlib import Path

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
