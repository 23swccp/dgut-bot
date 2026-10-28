import { describe, expect, it } from "vitest";
import { DEFAULT_SETTINGS, layoutText, PAGE_WIDTH, renderPage } from "./handwritingRenderer";

describe("handwriting layout", () => {
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
  it("reports an unavailable canvas", () => {
    const canvas = { width: 0, height: 0, getContext: () => null } as unknown as HTMLCanvasElement;
    expect(() => renderPage(canvas, [], DEFAULT_SETTINGS, 1, layoutText("a", DEFAULT_SETTINGS, () => 20))).toThrow("无法创建图片");
  });
});
