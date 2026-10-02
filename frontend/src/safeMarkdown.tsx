// 安全 Markdown：纯文本解析成受控的 React 节点。
// 永不使用 dangerouslySetInnerHTML；链接只允许 http/https，其余协议按纯文本渲染。
import type { ReactNode } from "react";

export type MdInline =
  | { type: "text"; text: string }
  | { type: "bold"; text: string }
  | { type: "code"; text: string }
  | { type: "link"; text: string; href: string };

export type MdBlock =
  | { type: "h1" | "h2" | "h3" | "p"; inlines: MdInline[] }
  | { type: "li"; inlines: MdInline[] }
  | { type: "code"; text: string };

const SAFE_HREF = /^https?:\/\//i;

export function safeHref(href: string): string | null {
  return SAFE_HREF.test(href.trim()) ? href.trim() : null;
}

export function parseInline(text: string): MdInline[] {
  const tokens: MdInline[] = [];
  // 顺序：链接 → 行内代码 → 粗体；未匹配的部分作为纯文本。
  const pattern = /\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`|\*\*([^*]+)\*\*/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) tokens.push({ type: "text", text: text.slice(last, match.index) });
    if (match[1] !== undefined) {
      const href = safeHref(match[2]);
      if (href) tokens.push({ type: "link", text: match[1], href });
      else tokens.push({ type: "text", text: match[1] });
    } else if (match[3] !== undefined) {
      tokens.push({ type: "code", text: match[3] });
    } else if (match[4] !== undefined) {
      tokens.push({ type: "bold", text: match[4] });
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) tokens.push({ type: "text", text: text.slice(last) });
  return tokens;
}

export function parseSafeMarkdown(source: string): MdBlock[] {
  const blocks: MdBlock[] = [];
  const lines = String(source || "").replace(/\r\n/g, "\n").split("\n");
  let paragraph: string[] = [];
  let codeBlock: string[] | null = null;

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push({ type: "p", inlines: parseInline(paragraph.join(" ")) });
      paragraph = [];
    }
  };

  for (const raw of lines) {
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
      codeBlock = [];
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      continue;
    }
    const heading = /^(#{1,3})\s+(.*)$/.exec(line.trim());
    if (heading) {
      flushParagraph();
      const level = heading[1].length;
      blocks.push({ type: (["h1", "h2", "h3"] as const)[level - 1], inlines: parseInline(heading[2]) });
      continue;
    }
    const list = /^[-*+]\s+(.*)$/.exec(line.trim());
    if (list) {
      flushParagraph();
      blocks.push({ type: "li", inlines: parseInline(list[1]) });
      continue;
    }
    paragraph.push(line.trim());
  }
  if (codeBlock !== null && codeBlock.length) blocks.push({ type: "code", text: codeBlock.join("\n") });
  flushParagraph();
  return blocks;
}

export function renderInline(inlines: MdInline[], keyPrefix: string): ReactNode[] {
  return inlines.map((inline, index) => {
    const key = `${keyPrefix}-${index}`;
    switch (inline.type) {
      case "bold":
        return <strong key={key}>{inline.text}</strong>;
      case "code":
        return <code className="md-inline-code" key={key}>{inline.text}</code>;
      case "link":
        return <a key={key} href={inline.href} target="_blank" rel="noreferrer noopener">{inline.text}</a>;
      default:
        return <span key={key}>{inline.text}</span>;
    }
  });
}

export function SafeMarkdown({ source }: { source: string }) {
  const blocks = parseSafeMarkdown(source);
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
            return <div className="md-list-item" key={key}>{renderInline(block.inlines, key)}</div>;
          case "code":
            return <pre className="md-code" key={key}>{block.text}</pre>;
          default:
            return <p className="md-paragraph" key={key}>{renderInline(block.inlines, key)}</p>;
        }
      })}
    </div>
  );
}
