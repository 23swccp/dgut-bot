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

// MathJax 负责结构布局；常用字母和数字换成已加载的手写字体，其他数学符号保留原字形。
export function replaceMathGlyphs(svg: string, glyph: (char: string) => { url: string; width: number } | null, randomness: number, seed: number): string {
  let index = 0;
  return svg.replace(/<path data-c="([0-9A-F]+)" d="[^"]*"><\/path>/g, (original, hex: string) => {
    const char = String.fromCodePoint(parseInt(hex, 16)).normalize("NFKC");
    if (!/^[A-Za-z0-9+=()\-]$/.test(char)) return original;
    const image = glyph(char); if (!image) return original;
    const n = Math.sin((++index + seed) * 127.1) * 43758.5453;
    const dy = ((n - Math.floor(n)) - .5) * 24 * randomness / 100;
    const width = Math.min(image.width, /[+=]/.test(char) ? 700 : 500);
    return `<image href="${image.url}" x="0" y="-960" width="${width}" height="1280" transform="translate(0 ${dy.toFixed(2)}) scale(1 -1)"/>`;
  });
}
function makeGlyphFactory(settings: Settings, selected: Set<number>, fallback: Set<number>): (char: string) => { url: string; width: number } | null {
  const canvas = globalThis.document.createElement("canvas"); canvas.width = 150; canvas.height = 128;
  const context = canvas.getContext("2d"); if (!context) throw new Error("无法创建公式文字图层。");
  const cache = new Map<string, { url: string; width: number }>();
  return char => {
    const code = char.codePointAt(0)!;
    if (!selected.has(code) && !fallback.has(code)) return null;
    const saved = cache.get(char); if (saved) return saved;
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.font = `100px "hw-${settings.font}", "hw-xiaolai", sans-serif`;
    context.textBaseline = "alphabetic"; context.fillStyle = settings.ink;
    context.fillText(char, 4, 96);
    const pixelWidth = Math.min(150, Math.ceil(context.measureText(char).width + 8));
    const glyphCanvas = globalThis.document.createElement("canvas"); glyphCanvas.width = pixelWidth; glyphCanvas.height = 128;
    const glyphContext = glyphCanvas.getContext("2d"); if (!glyphContext) throw new Error("无法创建公式文字图层。");
    glyphContext.drawImage(canvas, 0, 0);
    const result = { url: glyphCanvas.toDataURL("image/png"), width: pixelWidth * 10 }; cache.set(char, result); return result;
  };
}

export async function prepareMath(input: string, settings: Settings, seed = 0, selected = new Set<number>(), fallback = new Set<number>()): Promise<Token[]> {
  const parts = splitMath(input.replace(/\r\n?/g, "\n"));
  if (parts.filter(part => part.math).length > 100) throw new Error("公式超过 100 个，请分次生成。");
  const tokens: Token[] = [];
  const glyphFactory = parts.some(part => part.math) && (selected.size > 0 || fallback.size > 0) ? makeGlyphFactory(settings, selected, fallback) : null;
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
      if (glyphFactory) svg = replaceMathGlyphs(svg, glyphFactory, settings.randomness, seed + tokens.length);
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
