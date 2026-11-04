export type LiteralRange = { start: number; end: number };
export type PasteConversion = { text: string; literals: LiteralRange[]; count: number };

// 只转换完整公式；普通代码围栏、行内代码和未配对的分隔符保留原文。
export function convertPastedMath(source: string): PasteConversion {
  let text = "", count = 0, i = 0;
  const literals: LiteralRange[] = [];
  const appendLiteral = (value: string) => {
    for (let j = 0; j < value.length; j++) if (value[j] === "$") literals.push({ start: text.length + j, end: text.length + j + 1 });
    text += value;
  };
  const escaped = (pos: number) => { let n = 0; while (pos > 0 && source[--pos] === "\\") n++; return n % 2 === 1; };
  const findEnd = (delimiter: string, from: number, multiline: boolean) => {
    for (let j = from; j < source.length; j++) {
      if (!multiline && source[j] === "\n") return -1;
      if (source.startsWith(delimiter, j) && !escaped(j)) return j;
    }
    return -1;
  };
  while (i < source.length) {
    if ((i === 0 || source[i - 1] === "\n") && source.startsWith("```", i)) {
      const lineEnd = source.indexOf("\n", i);
      if (lineEnd !== -1) {
        const close = source.indexOf("\n```", lineEnd);
        if (close !== -1 && /^(?:\n```)[ \t]*(?:\r?\n|$)/.test(source.slice(close))) {
          const fenceEnd = close + source.slice(close).match(/^\n```[ \t]*/ )![0].length;
          const body = source.slice(lineEnd + 1, close);
          if (/^```(?:math|latex)[ \t]*\r?$/i.test(source.slice(i, lineEnd)) && body.trim()) {
            text += `$$${body}$$`; count++;
          } else appendLiteral(source.slice(i, fenceEnd));
          i = fenceEnd; continue;
        }
      }
    }
    if (source[i] === "`" && !escaped(i)) {
      let ticks = 1; while (source[i + ticks] === "`") ticks++;
      const end = source.indexOf("`".repeat(ticks), i + ticks);
      if (end !== -1) { appendLiteral(source.slice(i, end + ticks)); i = end + ticks; continue; }
    }
    let open = "", close = "", display = false;
    if (!escaped(i) && source.startsWith("$$", i)) { open = close = "$$"; display = true; }
    else if (!escaped(i) && source[i] === "$") { open = close = "$"; }
    else if (!escaped(i) && source.startsWith("\\(", i)) { open = "\\("; close = "\\)"; }
    else if (!escaped(i) && source.startsWith("\\[", i)) { open = "\\["; close = "\\]"; display = true; }
    if (open) {
      const end = findEnd(close, i + open.length, display);
      const body = end < 0 ? "" : source.slice(i + open.length, end);
      const currency = open === "$" && /\d/.test(source[i + 1] || "") && /\d/.test(source[end + 1] || "");
      const plausible = open !== "$" || (!/[`，。；：！？]/.test(body) && (!/^\d/.test(body) || !/\s/.test(body) || /[=^_\\{}+*/]/.test(body)));
      if (end >= 0 && body.trim() && !currency && plausible) {
        const delimiter = display ? "$$" : "$"; text += delimiter + body + delimiter; count++; i = end + close.length; continue;
      }
      // 未匹配的双美元符号不能降级为两个单美元符号。
      appendLiteral(open); i += open.length; continue;
    }
    appendLiteral(source[i++]);
  }
  return { text, literals, count };
}

// 仅在内部渲染文本中转义原文美元符号，编辑框仍显示粘贴的原文。
export function escapeLiteralDollars(text: string, ranges: LiteralRange[]): string {
  const positions = new Set(ranges.map(range => range.start));
  let result = "";
  for (let i = 0; i < text.length; i++) result += positions.has(i) && text[i] === "$" ? "\\$" : text[i];
  return result;
}
export function adjustLiteralRanges(before: string, after: string, ranges: LiteralRange[]): LiteralRange[] {
  let start = 0;
  while (start < before.length && start < after.length && before[start] === after[start]) start++;
  let oldEnd = before.length, newEnd = after.length;
  while (oldEnd > start && newEnd > start && before[oldEnd - 1] === after[newEnd - 1]) { oldEnd--; newEnd--; }
  const delta = newEnd - oldEnd;
  return ranges.flatMap(range => range.end <= start ? [range] : range.start >= oldEnd ? [{ start: range.start + delta, end: range.end + delta }] : []);
}
