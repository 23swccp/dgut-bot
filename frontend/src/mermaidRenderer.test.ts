import { beforeEach, describe, expect, it, vi } from "vitest";

const { initialize, render } = vi.hoisted(() => ({ initialize: vi.fn(), render: vi.fn() }));
vi.mock("mermaid", () => ({ default: { initialize, render } }));

beforeEach(() => { vi.resetModules(); initialize.mockReset(); render.mockReset(); });

describe("Mermaid renderer", () => {
  it("loads on demand, initializes once and gives each diagram a unique SVG id", async () => {
    const { renderMermaid } = await import("./mermaidRenderer");
    expect(initialize).not.toHaveBeenCalled();
    render.mockResolvedValue({ svg: "<svg>图</svg>" });
    const container = {} as HTMLElement;
    expect(await renderMermaid("flowchart LR; A-->B", container)).toBe("<svg>图</svg>");
    await renderMermaid("sequenceDiagram\nA->>B: hello", container);
    expect(initialize).toHaveBeenCalledTimes(1);
    expect(initialize).toHaveBeenCalledWith(expect.objectContaining({ startOnLoad: false, securityLevel: "strict", suppressErrorRendering: true }));
    expect(render.mock.calls[0][0]).not.toBe(render.mock.calls[1][0]);
    expect(render.mock.calls[0].slice(1)).toEqual(["flowchart LR; A-->B", container]);
  });

  it("serializes diagrams and continues after a failed render without changing the error", async () => {
    const { renderMermaid } = await import("./mermaidRenderer");
    let reject!: (error: Error) => void;
    render.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }))
      .mockResolvedValueOnce({ svg: "<svg>next</svg>" });
    const failure = new Error("Parse error on line 2");
    const first = renderMermaid("bad diagram", {} as HTMLElement);
    const firstError = first.catch(error => error);
    const second = renderMermaid("flowchart LR; A-->B", {} as HTMLElement);
    await vi.waitFor(() => expect(render).toHaveBeenCalledTimes(1));
    reject(failure);
    expect(await firstError).toBe(failure);
    expect(await second).toBe("<svg>next</svg>");
    expect(render).toHaveBeenCalledTimes(2);
  });
});
