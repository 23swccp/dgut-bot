import type { LiteElement } from "mathjax-full/js/adaptors/lite/Element.js";
import type TexError from "mathjax-full/js/input/tex/TexError.js";
import { mathjax } from "mathjax-full/js/mathjax.js";
import { TeX } from "mathjax-full/js/input/tex.js";
import { SVG } from "mathjax-full/js/output/svg.js";
import { liteAdaptor } from "mathjax-full/js/adaptors/liteAdaptor.js";
import { RegisterHTMLHandler } from "mathjax-full/js/handlers/html.js";
import { AllPackages } from "mathjax-full/js/input/tex/AllPackages.js";
import { PAGE_HEIGHT, PAGE_WIDTH, type Settings, type Token } from "./handwritingRenderer";

const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);
const tex = new TeX({ packages: AllPackages.filter(name => name !== "noerrors" && name !== "noundefined"), formatError: (_jax: unknown, error: TexError) => { throw error; } });
const document = mathjax.document("", { InputJax: tex, OutputJax: new SVG({ fontCache: "none" }) });

// 仅将成对的 $...$ 或 $$...$$ 视为公式；反斜杠转义的美元符号保留为文字。
export function splitMath(input: string): { text: string; math: boolean; display?: boolean }[] {
  const parts: { text: string; math: boolean; display?: boolean }[] = [];
  let plain = "", i = 0;
  const addPlain = () => { if (plain) { parts.push({ text: plain, math: false }); plain = ""; } };
  while (i < input.length) {
    if (input[i] === "\\" && input[i + 1] === "$") { plain += "$"; i += 2; continue; }
    if (input[i] !== "$") { plain += input[i++]; continue; }
    const delimiter = input.startsWith("$$", i) ? "$$" : "$";
    let end = i + delimiter.length;
    while (end < input.length) {
      if (input[end] === "\\") { end += 2; continue; }
      if (input.startsWith(delimiter, end)) break;
      end++;
    }
    if (end >= input.length) throw new Error("公式缺少结束符，请补全 $ 或 $$。输入美元符号请使用 \\$。");
    const formula = input.slice(i + delimiter.length, end);
    if (!formula.trim()) throw new Error("公式内容不能为空。");
    addPlain(); parts.push({ text: formula, math: true, display: delimiter === "$$" });
    i = end + delimiter.length;
  }
  addPlain(); return parts;
}

export async function prepareMath(input: string, settings: Settings): Promise<Token[]> {
  const parts = splitMath(input.replace(/\r\n?/g, "\n"));
  if (parts.filter(part => part.math).length > 100) throw new Error("公式超过 100 个，请分次生成。");
  const tokens: Token[] = [];
  let skipNewline = false;
  for (const part of parts) {
    if (!part.math) {
      const value = skipNewline && part.text.startsWith("\n") ? part.text.slice(1) : part.text;
      skipNewline = false; tokens.push(...Array.from(value, char => ({ char }))); continue;
    }
    skipNewline = false;
    let svg: string, width: number, height: number;
    try {
      const node = document.convert(part.text, { display: part.display });
      const element = adaptor.firstChild(node) as LiteElement;
      width = parseFloat(adaptor.getAttribute(element, "width") || "0") * settings.size / 2;
      height = parseFloat(adaptor.getAttribute(element, "height") || "0") * settings.size / 2;
      if (!(width > 0 && height > 0)) throw new Error("公式尺寸无效");
      adaptor.setAttribute(element, "style", `color:${settings.ink}`);
      svg = adaptor.outerHTML(element);
      if (svg.includes('data-mml-node="merror"')) throw new Error("公式语法有误，请检查命令和括号。");
    } catch (error) { throw new Error(`公式解析失败：${error instanceof Error ? error.message : String(error)}`); }
    const maxWidth = PAGE_WIDTH - 2 * settings.margin;
    const lineHeight = settings.size * settings.spacing;
    const maxHeight = (Math.floor((PAGE_HEIGHT - 2 * settings.margin) / lineHeight) - .06) * lineHeight;
    const scale = Math.min(1, maxWidth / width, maxHeight / height);
    width *= scale; height *= scale;
    const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
    let image: HTMLImageElement;
    try {
      image = await new Promise<HTMLImageElement>((resolve, reject) => {
        const img = new Image(); img.onload = () => resolve(img); img.onerror = () => reject(new Error("公式图片加载失败。")); img.src = url;
      });
    } finally { URL.revokeObjectURL(url); }
    if (part.display && tokens.length && tokens[tokens.length - 1].char !== "\n") tokens.push({ char: "\n" });
    tokens.push({ char: "", formula: { image, width, height } });
    if (part.display) { tokens.push({ char: "\n" }); skipNewline = true; }
  }
  return tokens;
}
