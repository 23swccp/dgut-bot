import { Children, isValidElement, useEffect, useState, type ReactNode } from "react";
import type { Components } from "react-markdown";
import { renderMermaid } from "./mermaidRenderer";
import "./mermaid.css";

type DiagramState = { source: string; svg?: string; error?: string };

function MermaidDiagram({ source }: { source: string }) {
  const [result, setResult] = useState<DiagramState | null>(null);
  const [attempt, setAttempt] = useState(0);
  const current = result?.source === source ? result : null;

  useEffect(() => {
    let disposed = false;
    // 排版需要可测量的 DOM；临时节点独立于 React 管理的内容，并在结束后清理。
    const container = document.createElement("div");
    container.className = "mermaid-render-target";
    container.setAttribute("aria-hidden", "true");
    document.body.appendChild(container);
    void renderMermaid(source, container).then(svg => {
      if (!disposed) setResult({ source, svg });
    }).catch(error => {
      if (!disposed) setResult({ source, error: String(error) });
    }).finally(() => container.remove());
    return () => { disposed = true; };
  }, [source, attempt]);

  return <figure className="mermaid-diagram">
    {current?.svg ? <div className="mermaid-diagram-svg" role="img" aria-label="Mermaid 图表"
      dangerouslySetInnerHTML={{ __html: current.svg }} />
      : current?.error ? <>
        <p role="alert">流程图渲染失败：{current.error}</p>
        <button type="button" className="secondary" onClick={() => { setResult(null); setAttempt(value => value + 1); }}>重试</button>
      </> : <p role="status">流程图加载中…</p>}
    {current?.error ? <pre><code className="language-mermaid">{source}</code></pre>
      : <details><summary>查看 Mermaid 源码</summary><pre><code className="language-mermaid">{source}</code></pre></details>}
  </figure>;
}

/** 只转换明确标记为 mermaid 的代码围栏，行内代码与其他语言原样展示。 */
export const MarkdownCodeBlock: Components["pre"] = ({ node: _node, children, ...props }) => {
  const nodes = Children.toArray(children);
  const code = nodes[0];
  if (nodes.length === 1 && isValidElement<{ className?: string; children?: ReactNode }>(code)
      && /(?:^|\s)language-mermaid(?:\s|$)/.test(code.props.className || "")
      && typeof code.props.children === "string") {
    return <MermaidDiagram source={code.props.children.replace(/\n$/, "")} />;
  }
  return <pre {...props}>{children}</pre>;
};
