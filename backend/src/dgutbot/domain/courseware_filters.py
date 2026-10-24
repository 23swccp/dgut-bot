"""导出范围与附件类型筛选；只处理元数据，不联网。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

SOURCES = frozenset({"resources", "courseware", "homework"})
KINDS = frozenset({"text", "document", "image", "audio", "video", "subtitle", "other"})


def source_kind(node_id: str) -> str:
    if node_id.startswith("resources:"):
        return "resources"
    if node_id.startswith("homework:"):
        return "homework"
    return "courseware"


def attachment_kind(name: str, url: str) -> str:
    extensions = {
        "document": {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "csv", "rtf", "odt", "odp", "epub"},
        "image": {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif", "tif", "tiff"},
        "audio": {"mp3", "wav", "ogg", "m4a", "aac", "flac", "wma"},
        "video": {"mp4", "m3u8", "webm", "mov", "avi", "mkv", "flv", "wmv", "m4v", "ts"},
        "subtitle": {"srt", "vtt", "ass", "ssa", "lrc"},
    }
    for value in (name, urlsplit(url).path):
        suffix = PurePosixPath(value).suffix.lower().lstrip(".")
        for kind, known in extensions.items():
            if suffix in known:
                return kind
    return "other"


@dataclass(frozen=True)
class ExportOptions:
    sources: frozenset[str] = SOURCES
    kinds: frozenset[str] = KINDS
    # 缺失课程键表示全部；显式空列表表示该课程不导出任何目录项。
    chapters: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def includes(self, course_id: str, node_id: str) -> bool:
        return source_kind(node_id) in self.sources and (
            course_id not in self.chapters or node_id in self.chapters[course_id]
        )

    def payload(self) -> dict[str, Any]:
        return {"sources": sorted(self.sources), "kinds": sorted(self.kinds),
                "chapters": {key: list(values) for key, values in self.chapters.items()}}


def export_options(value: Any = None) -> ExportOptions:
    if value is None:
        return ExportOptions()
    if not isinstance(value, dict):
        raise ValueError("options 应为导出筛选对象")
    if set(value) - {"sources", "kinds", "chapters"}:
        raise ValueError("未知的导出筛选字段")
    selected = {}
    for key, allowed in (("sources", SOURCES), ("kinds", KINDS)):
        items = value.get(key, sorted(allowed))
        if not isinstance(items, list) or not items or any(not isinstance(item, str) or item not in allowed for item in items):
            raise ValueError(f"{key} 必须是非空的有效筛选列表")
        selected[key] = frozenset(items)
    chapters = value.get("chapters", {})
    if not isinstance(chapters, dict) or any(
        not isinstance(key, str) or not isinstance(items, list)
        or any(not isinstance(item, str) or not item for item in items)
        for key, items in chapters.items()
    ):
        raise ValueError("chapters 应为课程 ID 到目录标识列表的映射")
    return ExportOptions(**selected, chapters={key: tuple(dict.fromkeys(items)) for key, items in chapters.items()})
