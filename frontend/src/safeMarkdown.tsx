// 安全 Markdown：纯文本解析成受控的 React 节点。
// 永不使用 dangerouslySetInnerHTML；链接只允许 http/https，其余协议按纯文本渲染。
// 覆盖面按 GitHub Release 正文的常见写法设计：标题、段落、有序/无序/缩进列表、
// 引用、分隔线、表格、代码块，行内支持粗体、删除线、代码与链接。
import type { ReactNode } from "react";

export type MdInline =
  | { type: "text"; text: string }
  | { type: "bold"; text: string }
  | { type: "strike"; text: string }
  | { type: "code"; text: string }
  | { type: "link"; text: string; href: string };

export type MdBlock =
  | { type: "h1" | "h2" | "h3" | "p"; inlines: MdInline[] }
  | { type: "li"; inlines: MdInline[]; depth: number; ordered: boolean; marker: string }
  | { type: "quote"; inlines: MdInline[] }
  | { type: "hr" }
  | { type: "table"; head: MdInline[][]; rows: MdInline[][][] }
  | { type: "code"; text: string };

const SAFE_HREF = /^https?:\/\//i;

export function safeHref(href: string): string | null {
  const trimmed = href.trim();
  return SAFE_HREF.test(trimmed) ? trimmed : null;
}

/** GitHub 正文里常见的噪声：图片（含徽章）与 HTML 注释。 */
export function stripMarkdownNoise(source: string): string {
  return String(source || "")
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/!\[[^\]]*\]\([^)\s]*\)/g, "");
}

function pushText(tokens: MdInline[], text: string, repo: string): void {
  if (!text) return;
  const pattern = repo ? /#[0-9]+|@[A-Za-z0-9][A-Za-z0-9-]*/g : /@[A-Za-z0-9][A-Za-z0-9-]*/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) tokens.push({ type: "text", text: text.slice(last, match.index) });
    const token = match[0];
    if (token.startsWith("#")) {
      tokens.push({ type: "link", text: token, href: `https://github.com/${repo}/issues/${token.slice(1)}` });
    } else {
      tokens.push({ type: "link", text: token, href: `https://github.com/${token.slice(1)}` });
    }
    last = match.index + token.length;
  }
  if (last < text.length) tokens.push({ type: "text", text: text.slice(last) });
}

export function parseInline(text: string, repo = ""): MdInline[] {
  const tokens: MdInline[] = [];
  // 顺序：链接 → 行内代码 → 粗体 → 删除线；未匹配的部分作为纯文本，再识别 #编号 / @用户。
  const pattern = /\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|~~([^~]+)~~/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) pushText(tokens, text.slice(last, match.index), repo);
    if (match[1] !== undefined) {
      const href = safeHref(match[2]);
      if (href) tokens.push({ type: "link", text: match[1], href });
      else tokens.push({ type: "text", text: match[1] });
    } else if (match[3] !== undefined) {
      tokens.push({ type: "code", text: match[3] });
    } else if (match[4] !== undefined) {
      tokens.push({ type: "bold", text: match[4] });
    } else {
      tokens.push({ type: "strike", text: match[5] });
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) pushText(tokens, text.slice(last), repo);
  return tokens;
}

const TABLE_SEPARATOR = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/;
const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

function tableCells(line: string): string[] {
  return line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(cell => cell.trim());
}

export function parseSafeMarkdown(source: string, repo = ""): MdBlock[] {
  const blocks: MdBlock[] = [];
  const lines = stripMarkdownNoise(source).replace(/\r\n/g, "\n").split("\n");
  let paragraph: string[] = [];
  let quote: string[] = [];
  let codeBlock: string[] | null = null;

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push({ type: "p", inlines: parseInline(paragraph.join(" "), repo) });
      paragraph = [];
    }
  };
  const flushQuote = () => {
    if (quote.length) {
      blocks.push({ type: "quote", inlines: parseInline(quote.join(" "), repo) });
      quote = [];
    }
  };

  for (let index = 0; index < lines.length; index += 1) {
    const raw = lines[index];
    const line = raw.trimEnd();

    if (codeBlock !== null) {
      if (line.trim().startsWith("```")) {
        blocks.push({ type: "code", text: codeBlock.join("\n") });
        codeBlock = null;
      } else {
        codeBlock.push(raw);
      }
      continue;
    }
    if (line.trim().startsWith("```")) {
      flushParagraph();
      flushQuote();
      codeBlock = [];
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      flushQuote();
      continue;
    }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
      flushParagraph();
      flushQuote();
      blocks.push({ type: "hr" });
      continue;
    }
    const heading = /^(#{1,3})\s+(.*)$/.exec(line.trim());
    if (heading) {
      flushParagraph();
      flushQuote();
      blocks.push({
        type: (["h1", "h2", "h3"] as const)[heading[1].length - 1],
        inlines: parseInline(heading[2], repo),
      });
      continue;
    }
    if (line.includes("|") && index + 1 < lines.length && TABLE_SEPARATOR.test(lines[index + 1])) {
      flushParagraph();
      flushQuote();
      const head = tableCells(line).map(cell => parseInline(cell, repo));
      const rows: MdInline[][][] = [];
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(tableCells(lines[index]).map(cell => parseInline(cell, repo)));
        index += 1;
      }
      index -= 1;
      blocks.push({ type: "table", head, rows });
      continue;
    }
    const quoted = /^\s*>\s?(.*)$/.exec(line);
    if (quoted) {
      flushParagraph();
      quote.push(quoted[1].trim());
      continue;
    }
    const item = LIST_ITEM.exec(line);
    if (item) {
      flushParagraph();
      flushQuote();
      const depth = Math.min(3, Math.floor(item[1].length / 2));
      const ordered = /^\d/.test(item[2]);
      blocks.push({
        type: "li", inlines: parseInline(item[3], repo), depth, ordered,
        marker: ordered ? item[2].replace(/[.)]$/, ".") : "-",
      });
      continue;
    }
    flushQuote();
    paragraph.push(line.trim());
  }
  if (codeBlock !== null && codeBlock.length) blocks.push({ type: "code", text: codeBlock.join("\n") });
  flushParagraph();
  flushQuote();
  return blocks;
}

export function renderInline(inlines: MdInline[], keyPrefix: string): ReactNode[] {
  return inlines.map((inline, index) => {
    const key = `${keyPrefix}-${index}`;
    switch (inline.type) {
      case "bold":
        return <strong key={key}>{inline.text}</strong>;
      case "strike":
        return <del key={key}>{inline.text}</del>;
      case "code":
        return <code className="md-inline-code" key={key}>{inline.text}</code>;
      case "link":
        return <a key={key} href={inline.href} target="_blank" rel="noreferrer noopener">{inline.text}</a>;
      default:
        return <span key={key}>{inline.text}</span>;
    }
  });
}

export function SafeMarkdown({ source, repo = "" }: { source: string; repo?: string }) {
  const blocks = parseSafeMarkdown(source, repo);
  return (
    <div className="safe-markdown">
      {blocks.map((block, index) => {
        const key = `md-${index}`;
        switch (block.type) {
          case "h1":
            return <h4 key={key}>{renderInline(block.inlines, key)}</h4>;
          case "h2":
            return <h5 key={key}>{renderInline(block.inlines, key)}</h5>;
          case "h3":
            return <h6 key={key}>{renderInline(block.inlines, key)}</h6>;
          case "li":
            return <div className={`md-list-item ${block.ordered ? "md-ordered" : ""}`} key={key}
              style={block.depth ? { paddingLeft: block.depth * 16 } : undefined}>
              {block.ordered && <span className="md-marker">{block.marker}</span>}
              {renderInline(block.inlines, key)}
            </div>;
          case "quote":
            return <blockquote className="md-quote" key={key}>{renderInline(block.inlines, key)}</blockquote>;
          case "hr":
            return <hr className="md-hr" key={key}/>;
          case "table":
            return <table className="md-table" key={key}>
              <thead><tr>{block.head.map((cell, cellIndex) => <th key={`${key}-h${cellIndex}`}>{renderInline(cell, `${key}-h${cellIndex}`)}</th>)}</tr></thead>
              <tbody>{block.rows.map((row, rowIndex) => <tr key={`${key}-r${rowIndex}`}>{row.map((cell, cellIndex) => <td key={`${key}-r${rowIndex}c${cellIndex}`}>{renderInline(cell, `${key}-r${rowIndex}c${cellIndex}`)}</td>)}</tr>)}</tbody>
            </table>;
          case "code":
            return <pre className="md-code" key={key}>{block.text}</pre>;
          default:
            return <p className="md-paragraph" key={key}>{renderInline(block.inlines, key)}</p>;
        }
      })}
    </div>
  );
}