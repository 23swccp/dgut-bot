import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_SETTINGS, layoutText, PAGE_WIDTH, renderPage, wearPaperLines } from "./handwritingRenderer";

describe("handwriting layout", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("wraps and paginates without losing text", () => {
    const text = "测".repeat(1000) + "\n完成";
    const layout = layoutText(text, DEFAULT_SETTINGS, () => 38);
    expect(layout.pages.length).toBeGreaterThan(1);
    expect(layout.pages.flat().map(g => g.char).join("")).toBe(text.replace("\n", ""));
    expect(layout.pages.flat().every(g => g.x + 38 <= PAGE_WIDTH - DEFAULT_SETTINGS.margin)).toBe(true);
  });
  it("keeps blank lines and returns no page for an empty input", () => {
    expect(layoutText("", DEFAULT_SETTINGS, () => 20).pages).toEqual([]);
    const layout = layoutText("甲\n\n乙", DEFAULT_SETTINGS, () => 20);
    expect(layout.pages[0][1].y - layout.pages[0][0].y).toBeCloseTo(layout.lineHeight * 2);
  });
  it("uses the loaded photo as the page background without drawing extra rules", () => {
    const context = { fillRect: vi.fn(), drawImage: vi.fn(), beginPath: vi.fn(), stroke: vi.fn() };
    const canvas = { width: 0, height: 0, getContext: () => context } as unknown as HTMLCanvasElement;
    vi.stubGlobal("document", { createElement: () => ({ width: 0, height: 0, getContext: () => ({}) }) });
    const image = {} as CanvasImageSource;
    const settings = { ...DEFAULT_SETTINGS, paper: "notebook" as const, paperWear: 0 };
    renderPage(canvas, [], settings, 1, layoutText("a", settings, () => 20), image);
    expect(context.drawImage).toHaveBeenCalledWith(image, 0, 0, 900, 1273);
    expect(context.stroke).not.toHaveBeenCalled();
  });
  it("refuses to render a photo background that failed to load", () => {
    const canvas = { width: 0, height: 0, getContext: () => ({}) } as unknown as HTMLCanvasElement;
    const settings = { ...DEFAULT_SETTINGS, paper: "notebook" as const, paperWear: 0 };
    expect(() => renderPage(canvas, [], settings, 1, layoutText("a", settings, () => 20))).toThrow("纸张背景加载失败");
  });
  it("adds deterministic line breaks without altering blank background", () => {
    const create = () => {
      const width = 80, height = 80, data = new Uint8ClampedArray(width * height * 4).fill(255);
      for (let x = 0; x < width; x++) for (let c = 0; c < 3; c++) data[(40 * width + x) * 4 + c] = 80;
      return { width, height, data } as ImageData;
    };
    const first = create(), second = create(), untouched = create();
    wearPaperLines(first, 100, 123); wearPaperLines(second, 100, 123); wearPaperLines(untouched, 0, 123);
    expect(first.data).toEqual(second.data);
    expect(first.data).not.toEqual(untouched.data);
    expect(first.data[(20 * 80 + 20) * 4]).toBe(255);
  });
  it("reports an unavailable canvas", () => {
    const canvas = { width: 0, height: 0, getContext: () => null } as unknown as HTMLCanvasElement;
    expect(() => renderPage(canvas, [], DEFAULT_SETTINGS, 1, layoutText("a", DEFAULT_SETTINGS, () => 20))).toThrow("无法创建图片");
  });
});
