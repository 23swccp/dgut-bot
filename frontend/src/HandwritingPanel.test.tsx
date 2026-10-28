import { describe, expect, it } from "vitest";
import { wrapHandwritingText } from "./HandwritingPanel";

describe("handwriting text layout", () => {
  it("wraps long lines and keeps explicit blank lines", () => {
    expect(wrapHandwritingText("你好世界\n\nABC", value => Array.from(value).length, 2)).toEqual(["你好", "世界", "", "AB", "C"]);
  });
  it("keeps an oversized character without dropping it", () => {
    expect(wrapHandwritingText("好", () => 10, 2)).toEqual(["好"]);
  });
});
