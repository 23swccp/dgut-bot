"""优学院学习页课件导出：纯解析、渲染与本地资源库写入。

平台数据由 Chromium 同源请求桥提供，本模块不直接联网。导出只处理两类页面：

* ``contentType=5``：图文正文页，正文 HTML 直接存在平台数据里，没有附件；
* ``contentType=7``：文档页，正文 HTML 之外可能带附件。

页面标题在 ``wholepageDTOList[].content``，正文在
``wholepageDTOList[].coursepageDTOList[].content``；块 ``type=12`` 是正文，
``type=6`` 是测验。测验块一律跳过：既不渲染题目，也不构造任何作答请求。

ID 提醒：优学院课程 ID 与 LMS 课程 ID 不通用，``wholepage`` 也必须传章节的
``nodeid`` 而不是章节 ``id``；调用方负责换算，本模块只接收已解析的数据。
"""

from __future__ import annotations

import html as html_module
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urljoin, urlsplit

from dgutbot.domain.course_scan import AuthRequiredError

# 优学院网关：与 LMS 的 /courseapi 不同，学习页数据走 /uaapi 前缀。
UA_BASE = "https://ua.dgut.edu.cn/uaapi"
RICH_TEXT_CONTENT = 5
DOCUMENT_CONTENT = 7
EXPORTABLE_CONTENT_TYPES = frozenset({RICH_TEXT_CONTENT, DOCUMENT_CONTENT})
TEXT_BLOCK_TYPE = 12
QUIZ_BLOCK_TYPE = 6
MARKER_NAME = ".dgutbot-courseware.json"
MANIFEST_NAME = "manifest.json"
INDEX_NAME = "index.md"
FILE_DIR = "_files"
MAX_NAME = 80
MAX_COURSE_NAME = 60

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCKS = frozenset({"p", "div", "section", "article", "blockquote", "table", "tr", "ul", "ol", "pre", "figure"})
_RESOURCE_URL_KEYS = ("url", "resourceUrl", "resourceFullurl", "fileUrl", "downloadUrl", "path", "filePath")
_RESOURCE_NAME_KEYS = ("docTitle", "title", "name", "fileName", "resourceName")
_RESOURCE_SIZE_KEYS = ("resourceContentSize", "size", "docSize", "contentSize", "length")


@dataclass(frozen=True)
class ExportAttachment:
    """正文里附带的可下载文件；没有可验证地址的条目不会写入计划。"""

    name: str
    url: str
    size: int = 0


@dataclass(frozen=True)
class ExportPage:
    """一页可导出的课件，``markdown`` 与 ``html`` 都是渲染后的最终内容。"""

    chapter: str
    chapter_index: int
    page_index: int
    title: str
    content_type: int
    markdown: str
    html: str
    attachments: tuple[ExportAttachment, ...] = ()
    skipped_blocks: int = 0

    @property
    def folder(self) -> str:
        return f"{self.chapter_index:02d}_{safe_name(self.chapter) or '未命名章节'}"

    @property
    def stem(self) -> str:
        return f"{self.page_index:02d}_{safe_name(self.title) or '未命名页面'}"


def _as_list(value: Any) -> list[Any]:
    """平台字段可能是单个对象、空 ``ArrayList`` 或 ``null``，统一成列表。"""
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return [value]
    return []


def _number(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first(mapping: dict[str, Any], keys: Iterable[str]) -> str:
    """按优先级取第一个非空标识；平台常把 ``nodeid`` 之类的 ID 发成数字。"""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not value.is_integer():
                continue
            return str(int(value))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _hidden(value: Any) -> bool:
    """``hide`` 为 ``0``/缺失时可见；其它非空值按隐藏处理。"""
    if value is None or value == "":
        return False
    return _number(value, 0) != 0


def _plain(value: Any) -> str:
    """去掉标签并把连续空白压成单个空格，用于标题与摘要。"""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]*>", " ", value)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def safe_name(name: str, *, limit: int = MAX_NAME) -> str:
    """生成跨平台安全的文件名，保留原扩展名并限制总长度。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t\x00-\x1f]+', "_", str(name or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    stem, dot, extension = cleaned.rpartition(".")
    if dot and 0 < len(extension) <= 6:
        keep = max(1, limit - len(extension) - 1)
        return f"{stem[:keep]}.{extension}"
    return cleaned[:limit]


def _absolute(url: str, base: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if url.startswith(("data:", "http://", "https://")):
        return url
    return urljoin(base or "https://ua.dgut.edu.cn/", url)


# ----------------------------------------------------------------- HTML 转 Markdown
class _MarkdownParser(HTMLParser):
    """把平台富文本转成可读 Markdown。

    平台正文是深层嵌套的 ``span``/``font``，因此只认块级边界与少量行内标记，
    其余标签忽略但不丢文本。
    """

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self._parts: list[str] = []
        self._href: list[str] = []
        self._inline: list[str] = []

    def _trim(self) -> None:
        # 只清理悬空空格，块边界写入的 "\n\n" 必须保留，否则段落会被粘连。
        while self._parts and self._parts[-1] in {" ", "\t"}:
            self._parts.pop()
        if self._parts:
            self._parts[-1] = self._parts[-1].rstrip(" ")

    def _break(self, count: int = 2) -> None:
        self._trim()
        if not "".join(self._parts).strip():
            return
        joined = "".join(self._parts)
        trailing = len(joined) - len(joined.rstrip("\n"))
        if trailing < count:
            self._parts.append("\n" * (count - trailing))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: (value or "") for key, value in attrs}
        if tag in _HEADINGS:
            self._break(2)
            self._parts.append("#" * _HEADINGS[tag] + " ")
        elif tag in _BLOCKS:
            self._break(2)
        elif tag == "br":
            self._break(1)
        elif tag == "hr":
            self._break(2)
            self._parts.append("---")
        elif tag == "li":
            self._break(1)
            self._parts.append("- ")
        elif tag in {"strong", "b"}:
            self._parts.append("**")
            self._inline.append("**")
        elif tag in {"em", "i"}:
            self._parts.append("*")
            self._inline.append("*")
        elif tag == "code":
            self._parts.append("`")
            self._inline.append("`")
        elif tag == "a":
            self._href.append(_absolute(values.get("href", ""), self._base))
            self._parts.append("[")
        elif tag == "img":
            source = _absolute(values.get("src", ""), self._base)
            if source:
                alt = (values.get("alt") or "").strip()
                self._parts.append(f"![{alt}]({source})")
        elif tag in {"td", "th"}:
            self._parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HEADINGS:
            self._break(2)
        elif tag in _BLOCKS:
            self._break(2)
        elif tag == "li":
            self._break(1)
        elif tag in {"strong", "b", "em", "i", "code"}:
            if self._inline:
                self._parts.append(self._inline.pop())
        elif tag == "a":
            href = self._href.pop() if self._href else ""
            self._parts.append(f"]({href})" if href else "]")

    def handle_data(self, data: str) -> None:
        if not data:
            return
        text = re.sub(r"\s+", " ", data)
        if text.strip():
            if text.startswith(" ") and self._parts and self._parts[-1].endswith("\n"):
                text = text.lstrip()
            self._parts.append(text)
        elif self._parts and not self._parts[-1].endswith(("\n", " ")):
            self._parts.append(" ")

    def markdown(self) -> str:
        self._trim()
        text = "".join(self._parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(fragment: str, *, base_url: str = "") -> str:
    """把一段平台 HTML 转成 Markdown；解析失败时保留裸文本。"""
    if not fragment or not fragment.strip():
        return ""
    parser = _MarkdownParser(base_url)
    try:
        parser.feed(fragment)
        parser.close()
    except Exception:
        return _plain(fragment)
    return parser.markdown()


def render_page_html(*, chapter: str, title: str, fragments: Iterable[str]) -> str:
    """生成便于离线阅读的单页 HTML，样式与导出的 Markdown 保持一致。"""
    body = "\n".join(fragment for fragment in fragments if fragment and fragment.strip())
    return (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html_module.escape(title)}</title>"
        "<style>body{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:860px;"
        "margin:32px auto;padding:0 20px;line-height:1.8;color:#222}img{max-width:100%}"
        "table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:4px 8px}</style>"
        f"<h2>{html_module.escape(chapter)}</h2>"
        f"<h3>{html_module.escape(title)}</h3>"
        f"{body}"
    )


# ----------------------------------------------------------------- 解析
def parse_directory(payload: Any) -> tuple[str, list[dict[str, Any]]]:
    """解析课程目录，返回 (课程名, 可导出章节列表)。

    章节按 ``orderindex`` 稳定排序；隐藏章节直接丢弃，不进入导出队列。
    """
    if not isinstance(payload, dict):
        return "", []
    course_name = _plain(payload.get("coursename") or payload.get("courseName") or "")
    chapters: list[dict[str, Any]] = []
    for raw in _as_list(payload.get("chapters")):
        if not isinstance(raw, dict) or _hidden(raw.get("hide")):
            continue
        node = _first(raw, ("nodeid", "nodeId", "chapterNodeId"))
        if not node:
            continue
        title = _plain(raw.get("nodetitle") or raw.get("title") or "")
        chapters.append({
            "nodeid": node,
            "title": title or f"第{len(chapters) + 1}章",
            "order": _number(raw.get("orderindex") or raw.get("orderIndex")),
        })
    chapters.sort(key=lambda item: item["order"])
    return course_name, chapters


def _attachment(entry: dict[str, Any], content: str) -> ExportAttachment | None:
    url = _first(entry, _RESOURCE_URL_KEYS)
    if not url:
        # 正文里的 <div class="file-doc" data-file="..."> 是附件地址的兜底来源。
        match = re.search(r"data-file=['\"]([^'\"]+)['\"]", content or "")
        url = unquote(match.group(1)) if match else ""
    if not url.lower().startswith(("http://", "https://")):
        return None
    name = _first(entry, _RESOURCE_NAME_KEYS) or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    size = next((_number(entry.get(key)) for key in _RESOURCE_SIZE_KEYS if entry.get(key)), 0)
    return ExportAttachment(name=name or "附件", url=url, size=size)


def _page_blocks(page: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [block for block in _as_list(page.get("coursepageDTOList")) if isinstance(block, dict)]
    blocks.sort(key=lambda block: _number(block.get("orderIndex") or block.get("orderindex")))
    return blocks


def _is_quiz(block: dict[str, Any]) -> bool:
    return _number(block.get("type")) == QUIZ_BLOCK_TYPE or bool(_as_list(block.get("questionDTOList")))


def parse_chapter(
    payload: Any, *, chapter_title: str, chapter_index: int, base_url: str = UA_BASE,
) -> list[ExportPage]:
    """解析章节整页数据，只返回图文页与文档页；测验块被跳过并计数。"""
    if not isinstance(payload, dict):
        return []
    items = [item for item in _as_list(payload.get("wholepageItemDTOList")) if isinstance(item, dict)]
    items.sort(key=lambda item: _number(item.get("orderindex") or item.get("orderIndex")))
    pages: list[ExportPage] = []
    for item in items:
        for page in _as_list(item.get("wholepageDTOList")):
            if not isinstance(page, dict) or _hidden(page.get("hide")):
                continue
            content_type = _number(page.get("contentType"))
            fragments: list[str] = []
            attachments: list[ExportAttachment] = []
            skipped = 0
            for block in _page_blocks(page):
                if _is_quiz(block):
                    skipped += 1
                    continue
                content = block.get("content") if isinstance(block.get("content"), str) else ""
                entries = [entry for entry in _as_list(block.get("resourceDTOList")) if isinstance(entry, dict)]
                for entry in entries:
                    attachment = _attachment(entry, content)
                    if attachment:
                        attachments.append(attachment)
                if not entries and content.strip():
                    # 没有 resourceDTOList 时，附件地址只存在于正文的 data-file 属性里。
                    attachment = _attachment({}, content)
                    if attachment:
                        attachments.append(attachment)
                if content.strip():
                    fragments.append(content)
            if content_type not in EXPORTABLE_CONTENT_TYPES and not fragments:
                continue
            if not fragments and not attachments:
                continue
            title = _plain(page.get("content")) or f"第{len(pages) + 1}页"
            markdown_parts = [html_to_markdown(fragment, base_url=base_url) for fragment in fragments]
            markdown = "\n\n".join(part for part in markdown_parts if part)
            if attachments:
                links = "\n".join(f"- [{item.name}]({item.url})" for item in attachments)
                markdown = f"{markdown}\n\n## 附件\n\n{links}".strip()
            pages.append(ExportPage(
                chapter=chapter_title,
                chapter_index=chapter_index,
                page_index=len(pages) + 1,
                title=title,
                content_type=content_type,
                markdown=markdown,
                html=render_page_html(chapter=chapter_title, title=title, fragments=fragments),
                attachments=tuple(attachments),
                skipped_blocks=skipped,
            ))
    return pages


# ----------------------------------------------------------------- 资源库
class CoursewareLibrary:
    """把导出结果写入本地资源库，并用标记文件避免同名课程互相覆盖。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def course_dir(self, course_name: str, course_id: str) -> Path:
        base = safe_name(course_name, limit=MAX_COURSE_NAME) or "未命名课程"
        identifier = safe_name(str(course_id or "unknown"), limit=40)
        candidates = [base, f"{base} ({identifier})"]
        candidates.extend(f"{base} ({identifier}-{index})" for index in range(2, 20))
        for name in candidates:
            path = self.root / name
            marker = self._read_marker(path)
            if marker is not None:
                if str(marker.get("id")) == str(course_id):
                    return path
                continue
            if path.exists():
                # 已存在但没有标记：可能是用户自己的目录，绝不接管。
                continue
            path.mkdir(parents=True, exist_ok=True)
            self._write_marker(path, course_id, course_name)
            return path
        raise RuntimeError("无法为课程创建独立目录，请清理同名文件夹后重试")

    @staticmethod
    def _read_marker(path: Path) -> dict[str, Any] | None:
        try:
            values = json.loads((path / MARKER_NAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return values if isinstance(values, dict) else None

    @staticmethod
    def _write_marker(path: Path, course_id: str, course_name: str) -> None:
        _write_json(path / MARKER_NAME, {"id": str(course_id), "name": course_name})

    def write_page(
        self, course_dir: Path, page: ExportPage, *, download: Callable[[str, Path], int] | None = None,
    ) -> dict[str, Any]:
        """写入一页的 Markdown、HTML 与附件；内容未变化时返回 ``unchanged``。"""
        folder = course_dir / page.folder
        folder.mkdir(parents=True, exist_ok=True)
        written = 0
        unchanged = 0
        for name, text in ((f"{page.stem}.md", page.markdown + "\n"), (f"{page.stem}.html", page.html)):
            if _write_if_changed(folder / name, text.encode("utf-8")):
                written += 1
            else:
                unchanged += 1
        files: list[str] = []
        failures: list[dict[str, str]] = []
        used: set[str] = set()
        for attachment in page.attachments:
            if download is None:
                break
            name = _unique_name(_attachment_name(attachment), used)
            target = folder / FILE_DIR / name
            used.add(name.lower())
            if attachment.size and target.exists() and target.stat().st_size == attachment.size:
                files.append(target.relative_to(course_dir).as_posix())
                continue
            try:
                download(attachment.url, target)
            except Exception as error:  # 单个附件失败不能影响正文导出
                failures.append({"file": name, "reason": str(error)[:160]})
                continue
            files.append(target.relative_to(course_dir).as_posix())
        return {
            "path": (folder / f"{page.stem}.md").relative_to(course_dir).as_posix(),
            "html": (folder / f"{page.stem}.html").relative_to(course_dir).as_posix(),
            "chapter": page.chapter,
            "title": page.title,
            "contentType": page.content_type,
            "skippedBlocks": page.skipped_blocks,
            "written": written,
            "unchanged": unchanged,
            "files": files,
            "failures": failures,
        }

    def write_manifest(self, course_dir: Path, *, course: Any, records: list[dict[str, Any]], state: str) -> None:
        values = {
            "version": 1,
            "course": str(getattr(course, "name", "") or ""),
            "courseId": str(getattr(course, "id", "") or ""),
            "state": state,
            "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pages": records,
        }
        _write_json(course_dir / MANIFEST_NAME, values)

    def write_index(self, course_dir: Path, *, course_name: str, records: list[dict[str, Any]]) -> None:
        lines = [f"# {course_name or '课程课件'}"]
        chapter = ""
        for record in records:
            if record["chapter"] != chapter:
                chapter = record["chapter"]
                lines.extend(["", f"## {chapter}", ""])
            label = str(record["title"]).replace("[", "［").replace("]", "］")
            lines.append(f"- [{label}]({_link_path(record['path'])})")
        target = course_dir / INDEX_NAME
        _write_if_changed(target, ("\n".join(lines).strip() + "\n").encode("utf-8"))


def _link_path(path: str) -> str:
    """只转义会打断 Markdown 链接目标的字符，保留中文可读性。"""
    return (str(path).replace("%", "%25").replace(" ", "%20")
            .replace("(", "%28").replace(")", "%29"))


def _unique_name(name: str, used: set[str]) -> str:
    """同页出现同名附件时追加序号，避免后写的覆盖先写的。"""
    if name.lower() not in used:
        return name
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    index = 2
    while True:
        candidate = f"{stem} ({index}).{extension}" if extension else f"{stem} ({index})"
        if candidate.lower() not in used:
            return candidate
        index += 1


def _attachment_name(attachment: ExportAttachment) -> str:
    name = safe_name(attachment.name) or "附件"
    if "." not in name:
        suffix = urlsplit(attachment.url).path.rsplit("/", 1)[-1]
        extension = suffix.rsplit(".", 1)[-1] if "." in suffix else ""
        if 0 < len(extension) <= 6:
            name = f"{name}.{extension.lower()}"
    return safe_name(name) or "附件"


def _write_if_changed(path: Path, data: bytes) -> bool:
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return True


def _write_json(path: Path, values: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


# ----------------------------------------------------------------- 服务
class CoursewareExportService:
    """单实例串行的课程导出任务；每门课失败只记录，不打断其余课程。"""

    def __init__(
        self,
        *,
        root: Path,
        directory: Callable[[Any], Any],
        chapter: Callable[[str], Any],
        download: Callable[[str, Path], int] | None = None,
        authenticated: Callable[[], bool] = lambda: True,
        redact: Callable[[object], str] = str,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        self._root = Path(root)
        self._library = CoursewareLibrary(self._root)
        self._directory = directory
        self._chapter = chapter
        self._download = download
        self._authenticated = authenticated
        self._redact = redact
        self._log = log
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._snapshot = self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "state": "idle", "courseId": "", "courseName": "", "chapterTotal": 0, "chapterDone": 0,
            "pageTotal": 0, "pageWritten": 0, "pageUnchanged": 0, "fileWritten": 0,
            "failures": [], "startedAt": "", "finishedAt": "", "folder": "", "error": "",
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._snapshot))

    def start(self, course: Any) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.snapshot()
            if not self._authenticated():
                self._snapshot = self._empty() | {"state": "auth_required", "error": "登录状态不可用，请重新登录"}
                return self.snapshot()
            self._cancel = threading.Event()
            self._snapshot = self._empty() | {
                "state": "running",
                "courseId": str(getattr(course, "id", "") or ""),
                "courseName": str(getattr(course, "name", "") or ""),
                "startedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            self._thread = threading.Thread(target=self._run, args=(course,), name="courseware-export", daemon=True)
            self._thread.start()
            return self.snapshot()

    def cancel(self) -> dict[str, Any]:
        self._cancel.set()
        with self._lock:
            if self._snapshot["state"] == "running":
                self._snapshot["state"] = "cancelling"
        return self.snapshot()

    def _run(self, course: Any) -> None:
        """任何未预期异常都必须落到终态，不能让快照永远停在 running。"""
        try:
            self._run_course(course)
        except Exception as error:  # noqa: BLE001 - 后台线程没有调用方可以承接异常
            self._finish("error", self._redact(error))

    def _run_course(self, course: Any) -> None:
        try:
            payload = self._directory(course)
        except AuthRequiredError as error:
            self._finish("auth_required", self._redact(error))
            return
        except Exception as error:
            self._finish("error", self._redact(error))
            return
        course_name, chapters = parse_directory(payload)
        course_name = course_name or str(getattr(course, "name", "") or "")
        try:
            course_dir = self._library.course_dir(course_name, str(getattr(course, "id", "") or ""))
        except Exception as error:
            self._finish("error", self._redact(error))
            return
        with self._lock:
            self._snapshot.update(courseName=course_name, chapterTotal=len(chapters),
                                  folder=str(course_dir), state="running")
        records: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        stopped = False
        for index, chapter in enumerate(chapters, 1):
            if self._cancel.is_set():
                stopped = True
                break
            title = str(chapter.get("title") or "")
            node = str(chapter.get("nodeid") or "")
            try:
                page_payload = self._chapter(node)
            except AuthRequiredError as error:
                self._finish("auth_required", self._redact(error), records=records, course=course, directory=course_dir)
                return
            except Exception as error:
                failures.append({"chapter": title, "reason": self._redact(error)})
                with self._lock:
                    self._snapshot["failures"] = list(failures)
                    self._snapshot["chapterDone"] = index
                continue
            pages = parse_chapter(page_payload, chapter_title=title, chapter_index=index)
            for page in pages:
                if self._cancel.is_set():
                    stopped = True
                    break
                try:
                    record = self._library.write_page(course_dir, page, download=self._download)
                except Exception as error:
                    failures.append({"chapter": title, "reason": self._redact(error)})
                    continue
                records.append(record)
                failures.extend(dict(item, chapter=title) for item in record.get("failures") or [])
                with self._lock:
                    self._snapshot["pageTotal"] += 1
                    self._snapshot["pageWritten"] += int(record["written"] > 0)
                    self._snapshot["pageUnchanged"] += int(record["written"] == 0)
                    self._snapshot["fileWritten"] += len(record["files"])
                    self._snapshot["failures"] = list(failures)
            with self._lock:
                self._snapshot["chapterDone"] = index
            if stopped:
                break
        state = "cancelled" if stopped else ("error" if failures and not records else "completed")
        self._finish(state, "", records=records, course=course, directory=course_dir, failures=failures)

    def _finish(
        self, state: str, error: str, *, records: list[dict[str, Any]] | None = None,
        course: Any = None, directory: Path | None = None, failures: list[dict[str, Any]] | None = None,
    ) -> None:
        records = records or []
        if directory is not None and course is not None:
            try:
                self._library.write_manifest(directory, course=course, records=records, state=state)
                self._library.write_index(directory, course_name=self._snapshot.get("courseName", ""), records=records)
            except OSError as write_error:
                error = error or f"写入清单失败：{self._redact(write_error)}"
                state = "error" if state == "completed" else state
        with self._lock:
            failure_items = failures if failures is not None else list(self._snapshot.get("failures") or [])
            self._snapshot.update(
                state=state, error=self._redact(error) if error else "",
                failures=failure_items,
                finishedAt=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            summary = dict(self._snapshot)
        if self._log is None:
            return
        if error:
            self._log(f"课件导出：{self._redact(error)}", "warn")
        elif state == "completed":
            self._log(f"课件导出完成：{summary['pageTotal']} 页，附件 {summary['fileWritten']} 个 → {summary['folder']}", "success")
        elif state == "cancelled":
            self._log(f"课件导出已取消，已保留 {summary['pageTotal']} 页 → {summary['folder']}", "warn")
