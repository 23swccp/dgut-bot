import { describe, expect, it } from "vitest";
import { adjustLiteralRanges, convertPastedMath, escapeLiteralDollars } from "./handwritingPaste";
import { splitMath } from "./handwritingMath";

describe("pasted Markdown formulas", () => {
  it("detects and converts inline, block and fenced formulas", () => {
    const source = String.raw`文字 $x^2$、\(a+b\)` + "\n" + String.raw`\[\frac{1}{2}\]` + "\n```math\nE=mc^2\n```";
    const result = convertPastedMath(source);
    expect(result.count).toBe(4);
    expect(result.text).toBe(String.raw`文字 $x^2$、$a+b$` + "\n" + String.raw`$$\frac{1}{2}$$` + "\n$$E=mc^2$$");
    expect(splitMath(escapeLiteralDollars(result.text, result.literals)).filter(part => part.math)).toHaveLength(4);
  });
  it("leaves code, escaped dollars, currency and incomplete formulas literal", () => {
    const source = "`$x$`\n```js\nlet x = '$a$';\n```\n" + String.raw`价格 $5 and $10，\$20；未闭合 $x`;
    const result = convertPastedMath(source);
    expect(result.count).toBe(0);
    expect(result.text).toBe(source);
    expect(splitMath(escapeLiteralDollars(result.text, result.literals)).every(part => !part.math)).toBe(true);
  });
  it("keeps literal dollars adjacent to converted formulas and tracks edits", () => {
    const source = String.raw`价格 $5，公式 \(x\)，代码 ` + '`$notMath$`';
    const result = convertPastedMath(source);
    expect(result.count).toBe(1);
    const parts = splitMath(escapeLiteralDollars(result.text, result.literals));
    expect(parts.filter(part => part.math).map(part => part.text)).toEqual(["x"]);
    const edited = "😀" + result.text;
    const shifted = adjustLiteralRanges(result.text, edited, result.literals);
    expect(splitMath(escapeLiteralDollars(edited, shifted)).filter(part => part.math)).toHaveLength(1);
    const removed = edited.replace("$5", "5");
    expect(adjustLiteralRanges(edited, removed, shifted).length).toBe(shifted.length - 1);
  });
});
