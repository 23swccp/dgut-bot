import { useEffect, useRef, useState } from "react";
import { Filter } from "lucide-react";
import { strToU8, zipSync } from "fflate";
import { DEFAULT_SETTINGS, FONT_NAMES, PAPER_PHOTOS, isPhotoPaper, layoutText, renderPage, type FontKey, type Layout, type Paper, type Settings } from "./handwritingRenderer";
import "./HandwritingPanel.css";

type Snapshot = { settings: Settings; seed: number; layout: Layout; missing: string[]; paperImage: HTMLImageElement | null };
type Coverage = Record<FontKey, number[]>;
const fonts = Object.keys(FONT_NAMES) as FontKey[];
let coveragePromise: Promise<Coverage> | undefined;
function coverage(): Promise<Coverage> {
  if (!coveragePromise) coveragePromise = fetch("/handwriting-fonts/coverage.json").then(response => { if (!response.ok) throw new Error("无法读取字体字库信息"); return response.json() as Promise<Coverage>; }).catch(error => { coveragePromise = undefined; throw error; });
  return coveragePromise;
}
const paperImages = new Map<Paper, Promise<HTMLImageElement>>();
function loadPaper(paper: Paper): Promise<HTMLImageElement | null> {
  if (!isPhotoPaper(paper)) return Promise.resolve(null);
  const cached = paperImages.get(paper);
  if (cached) return cached;
  const result = new Promise<HTMLImageElement>((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("纸张背景加载失败，请重新选择背景。"));
    image.src = PAPER_PHOTOS[paper];
  }).catch(error => { paperImages.delete(paper); throw error; });
  paperImages.set(paper, result);
  return result;
}
function save(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href = url; a.download = filename; a.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function encode(canvas: HTMLCanvasElement, format: "png" | "jpeg"): Promise<Blob> {
  return new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error("图片导出失败，请重试。")), `image/${format}`, .92));
}

export function HandwritingPanel() {
  const [text, setText] = useState("");
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
  const [seed, setSeed] = useState(20261028);
  const [page, setPage] = useState(0);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [working, setWorking] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [format, setFormat] = useState<"png" | "jpeg">("png");
  const [error, setError] = useState("");
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  function update<K extends keyof Settings>(key: K, value: Settings[K]) { setSettings(old => ({ ...old, [key]: value })); }

  useEffect(() => {
    let cancelled = false;
    if (!text.trim()) { setSnapshot(null); setError(""); setWorking(false); return; }
    setWorking(true); setError("");
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const [, , known, paperImage] = await Promise.all([document.fonts.load(`38px "hw-${settings.font}"`), document.fonts.load('38px "hw-xiaolai"'), coverage(), loadPaper(settings.paper)]);
          if (cancelled) return;
          const canvas = document.createElement("canvas"); const ctx = canvas.getContext("2d");
          if (!ctx) throw new Error("无法创建预览，请重试。");
          ctx.font = `${settings.size}px "hw-${settings.font}", "hw-xiaolai", sans-serif`;
          const layout = layoutText(text, settings, char => ctx.measureText(char).width);
          if (layout.pages.length > 100) throw new Error("页数超过 100 页，请减少空行或分次导出。");
          const selected = new Set(known[settings.font]); const fallback = new Set(known.xiaolai);
          const missing = [...new Set(Array.from(text).filter(char => !/\s/u.test(char) && !selected.has(char.codePointAt(0)!)))];
          const unavailable = missing.filter(char => !fallback.has(char.codePointAt(0)!));
          if (unavailable.length) throw new Error(`所选字体及备用字体缺少：${unavailable.slice(0,30).join(" ")}，请修改文字后重试。`);
          setSnapshot({ settings, seed, layout, missing, paperImage });
          setPage(old => Math.min(old, layout.pages.length - 1));
        } catch (e) { if (!cancelled) { setSnapshot(null); setError(e instanceof Error ? e.message : String(e)); } }
        finally { if (!cancelled) setWorking(false); }
      })();
    }, 200);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [text, settings, seed]);

  useEffect(() => {
    if (!snapshot || !canvasRef.current) return;
    try { renderPage(canvasRef.current, snapshot.layout.pages[page] || [], snapshot.settings, snapshot.seed, snapshot.layout, snapshot.paperImage); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, [snapshot, page]);

  async function download(all: boolean) {
    if (!snapshot || working || exporting) return;
    setExporting(true); setError("");
    try {
      const indexes = all ? snapshot.layout.pages.map((_, i) => i) : [page];
      const ext = format === "jpeg" ? "jpg" : "png";
      if (indexes.length === 1) {
        const canvas = document.createElement("canvas"); renderPage(canvas, snapshot.layout.pages[indexes[0]], snapshot.settings, snapshot.seed, snapshot.layout, snapshot.paperImage);
        save(await encode(canvas, format), `字迹-${indexes[0]+1}.${ext}`);
      } else {
        const files: Record<string, Uint8Array> = {};
        for (const index of indexes) {
          const canvas = document.createElement("canvas"); renderPage(canvas, snapshot.layout.pages[index], snapshot.settings, snapshot.seed, snapshot.layout, snapshot.paperImage);
          files[`page-${String(index+1).padStart(3,"0")}.${ext}`] = new Uint8Array(await (await encode(canvas, format)).arrayBuffer());
        }
        if (snapshot.settings.paper === "wrinkled") files["背景版权说明.txt"] = strToU8(`背景照片：D Sharon Pruitt / Pink Sherbet Photography，CC BY 2.0。来源：https://commons.wikimedia.org/wiki/File:Free_Wrinkled_Lined_Blank_Notebook_Paper_High_Resolution_Creative_Commons_(8077064004).jpg
授权：https://creativecommons.org/licenses/by/2.0/
照片已缩放以适应页面。
`);
        const zip = zipSync(files, { level: 0 }); save(new Blob([zip.buffer as ArrayBuffer], { type: "application/zip" }), "字迹图片.zip");
      }
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setExporting(false); }
  }
  const disabled = !snapshot || working || exporting || !!error;
  return <article className="card handwriting-card">
    <div className="homework-card-title"><h3>字迹生成器</h3><p>输入文字，实时预览并导出图片。</p></div>
    <div className="handwriting-workspace">
      <div className="handwriting-editor"><label className="homework-input-label" htmlFor="handwriting-text">输入文字</label><textarea id="handwriting-text" value={text} maxLength={5000} placeholder="在这里输入或粘贴文字…" onChange={e => setText(e.target.value)} /><span className="handwriting-count">{text.length} / 5000 字</span></div>
      <div className="handwriting-output"><div className="handwriting-output-head"><strong>预览</strong><button type="button" className="secondary compact" ref={triggerRef} onClick={() => dialogRef.current?.showModal()}><Filter size={16} aria-hidden="true" /> 样式设置</button></div>
        <div className="handwriting-preview">{!snapshot && <p>{working ? "正在生成预览…" : "输入文字后显示预览"}</p>}<canvas ref={canvasRef} hidden={!snapshot} aria-label={`字迹预览第 ${page+1} 页`} /></div>
        {working && <p className="handwriting-hint" role="status">正在更新预览…</p>}
        {snapshot && <div className="handwriting-pagination"><button type="button" className="secondary compact" disabled={page === 0 || working} onClick={() => setPage(p => p-1)}>上一页</button><span>第 {page+1} / {snapshot.layout.pages.length} 页</span><button type="button" className="secondary compact" disabled={page >= snapshot.layout.pages.length-1 || working} onClick={() => setPage(p => p+1)}>下一页</button></div>}
        {snapshot && snapshot.missing.length > 0 && <p className="handwriting-hint" role="status">所选字体缺少：{snapshot.missing.slice(0,30).join(" ")}。已尝试备用字体，请检查预览。</p>}
        {error && <p className="homework-result error" role="alert">{error}</p>}
        <div className="handwriting-export"><label>格式 <select value={format} onChange={e => setFormat(e.target.value as "png" | "jpeg")}><option value="png">PNG</option><option value="jpeg">JPG</option></select></label><button type="button" className="secondary compact" disabled={disabled} onClick={() => void download(false)}>下载当前页</button><button type="button" className="primary compact" disabled={disabled} onClick={() => void download(true)}>{exporting ? "正在导出…" : "下载全部"}</button></div>
      </div>
    </div>
    <dialog className="handwriting-dialog" ref={dialogRef} onClose={() => triggerRef.current?.focus()} aria-labelledby="handwriting-settings-title"><div className="handwriting-dialog-head"><h3 id="handwriting-settings-title">样式设置</h3><button type="button" className="secondary compact" onClick={() => dialogRef.current?.close()}>完成</button></div>
      <div className="handwriting-settings">
        <label>字体<select value={settings.font} onChange={e => update("font", e.target.value as FontKey)}>{fonts.map(font => <option key={font} value={font}>{FONT_NAMES[font]}</option>)}</select></label>
        <label>纸张<select value={settings.paper} onChange={e => update("paper",e.target.value as Settings["paper"])}><option value="blank">白纸</option><option value="ruled">横线纸</option><option value="grid">方格纸</option><option value="notebook">笔记本横线页（照片）</option><option value="wrinkled">褶皱信纸（照片）</option><option value="recycled">再生纸纹理（照片）</option></select></label>
        <label>扫描效果<select value={settings.scan} onChange={e => update("scan",e.target.value as Settings["scan"])}><option value="none">关闭</option><option value="clear">清晰扫描</option><option value="natural">自然扫描</option></select></label>
        <label>墨色<input type="color" value={settings.ink} onChange={e => update("ink",e.target.value)} /></label>
        {([ ["size","字号",26,54,1], ["spacing","行距",1.3,2.2,.1], ["margin","页边距",45,110,5], ["randomness","随机程度",0,100,5], ["dry","断水程度",0,100,5], ["paperWear","纸张线条断痕",0,100,5] ] as const).map(([key,label,min,max,step]) => <label key={key}>{label}<span className="handwriting-slider"><input type="range" min={min} max={max} step={step} value={settings[key]} disabled={key === "paperWear" && (settings.paper === "blank" || settings.paper === "recycled")} onChange={e => update(key,Number(e.target.value))} /><output>{settings[key]}</output></span></label>)}
      </div>{settings.paper === "wrinkled" && <p className="handwriting-paper-credit">褶皱信纸：<a href="https://commons.wikimedia.org/wiki/File:Free_Wrinkled_Lined_Blank_Notebook_Paper_High_Resolution_Creative_Commons_(8077064004).jpg" target="_blank" rel="noreferrer">D Sharon Pruitt / Pink Sherbet Photography</a>，<a href="https://creativecommons.org/licenses/by/2.0/" target="_blank" rel="noreferrer">CC BY 2.0</a>。分享生成图片时请保留署名和授权链接。</p>}<div className="handwriting-dialog-actions"><button type="button" className="secondary compact" onClick={() => setSettings(DEFAULT_SETTINGS)}>恢复默认</button><button type="button" className="secondary compact" onClick={() => setSeed(s => (s+1) >>> 0)}>重新生成随机效果</button></div>
    </dialog>
  </article>;
}
