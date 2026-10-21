import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { parseSafeMarkdown, SafeMarkdown, stripMarkdownNoise } from "./safeMarkdown";

const repo = "23swccp/dgut-bot";

describe("safe markdown for release notes", () => {
  it("renders bullets, headings and paragraphs like GitHub does", () => {
    const blocks = parseSafeMarkdown("## 本次更新\n\n- 第一条\n- 第二条\n", repo);
    expect(blocks.map(block => block.type)).toEqual(["h2", "li", "li"]);
    expect(renderToStaticMarkup(<SafeMarkdown source={"## 本次更新\n\n- 第一条"} />)).toContain("本次更新</span></h5>");
  });

  it("keeps ordered lists and nested indentation", () => {
    const blocks = parseSafeMarkdown("1. 第一步\n2. 第二步\n  - 子项\n", repo);
    const list = blocks.filter(block => block.type === "li");
    expect(list).toHaveLength(3);
    expect(list[0]).toMatchObject({ ordered: true, marker: "1.", depth: 0 });
    expect(list[2]).toMatchObject({ ordered: false, depth: 1 });
  });

  it("supports tables, quotes and horizontal rules", () => {
    const blocks = parseSafeMarkdown("| 项目 | 说明 |\n| --- | --- |\n| 签到 | 可选课堂 |\n\n> 引用一句\n\n---\n", repo);
    expect(blocks.map(block => block.type)).toEqual(["table", "quote", "hr"]);
    const table = blocks[0] as { head: unknown[]; rows: unknown[][] };
    expect(table.head).toHaveLength(2);
    expect(table.rows).toHaveLength(1);
    const html = renderToStaticMarkup(<SafeMarkdown source={"| a | b |\n| --- | --- |\n| 1 | 2 |"} />);
    expect(html).toContain("<table class=\"md-table\">");
    expect(html).toContain("<td><span>1</span></td>");
  });

  it("renders inline bold, code, strike and safe links", () => {
    const html = renderToStaticMarkup(<SafeMarkdown source={"**加粗** 与 `代码` 与 ~~删除~~ 与 [链接](https://example.com)"} />);
    expect(html).toContain("<strong>加粗</strong>");
    expect(html).toContain("md-inline-code");
    expect(html).toContain("<del>删除</del>");
    expect(html).toContain('href="https://example.com"');
  });

  it("turns issue references and users into repository links", () => {
    const html = renderToStaticMarkup(<SafeMarkdown source={"修复某问题 #1250 @long098sdo"} repo={repo} />);
    expect(html).toContain('href="https://github.com/23swccp/dgut-bot/issues/1250"');
    expect(html).toContain('href="https://github.com/long098sdo"');
  });

  it("drops images, html comments and unsafe link targets", () => {
    expect(stripMarkdownNoise("![badge](https://img.shields.io/x) 正文 <!-- 注释 -->")).toBe(" 正文 ");
    const html = renderToStaticMarkup(<SafeMarkdown source={"[点我](javascript:alert(1))"} />);
    expect(html).not.toContain("javascript:");
    expect(html).toContain("点我");
  });

  it("keeps fenced code blocks verbatim", () => {
    const html = renderToStaticMarkup(<SafeMarkdown source={"```\n- 不是列表\n# 不是标题\n```"} />);
    expect(html).toContain("<pre class=\"md-code\">");
    expect(html).toContain("- 不是列表");
  });
});