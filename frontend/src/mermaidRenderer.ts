let enginePromise: Promise<(typeof import("mermaid"))["default"]> | undefined;
let queue: Promise<unknown> = Promise.resolve();
let sequence = 0;

function engine() {
  enginePromise ??= import("mermaid").then(({ default: mermaid }) => {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      suppressErrorRendering: true,
      theme: "neutral",
      fontFamily: '"Microsoft YaHei UI", sans-serif',
      htmlLabels: false,
    });
    return mermaid;
  }).catch(error => { enginePromise = undefined; throw error; });
  return enginePromise;
}

/** Mermaid 共享内部状态，多个 Markdown 区域的渲染需要顺序执行。 */
export function renderMermaid(source: string, container: HTMLElement): Promise<string> {
  const result = queue.then(async () => {
    const mermaid = await engine();
    const { svg } = await mermaid.render(`dgut-mermaid-${++sequence}`, source, container);
    return svg;
  });
  queue = result.catch(() => undefined);
  return result;
}
