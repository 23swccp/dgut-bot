import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import { MarkdownCodeBlock } from "./MarkdownCodeBlock";

const render = (source: string) => renderToStaticMarkup(
  <ReactMarkdown components={{ pre: MarkdownCodeBlock }}>{source}</ReactMarkdown>,
);

describe("Markdown code fences", () => {
  it("turns Mermaid fences into a diagram with loading state and original source", () => {
    const html = render("```mermaid\nflowchart LR\nA[读取] --> B[提交]\n```");
    expect(html).toContain('<figure class="mermaid-diagram">');
    expect(html).toContain('role="status"');
    expect(html).toContain("查看 Mermaid 源码");
    expect(html).toContain("A[读取] --&gt; B[提交]");
    expect(html).not.toContain("<pre><figure");
  });

  it("keeps other languages, unlabelled fences and inline code unchanged", () => {
    const html = render("```python\nprint('mermaid')\n```\n\n```\nflowchart LR\nA-->B\n```\n\n`mermaid`" );
    expect(html).toContain('<pre><code class="language-python">');
    expect(html).toContain("<pre><code>flowchart LR");
    expect(html).toContain("<code>mermaid</code>");
    expect(html).not.toContain("mermaid-diagram");
  });
});
