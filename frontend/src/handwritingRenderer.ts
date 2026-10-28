export type FontKey = "xiaolai" | "longcang" | "cef" | "maoken";
export type Settings = { font: FontKey; paper: "blank" | "ruled" | "grid"; scan: "none" | "clear" | "natural"; size: number; spacing: number; margin: number; randomness: number; dry: number; ink: string };
export const DEFAULT_SETTINGS: Settings = { font: "xiaolai", paper: "blank", scan: "clear", size: 38, spacing: 1.7, margin: 65, randomness: 25, dry: 15, ink: "#1a1a1a" };
export const FONT_NAMES: Record<FontKey, string> = { xiaolai: "小赖字体", longcang: "龙藏体", cef: "快去写作业", maoken: "猫啃硬笔楷书" };
export const PAGE_WIDTH = 900, PAGE_HEIGHT = 1273;
export type Glyph = { char: string; x: number; y: number; index: number };
export type Layout = { pages: Glyph[][]; rowsPerPage: number; lineHeight: number };

export function layoutText(text: string, settings: Settings, measure: (char: string) => number): Layout {
  const lineHeight = settings.size * settings.spacing;
  const rowsPerPage = Math.max(1, Math.floor((PAGE_HEIGHT - 2 * settings.margin) / lineHeight));
  if (!text.length) return { pages: [], rowsPerPage, lineHeight };
  const pages: Glyph[][] = [[]];
  let row = 0, x = settings.margin, index = 0;
  const nextLine = () => { row++; x = settings.margin; if (row >= rowsPerPage) { pages.push([]); row = 0; } };
  for (const char of Array.from(text.replace(/\r\n?/g, "\n"))) {
    if (char === "\n") { nextLine(); index++; continue; }
    const width = Math.max(0, measure(char));
    if (x > settings.margin && x + width > PAGE_WIDTH - settings.margin) nextLine();
    pages[pages.length - 1].push({ char, x, y: settings.margin + row * lineHeight + lineHeight * 0.53, index });
    x += width; index++;
  }
  return { pages, rowsPerPage, lineHeight };
}
function random(seed: number, index: number, salt: number): number {
  let n = (seed ^ Math.imul(index + 1, 0x9e3779b1) ^ Math.imul(salt + 1, 0x85ebca6b)) >>> 0;
  n ^= n >>> 16; n = Math.imul(n, 0x7feb352d); n ^= n >>> 15; n = Math.imul(n, 0x846ca68b); n ^= n >>> 16;
  return (n >>> 0) / 4294967296;
}
export function renderPage(canvas: HTMLCanvasElement, glyphs: Glyph[], settings: Settings, seed: number, layout: Layout): void {
  canvas.width = PAGE_WIDTH; canvas.height = PAGE_HEIGHT;
  const ctx = canvas.getContext("2d"); if (!ctx) throw new Error("无法创建图片，请重试。");
  ctx.fillStyle = settings.scan === "natural" ? "#f8f7f2" : settings.scan === "clear" ? "#ffffff" : "#fffefa"; ctx.fillRect(0, 0, PAGE_WIDTH, PAGE_HEIGHT);
  if (settings.scan === "natural") {
    ctx.fillStyle = "rgba(95,91,80,0.035)";
    for (let i = 0; i < 650; i++) ctx.fillRect(random(seed, i, 21) * PAGE_WIDTH, random(seed, i, 22) * PAGE_HEIGHT, 1, 1);
  }
  if (settings.paper !== "blank") {
    ctx.strokeStyle = settings.scan === "clear" ? "#c4c4c4" : "#d5dfe1"; ctx.lineWidth = 1;
    for (let row = 0; row < layout.rowsPerPage; row++) { const y = settings.margin + (row + 1) * layout.lineHeight; ctx.beginPath(); ctx.moveTo(settings.margin, y); ctx.lineTo(PAGE_WIDTH-settings.margin, y); ctx.stroke(); }
    if (settings.paper === "grid") for (let x = settings.margin; x <= PAGE_WIDTH-settings.margin; x += layout.lineHeight) { ctx.beginPath(); ctx.moveTo(x, settings.margin); ctx.lineTo(x, PAGE_HEIGHT-settings.margin); ctx.stroke(); }
  }
  const tile = document.createElement("canvas"); tile.width = 160; tile.height = 160;
  const pen = tile.getContext("2d"); if (!pen) throw new Error("无法创建文字图层，请重试。");
  for (const glyph of glyphs) {
    if (/^\s$/u.test(glyph.char)) continue;
    const variation = settings.randomness / 100;
    const size = settings.size * (1 + (random(seed, glyph.index, 1)-.5) * .12 * variation);
    pen.clearRect(0, 0, 160, 160); pen.globalCompositeOperation = "source-over";
    pen.font = `${size}px "hw-${settings.font}", "hw-xiaolai", sans-serif`; pen.textBaseline = "middle"; pen.fillStyle = settings.ink;
    pen.fillText(glyph.char, 40, 80);
    if (settings.dry > 0) {
      pen.globalCompositeOperation = "destination-out";
      const count = Math.round(settings.dry / 15);
      for (let k=0; k<count; k++) {
        const salt = k*5+10; const x = 38+random(seed,glyph.index,salt)*size*1.1; const y = 80-size*.55+random(seed,glyph.index,salt+1)*size*1.1;
        pen.fillStyle = `rgba(0,0,0,${0.18+random(seed,glyph.index,salt+2)*settings.dry/125})`;
        pen.beginPath(); pen.ellipse(x,y,2+random(seed,glyph.index,salt+3)*settings.dry/10,0.7+settings.dry/40,random(seed,glyph.index,salt+4)*.5,0,Math.PI*2); pen.fill();
      }
      pen.globalCompositeOperation = "source-over";
    }
    ctx.save(); ctx.translate(glyph.x + (random(seed,glyph.index,4)-.5)*settings.size*.06*variation, glyph.y + (random(seed,glyph.index,2)-.5)*settings.size*.12*variation); ctx.rotate((random(seed,glyph.index,3)-.5)*.12*variation); ctx.drawImage(tile,-40,-80); ctx.restore();
  }
}
