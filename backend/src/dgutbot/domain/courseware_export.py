"""优学院学习页课件导出：纯解析、渲染与本地资源库写入。

平台数据由 Chromium 同源请求桥提供，本模块不直接联网。导出正文、文档、
图片及音视频；独立资源区由来源适配器转换为同一种页面模型。

页面标题在 ``wholepageDTOList[].content``，正文在
``wholepageDTOList[].coursepageDTOList[].content``；块 ``type=12`` 是正文，
``type=6`` 是测验。测验块一律跳过：既不渲染题目，也不构造任何作答请求。

ID 提醒：优学院课程 ID 与 LMS 课程 ID 不通用，``wholepage`` 也必须传章节的
``nodeid`` 而不是章节 ``id``；调用方负责换算，本模块只接收已解析的数据。
"""

from __future__ import annotations

import html as html_module
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit

from dgutbot.domain.course_scan import AuthRequiredError
from dgutbot.domain.courseware_filters import ExportOptions, attachment_kind, source_kind

# 优学院网关：与 LMS 的 /courseapi 不同，学习页数据走 /uaapi 前缀。
UA_BASE = "https://ua.dgut.edu.cn/uaapi"
UA_RESOURCE_BASE = "https://lms.dgut.edu.cn/uobs/view/"
RICH_TEXT_CONTENT = 5
DOCUMENT_CONTENT = 7
TEXT_BLOCK_TYPE = 12
QUIZ_BLOCK_TYPE = 6
MARKER_NAME = ".dgutbot-courseware.json"
MANIFEST_NAME = "manifest.json"
INDEX_NAME = "index.md"
FILE_DIR = "_files"
MAX_NAME = 64
MAX_COURSE_NAME = 48

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCKS = frozenset({"p", "div", "section", "article", "blockquote", "table", "tr", "ul", "ol", "pre", "figure"})
_RESOURCE_URL_KEYS = ("url", "resourceUrl", "resourceFullurl", "fileUrl", "downloadUrl", "locationFullurl", "location", "path", "filePath")
_RESOURCE_NAME_KEYS = ("docTitle", "title", "name", "fileName", "resourceName")
_RESOURCE_SIZE_KEYS = ("resourceContentSize", "size", "docSize", "contentSize", "length")


@dataclass(frozen=True)
class ExportAttachment:
    """正文里附带的可下载文件；没有可验证地址的条目不会写入计划。"""

    name: str
    url: str
    size: int = 0
    playback_url: str = ""


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
    source_url: str = ""
    resource_id: str = ""

    @property
    def folder(self) -> str:
        return f"{self.chapter_index:02d}_{safe_name(self.chapter) or '未命名章节'}"

    @property
    def stem(self) -> str:
        return f"{self.page_index:02d}_{safe_name(self.title) or '未命名页面'}"


@dataclass(frozen=True)
class ExportSectionResult:
    """其它课程入口转换后的页面及发现阶段的失败详情。"""

    pages: tuple[ExportPage, ...]
    failures: tuple[dict[str, str], ...] = ()


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
    if cleaned.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"{kind}{i}" for kind in ("COM", "LPT") for i in range(1, 10)]}:
        cleaned = "_" + cleaned
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


def _ua_resource_url(url: str) -> str:
    for prefix in ("http://leicloud.ulearning.cn/", "http://leicloud.qiniudn.com/", "https://tskcloud.ulearning.cn/"):
        if url.startswith(prefix):
            return UA_RESOURCE_BASE + url[len(prefix):]
    if url and not urlsplit(url).scheme:
        return UA_RESOURCE_BASE + url.lstrip("/")
    return url


def _nested_file_url(url: str) -> str:
    """从学校文档查看器的 furl 取真实文件，再统一旧存储地址。"""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host != "docs.ulearning.cn" and not host.endswith(".docs.ulearning.cn"):
        return ""
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "furl" and value.lower().startswith(("http://", "https://")):
            return _ua_resource_url(value)
    return ""


def _attachment(entry: dict[str, Any], content: str) -> ExportAttachment | None:
    url = _first(entry, _RESOURCE_URL_KEYS)
    if not url:
        # 正文里的 <div class="file-doc" data-file="..."> 是附件地址的兜底来源。
        match = re.search(r"data-file=['\"]([^'\"]+)['\"]", content or "")
        url = unquote(match.group(1)) if match else ""
    if not url:
        # 文档课件通过查看器 iframe 嵌套：src="...docs.ulearning.cn?...&furl=<真实文件>"。
        match = re.search(r"docs\.ulearning\.cn[^'\"<>]*?furl=([^&'\"<>]+)", content or "")
        url = unquote(match.group(1)) if match else ""
    url = _ua_resource_url(url)
    url = _nested_file_url(url) or url
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


def _resource_key(url: str) -> str:
    """合并学校资源主机别名及认证查询参数，其它主机按独立身份保留。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    path = unquote(parts.path)
    if host in {"lms.dgut.edu.cn", "uobs.dgut.edu.cn"} and path.startswith("/uobs/view/"):
        host = "uobs.dgut.edu.cn"
        path = path[len("/uobs"):]
    query = parts.query
    if host in {"lms.dgut.edu.cn", "uobs.dgut.edu.cn"} and path.startswith("/view/"):
        query = urlencode([(key, value) for key, value in parse_qsl(query, keep_blank_values=True)
                           if key.lower() not in {"token", "authorization", "ua-authorization", "ts"}])
    return f"{parts.scheme.lower()}://{host}{':' + str(parts.port) if parts.port else ''}{path}?{query}"


def _prefer_attachment(left: ExportAttachment, right: ExportAttachment) -> ExportAttachment:
    """合并同一文件的两种写法：保留人类可读名称、真实大小和播放地址。"""
    def score(item: ExportAttachment) -> tuple[bool, bool, int]:
        stem = unquote(urlsplit(item.url).path.rsplit("/", 1)[-1])
        return (item.size > 0, item.name != stem, item.size)

    chosen, other = (right, left) if score(right) > score(left) else (left, right)
    if not chosen.playback_url and other.playback_url:
        return replace(chosen, playback_url=other.playback_url)
    return chosen


class _EmbeddedResources(HTMLParser):
    """只取正文中可下载的图片、媒体与文件链接，同时将相对地址绝对化。"""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=False)
        self.base_url = base_url
        self.parts: list[str] = []
        self.attachments: list[ExportAttachment] = []
        self._skip_until = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_until:
            return
        output = []
        values = dict(attrs)
        for key, value in attrs:
            if value and key in {"src", "poster", "href", "data-file", "data"}:
                value = (_ua_resource_url(unquote(value)) if key == "data-file"
                         else _absolute(value, self.base_url))
                # 文档课件嵌套在查看器里：真实文件在 furl 参数，按附件导出。
                if (tag in {"iframe", "embed"} and key == "src") or (tag == "object" and key == "data"):
                    nested = _nested_file_url(value)
                    if nested:
                        name = unquote(urlsplit(nested).path.rsplit("/", 1)[-1]) or tag
                        self.attachments.append(ExportAttachment(name, nested))
                        # 查看器无法离线渲染，替换成指向真实文件的链接并吞掉内部内容。
                        self._skip_until = tag
                        self.parts.append(
                            f'<p><a href="{html_module.escape(nested, quote=True)}">'
                            f"{html_module.escape(name)}</a></p>",
                        )
                        return
                downloadable = key in {"poster", "data-file"} or (
                    key == "src" and tag in {"img", "audio", "video", "source"}
                ) or (tag == "a" and key == "href" and (
                    "download" in values or bool(re.search(
                        r"\.(pdf|docx?|pptx?|xlsx?|zip|rar|7z|mp[34]|m4[av]|wav|ogg|webm|m3u8|png|jpe?g|gif|svg)$",
                        urlsplit(value).path, re.I,
                    ))
                ))
                if downloadable and value.startswith(("http://", "https://")):
                    name = unquote(urlsplit(value).path.rsplit("/", 1)[-1]) or tag
                    self.attachments.append(ExportAttachment(name, value))
            output.append(key if value is None else f'{key}="{html_module.escape(value, quote=True)}"')
        self.parts.append(f"<{tag}{' ' if output else ''}{' '.join(output)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_until:
            return
        self.handle_starttag(tag, attrs)
        # 自闭合标签没有结束事件，立即恢复，避免吞掉后续正文。
        if self._skip_until == tag:
            self._skip_until = ""

    def handle_endtag(self, tag: str) -> None:
        if self._skip_until:
            if tag == self._skip_until:
                self._skip_until = ""
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._skip_until:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._skip_until:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._skip_until:
            self.parts.append(f"&#{name};")


def embedded_resources(fragment: str, *, base_url: str) -> tuple[str, list[ExportAttachment]]:
    parser = _EmbeddedResources(base_url)
    parser.feed(fragment)
    parser.close()
    return "".join(parser.parts), parser.attachments


def parse_chapter(
    payload: Any, *, chapter_title: str, chapter_index: int, base_url: str = UA_BASE,
) -> list[ExportPage]:
    """解析章节正文和可下载资源；测验块先跳过，避免提取题目或题目附件。"""
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
                # 实际视频页的地址直接放在块上，不在 resourceDTOList 内。
                if _first(block, _RESOURCE_URL_KEYS):
                    attachment = _attachment(block, "")
                    if attachment:
                        attachments.append(attachment)
                for subtitle in _as_list(block.get("srtDTO")):
                    attachment = _attachment(subtitle, "")
                    if attachment:
                        attachments.append(attachment)
                entries = [entry for entry in _as_list(block.get("resourceDTOList")) if isinstance(entry, dict)]
                for entry in entries:
                    attachment = _attachment(entry, content)
                    if attachment:
                        attachments.append(attachment)
                if content.strip():
                    content, embedded = embedded_resources(content, base_url=base_url)
                    attachments.extend(embedded)
                    fragments.append(content)
            if not fragments and not attachments:
                continue
            title = _plain(page.get("content")) or f"第{len(pages) + 1}页"
            unique_attachments: dict[str, ExportAttachment] = {}
            for attachment in attachments:
                key = _resource_key(attachment.url)
                current = unique_attachments.get(key)
                unique_attachments[key] = attachment if current is None else _prefer_attachment(current, attachment)
            # 去重同时统一正文中的地址，保证别名也能改写为同一份本地文件。
            for attachment in sorted(attachments, key=lambda item: len(item.url), reverse=True):
                canonical = unique_attachments[_resource_key(attachment.url)].url
                fragments = [fragment.replace(html_module.escape(attachment.url, quote=True),
                                               html_module.escape(canonical, quote=True)) for fragment in fragments]
            markdown = "\n\n".join(part for fragment in fragments
                                     if (part := html_to_markdown(fragment, base_url=base_url)))
            pages.append(ExportPage(
                chapter=chapter_title,
                chapter_index=chapter_index,
                page_index=len(pages) + 1,
                title=title,
                content_type=content_type,
                markdown=markdown,
                html=render_page_html(chapter=chapter_title, title=title, fragments=fragments),
                attachments=tuple(unique_attachments.values()),
                skipped_blocks=skipped,
            ))
    return pages


# ----------------------------------------------------------------- 资源库
class CoursewareLibrary:
    """把导出结果写入本地资源库，并用标记文件避免同名课程互相覆盖。"""

    def __init__(self, root: Path, *, redact: Callable[[object], str] = str) -> None:
        self.root = Path(root)
        self._redact = redact

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
        files: list[str] = []
        failures: list[dict[str, str]] = []
        local_links: dict[str, str] = {}
        identities: dict[str, str] = {}
        identity_path = folder / FILE_DIR / f"{page.page_index:02d}" / ".sources.json"
        try:
            previous_identities = json.loads(identity_path.read_text(encoding="utf-8"))
            if not isinstance(previous_identities, dict):
                previous_identities = {}
        except (OSError, ValueError):
            previous_identities = {}
        used: set[str] = set()
        for attachment in page.attachments:
            if download is None:
                break
            name = _unique_name(_attachment_name(attachment), used)
            # 各页拥有各自的附件目录，同章其它页面不会覆盖或误用同名文件。
            target = folder / FILE_DIR / f"{page.page_index:02d}" / name
            used.add(name.lower())
            # 文件名和大小相同也可能是另一份附件；来源只存哈希，避免认证 URL 落盘。
            identity = hashlib.sha256(_resource_key(attachment.url).encode("utf-8")).hexdigest()
            if not (previous_identities.get(name) == identity and attachment.size
                    and target.is_file() and target.stat().st_size == attachment.size):
                try:
                    download(attachment.url, target)
                except Exception as error:  # 单个附件失败不能影响正文导出
                    download_error = error
                    if attachment.playback_url and not target.is_file():
                        name = _unique_name(f"{Path(name).stem}.mp4", used)
                        used.add(name.lower())
                        target = target.with_name(name)
                        try:
                            download(attachment.playback_url, target)
                        except Exception as playback_error:
                            download_error = RuntimeError(f"原文件：{error}；播放版：{playback_error}")
                        else:
                            download_error = None
                    if download_error is not None:
                        retained = target.is_file() and previous_identities.get(name) == identity
                        reason = ("刷新失败，已保留上次文件：" if retained else "") + self._redact(download_error)
                        failures.append({"file": name, "reason": reason[:160]})
                        if not retained:
                            continue
            files.append(target.relative_to(course_dir).as_posix())
            identities[name] = identity
            local_links[attachment.url] = _link_path(target.relative_to(folder).as_posix())
        if identities:
            _write_json(identity_path, identities)
        markdown, html = _page_links(page, local_links)
        written = 0
        unchanged = 0
        for name, text in ((f"{page.stem}.md", markdown + "\n"), (f"{page.stem}.html", html)):
            if _write_if_changed(folder / name, text.encode("utf-8")):
                written += 1
            else:
                unchanged += 1
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
            "sourceUrl": page.source_url,
            "resourceId": page.resource_id,
        }

    def read_records(self, course_dir: Path) -> list[dict[str, Any]]:
        try:
            return json.loads((course_dir / MANIFEST_NAME).read_text(encoding="utf-8"))["pages"]
        except FileNotFoundError:
            return []

    def write_manifest(
        self, course_dir: Path, *, course: Any, records: list[dict[str, Any]], state: str,
        failures: list[dict[str, Any]] | None = None, error: str = "",
    ) -> None:
        values = {
            "version": 1,
            "course": str(getattr(course, "name", "") or ""),
            "courseId": str(getattr(course, "id", "") or ""),
            "state": state,
            "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pages": records,
            "failures": failures or [],
            "error": error,
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


def _page_links(page: ExportPage, local_links: dict[str, str]) -> tuple[str, str]:
    """已下载资源使用本地相对链接，失败资源保留来源地址。"""
    markdown, html = page.markdown, page.html
    for url in sorted(local_links, key=len, reverse=True):
        local = local_links[url]
        markdown = markdown.replace(url, local)
        html = html.replace(html_module.escape(url, quote=True), html_module.escape(local, quote=True))
        html = html.replace(url, html_module.escape(local, quote=True))
    if page.attachments:
        links = []
        markdown_links = []
        for attachment in page.attachments:
            target = local_links.get(attachment.url, attachment.url)
            label = attachment.name.replace("[", "［").replace("]", "］")
            markdown_links.append(f"- [{label}]({target})")
            links.append(f'<li><a href="{html_module.escape(target, quote=True)}">'
                         f'{html_module.escape(attachment.name)}</a></li>')
        markdown = (markdown + "\n\n## 附件\n\n" + "\n".join(markdown_links)).strip()
        html += "<h3>附件</h3><ul>" + "".join(links) + "</ul>"
    return markdown, html


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
    if urlsplit(attachment.url).path.lower().endswith(".m3u8"):
        return safe_name(Path(name).stem + ".mp4")
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
    """串行课程导出队列；普通失败继续下一门，取消或登录失效停止队列。"""

    def __init__(
        self,
        *,
        root: Path,
        directory: Callable[[Any], Any],
        chapter: Callable[[str], Any],
        directory_for_options: Callable[[Any, ExportOptions], Any] | None = None,
        download: Callable[[str, Path], int] | None = None,
        authenticated: Callable[[], bool] = lambda: True,
        redact: Callable[[object], str] = str,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        self._root = Path(root)
        self._library = CoursewareLibrary(self._root, redact=redact)
        self._directory = directory
        self._directory_for_options = directory_for_options
        self._chapter = chapter
        self._download = download
        self._authenticated = authenticated
        self._redact = redact
        self._log = log
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._snapshot = self._empty()
        self._queue: list[dict[str, Any]] = []
        self._queue_state = "idle"
        self._active_index = -1

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "state": "idle", "courseId": "", "courseName": "", "chapterTotal": 0, "chapterDone": 0,
            "pageTotal": 0, "pageWritten": 0, "pageUnchanged": 0, "fileWritten": 0,
            "failures": [], "startedAt": "", "finishedAt": "", "folder": "", "error": "",
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            results = [dict(item) for item in self._queue]
            if self._active_index >= 0:
                results[self._active_index] = dict(self._snapshot)
            return json.loads(json.dumps(self._snapshot | {
                "state": self._queue_state, "results": results, "root": str(self._root),
                "courseTotal": len(results),
                "courseDone": sum(item["state"] not in {"queued", "running", "cancelling", "skipped"}
                                  for item in results),
            }))

    def start(self, course: Any, *, options: ExportOptions | None = None) -> dict[str, Any]:
        return self.start_many([course], options=options)

    def start_many(self, courses: list[Any], *, options: ExportOptions | None = None) -> dict[str, Any]:
        if not courses:
            raise ValueError("请至少选择一门课程")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.snapshot()
            self._queue = [self._empty() | {
                "courseId": str(course.id), "courseName": str(course.name), "state": "queued",
            } for course in courses]
            self._active_index = -1
            self._snapshot = self._empty()
            if not self._authenticated():
                self._snapshot = self._empty() | {"state": "auth_required", "error": "登录状态不可用，请重新登录"}
                self._queue_state = "auth_required"
                for item in self._queue:
                    item["state"] = "skipped"
                return self.snapshot()
            self._cancel = threading.Event()
            self._queue_state = "running"
            self._thread = threading.Thread(target=self._run_queue, args=(courses, options or ExportOptions()), name="courseware-export", daemon=True)
            self._thread.start()
            return self.snapshot()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            self._cancel.set()
            if self._queue_state == "running":
                self._queue_state = "cancelling"
        return self.snapshot()

    def _run_queue(self, courses: list[Any], options: ExportOptions) -> None:
        # 第一门仍进入原有取消落盘流程，保留单课程接口的行为。
        for index, course in enumerate(courses):
            with self._lock:
                if index and self._cancel.is_set():
                    break
                self._active_index = index
                self._snapshot = self._queue[index] | {
                    "state": "running", "startedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            self._run(course, options)
            with self._lock:
                self._queue[index] = dict(self._snapshot)
                if self._snapshot["state"] == "auth_required":
                    break
        with self._lock:
            for item in self._queue:
                if item["state"] == "queued":
                    item["state"] = "skipped"
            self._queue_state = ("auth_required" if self._snapshot["state"] == "auth_required" else
                                 "cancelled" if self._cancel.is_set() else
                                 "error" if all(item["state"] == "error" for item in self._queue) else "completed")

    def _run(self, course: Any, options: ExportOptions) -> None:
        """任何未预期异常都必须落到终态，不能让快照永远停在 running。"""
        try:
            self._run_course(course, options)
        except Exception as error:  # noqa: BLE001 - 后台线程没有调用方可以承接异常
            self._finish("error", self._redact(error))

    def _run_course(self, course: Any, options: ExportOptions) -> None:
        try:
            payload = (self._directory_for_options(course, options) if self._directory_for_options
                       else self._directory(course))
        except AuthRequiredError as error:
            self._finish("auth_required", self._redact(error))
            return
        except Exception as error:
            self._finish("error", self._redact(error))
            return
        course_name, chapters = parse_directory(payload)
        if {node for node in options.chapters.get(str(course.id), ()) if source_kind(node) in options.sources} - {
            str(chapter["nodeid"]) for chapter in chapters
        }:
            raise ValueError("所选目录已变化，请重新读取目录后选择")
        # 保留原章节序号；仅筛选部分章节时不得改变其已有磁盘路径。
        chapters = [dict(chapter, export_index=index) for index, chapter in enumerate(chapters, 1)
                    if options.includes(str(course.id), str(chapter["nodeid"]))]
        course_name = course_name or str(getattr(course, "name", "") or "")
        try:
            course_dir = self._library.course_dir(course_name, str(getattr(course, "id", "") or ""))
        except Exception as error:
            self._finish("error", self._redact(error))
            return
        with self._lock:
            self._snapshot.update(courseName=course_name, chapterTotal=len(chapters),
                                  folder=str(course_dir), state="running")
        records = self._library.read_records(course_dir)
        failures: list[dict[str, Any]] = [dict(item, reason=self._redact(item["reason"]))
                                         for item in payload.get("failures") or []]
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
                self._finish("auth_required", self._redact(error), records=records, course=course,
                             directory=course_dir, failures=failures)
                return
            except Exception as error:
                failures.append({"chapter": title, "reason": self._redact(error)})
                with self._lock:
                    self._snapshot["failures"] = list(failures)
                    self._snapshot["chapterDone"] = index
                continue
            if isinstance(page_payload, ExportSectionResult):
                pages = page_payload.pages
                failures.extend(dict(item, chapter=title, reason=self._redact(item["reason"]))
                                for item in page_payload.failures)
            else:
                pages = parse_chapter(page_payload, chapter_title=title, chapter_index=chapter["export_index"])
            chapter_records: list[dict[str, Any]] = []
            page_failed = isinstance(page_payload, ExportSectionResult) and bool(page_payload.failures)
            for page in pages:
                if self._cancel.is_set():
                    stopped = True
                    break
                attachments = tuple(attachment for attachment in page.attachments
                                    if attachment_kind(attachment.name, attachment.url) in options.kinds)
                if "text" not in options.kinds and not attachments:
                    continue
                page = replace(page, attachments=attachments)
                if "text" not in options.kinds:
                    page = replace(page, markdown="", html=render_page_html(chapter=page.chapter, title=page.title, fragments=[]))
                try:
                    record = self._library.write_page(course_dir, page, download=self._download)
                except Exception as error:
                    failures.append({"chapter": title, "reason": self._redact(error)})
                    page_failed = True
                    continue
                record["chapterId"] = node
                chapter_records.append(record)
                records = [old for old in records if old["path"] != record["path"]]
                records.append(record)
                failures.extend(dict(item, chapter=title) for item in record.get("failures") or [])
                with self._lock:
                    self._snapshot["pageTotal"] += 1
                    self._snapshot["pageWritten"] += int(record["written"] > 0)
                    self._snapshot["pageUnchanged"] += int(record["written"] == 0)
                    self._snapshot["fileWritten"] += len(record["files"])
                    self._snapshot["failures"] = list(failures)
            if not stopped and not page_failed and options.kinds == ExportOptions().kinds:
                # 完整刷新的章节可替换旧记录；失败或取消章节保留上次成功结果。
                current_paths = {record["path"] for record in chapter_records}
                old_folder = f"{chapter['export_index']:02d}_{safe_name(title) or '未命名章节'}"
                records = [record for record in records if record["path"] in current_paths or not (
                    record.get("chapterId") == node or (
                        not record.get("chapterId") and record["path"].split("/")[0] == old_folder
                    )
                )]
            with self._lock:
                self._snapshot["chapterDone"] = index
            if stopped:
                break
        state = "cancelled" if stopped or self._cancel.is_set() else (
            "error" if failures and not self._snapshot["pageTotal"] else "completed"
        )
        self._finish(state, "", records=records, course=course, directory=course_dir, failures=failures)

    def _finish(
        self, state: str, error: str, *, records: list[dict[str, Any]] | None = None,
        course: Any = None, directory: Path | None = None, failures: list[dict[str, Any]] | None = None,
    ) -> None:
        records = records or []
        records = sorted(records, key=lambda record: record["path"])
        with self._lock:
            failure_items = failures if failures is not None else list(self._snapshot.get("failures") or [])
        if directory is not None and course is not None:
            try:
                self._library.write_index(directory, course_name=self._snapshot.get("courseName", ""), records=records)
                self._library.write_manifest(directory, course=course, records=records, state=state,
                                             failures=failure_items, error=self._redact(error) if error else "")
            except OSError as write_error:
                error = error or f"写入清单失败：{self._redact(write_error)}"
                state = "error" if state == "completed" else state
        with self._lock:
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
            failures_note = f"，失败 {len(failure_items)} 项" if failure_items else ""
            self._log(f"课件导出完成：{summary['pageTotal']} 页，附件 {summary['fileWritten']} 个{failures_note} → {summary['folder']}",
                      "warn" if failure_items else "success")
        elif state == "cancelled":
            self._log(f"课件导出已取消，已保留 {summary['pageTotal']} 页 → {summary['folder']}", "warn")
