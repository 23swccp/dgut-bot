import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ReleaseMarkdown } from "./ReleaseMarkdown";

const render = (source: string, repo = "23swccp/dgut-bot") =>
  renderToStaticMarkup(<ReleaseMarkdown source={source} repo={repo} />);

describe("GitHub Release markdown", () => {
  it("recognizes Mermaid fences after HTML sanitization and keeps ordinary code intact", () => {
    const html = render("```mermaid\nflowchart LR\nA[更新] --> B[重启]\n```\n\n```python\nprint('hello')\n```");
    expect(html).toContain('<figure class="mermaid-diagram">');
    expect(html).toContain("A[更新] --&gt; B[重启]");
    expect(html).toContain('<pre><code class="language-python">');
    expect(html).not.toContain("<pre><figure");
  });
  it("links issues, PRs, contributors and full URLs", () => {
    const html = render("修复 #3，感谢 @DWDROME\n[合并改进](https://github.com/23swccp/dgut-bot/pull/6)\nhttps://example.com/release");
    expect(html).toContain('href="https://github.com/23swccp/dgut-bot/issues/3"');
    expect(html).toContain('href="https://github.com/DWDROME"');
    expect(html).toContain('href="https://github.com/23swccp/dgut-bot/pull/6"');
    expect(html).toContain('href="https://example.com/release"');
    expect(html).toContain('target="_blank" rel="noopener noreferrer"');
    expect(render("处理 #12", "another/project")).toContain('href="https://github.com/another/project/issues/12"');
  });

  it("preserves formatting, nested lists, tables and task lists", () => {
    const html = render("## 本次更新\n\n1. **改进**\n   - *子项*\n\n- [x] 已完成\n\n| 项目 | 状态 |\n| --- | --- |\n| 签到 | ~~旧~~ 新 |\n\n> 引用\n\n---");
    for (const tag of ["<h2>", "<ol>", "<ul>", "<strong>", "<em>", "<table>", "<del>", "<blockquote>", "<hr/>"]) {
      expect(html).toContain(tag);
    }
    expect(html).toContain('type="checkbox"');
    expect(html).toContain('checked=""');
  });

  it("keeps images and single line breaks from Release text", () => {
    const html = render("第一行\n第二行\n\n![更新演示](https://example.com/demo.gif)");
    expect(html).toContain("第一行<br/>\n第二行");
    expect(html).toContain('src="https://example.com/demo.gif"');
    expect(html).toContain('alt="更新演示"');
  });

  it("keeps footnote anchors connected to their targets", () => {
    const html = render("说明[^1]\n\n[^1]: 详细解释");
    const targets = [...html.matchAll(/href="#([^"]+)"/g)].map(match => match[1]);
    expect(targets).toHaveLength(2);
    for (const target of targets) expect(html).toContain(`id="${target}"`);
    const anchored = render('<a href="#details">查看详情</a><h3 id="details">详情</h3>');
    expect(anchored).toContain('href="#user-content-details"');
    expect(anchored).toContain('id="user-content-details"');
  });

  it("leaves fenced and inline code intact without creating reference links", () => {
    const html = render("```markdown\n![示例](https://example.com/image.png)\n<!-- 必须保留的注释 -->\n#3 @DWDROME\n```\n\n`![代码](https://example.com/code.png) #6 @someone`");
    expect(html).toContain("![示例](https://example.com/image.png)");
    expect(html).toContain("&lt;!-- 必须保留的注释 --&gt;");
    expect(html).toContain("![代码](https://example.com/code.png) #6 @someone</code>");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<a ");
  });

  it("supports safe HTML while rejecting scripts, event handlers and dangerous URLs", () => {
    const html = render('<details><summary>详情</summary><p>展开内容</p></details>\n\n<img src="https://example.com/demo.png" onerror="alert(1)">\n<script>alert(2)</script>\n\n[危险链接](javascript:alert%283%29)\n<!-- 不显示的注释 -->');
    expect(html).toContain("<details><summary>详情</summary><p>展开内容</p></details>");
    expect(html).toContain('src="https://example.com/demo.png"');
    expect(html).not.toMatch(/<script|onerror|javascript:|alert\(2\)|不显示的注释/);
    expect(html).toContain("危险链接");
  });
});
