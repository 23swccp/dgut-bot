import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_SETTINGS, layoutText, layoutTokens, PAGE_WIDTH, renderPage, wearPaperLines } from "./handwritingRenderer";

import { prepareMath, replaceMathGlyphs, splitMath } from "./handwritingMath";

describe("handwriting layout", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("parses mixed text and formula delimiters, preserving escaped dollars", () => {
    expect(splitMath(String.raw`价格 \$5，$x^2$ 和 $$\frac{a}{b}$$`)).toEqual([
      { text: "价格 $5，", math: false }, { text: "x^2", math: true, display: false },
      { text: " 和 ", math: false }, { text: String.raw`\frac{a}{b}`, math: true, display: true },
    ]);
    expect(() => splitMath("文字 $x")).toThrow("缺少结束符");
  });
  it("uses handwriting glyphs while retaining unsupported mathematical symbols", () => {
    const svg = '<path data-c="1D465" d="x"></path><path data-c="32" d="two"></path><path data-c="2211" d="sum"></path>';
    const output = replaceMathGlyphs(svg, char => ({ url: `data:image/png;base64,${char}`, width: 500 }), 25, 1);
    expect(output).toContain('base64,x');
    expect(output).toContain('base64,2');
    expect(output).toContain('data-c="2211"');
    expect(replaceMathGlyphs(svg, () => null, 25, 1)).toBe(svg);
    expect(replaceMathGlyphs(svg, char => ({ url: char, width: 500 }), 25, 1)).toBe(replaceMathGlyphs(svg, char => ({ url: char, width: 500 }), 25, 1));
  });
  it("renders formulas locally and rejects invalid TeX", async () => {
    let svg = "";
    vi.stubGlobal("URL", { createObjectURL: vi.fn((blob: Blob) => { void blob.text().then(value => { svg = value; }); return "blob:test"; }), revokeObjectURL: vi.fn() });
    class FakeImage { onload: (() => void) | null = null; set src(_value: string) { queueMicrotask(() => this.onload?.()); } }
    vi.stubGlobal("Image", FakeImage);
    const tokens = await prepareMath(String.raw`解：$\frac{1}{2}$`, DEFAULT_SETTINGS);
    expect(tokens[tokens.length - 1]?.formula?.width).toBeGreaterThan(0);
    expect(svg).toContain("<svg");
    expect(svg).toContain('xmlns="http://www.w3.org/2000/svg"');
    expect(svg).toContain("<path");
    await expect(prepareMath(String.raw`$\notacommand$`, DEFAULT_SETTINGS)).rejects.toThrow("公式解析失败");
  });
  it("wraps and paginates tall formulas as indivisible items", () => {
    const formula = { image: {} as HTMLImageElement, width: 750, height: 140 };
    const layout = layoutTokens([{ char: "甲" }, { char: "", formula }, { char: "乙" }], DEFAULT_SETTINGS, () => 40);
    const glyphs = layout.pages.flat();
    expect(glyphs[1].y).toBeGreaterThan(glyphs[0].y);
    expect(glyphs[2].y).toBeGreaterThan(glyphs[1].y);
    expect(glyphs[1].y - 70).toBeGreaterThanOrEqual(DEFAULT_SETTINGS.margin);
  });
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
