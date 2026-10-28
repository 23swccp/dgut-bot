import { useRef, useState } from "react";
import "./HandwritingPanel.css";

export function wrapHandwritingText(text: string, measure: (text: string) => number, width: number): string[] {
  const lines: string[] = [];
  for (const paragraph of text.replace(/\r\n?/g, "\n").split("\n")) {
    let line = "";
    for (const char of paragraph) {
      if (line && measure(line + char) > width) { lines.push(line); line = ""; }
      line += char;
    }
    lines.push(line);
  }
  return lines;
}

export function HandwritingPanel() {
  const [text, setText] = useState("");
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const canvasRef = useRef<HTMLCanvasElement>(null);

  function generate() {
    setError("");
    setReady(false);
    if (!text.trim()) { setError("请输入要生成的文字。"); return; }
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) { setError("无法生成图片，请重试。"); return; }
    context.font = '34px KaiTi, "STKaiti", serif';
    const lines = wrapHandwritingText(text, value => context.measureText(value).width, 860);
    if (lines.length > 180) { setError("内容行数过多，请减少空行或分次生成。"); return; }
    canvas.width = 1000;
    canvas.height = Math.max(320, 120 + lines.length * 56);
    context.fillStyle = "#fffefa";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.font = '34px KaiTi, "STKaiti", serif';
    context.textBaseline = "middle";
    lines.forEach((line, index) => {
      const y = 90 + index * 56;
      context.strokeStyle = "#d9e4e6";
      context.beginPath(); context.moveTo(55, y + 25); context.lineTo(945, y + 25); context.stroke();
      context.fillStyle = "#263648";
      context.fillText(line, 70, y);
    });
    setReady(true);
  }

  function download() {
    const canvas = canvasRef.current;
    if (!canvas || !ready) return;
    canvas.toBlob(blob => {
      if (!blob) { setError("图片导出失败，请重试。"); return; }
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url; link.download = "字迹.png"; link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }, "image/png");
  }

  return <article className="card handwriting-card">
    <div className="homework-card-title"><h3>字迹生成器</h3><p>将文字排成楷体风格的纸张图片。</p></div>
    <label className="homework-input-label" htmlFor="handwriting-text">要生成的文字</label>
    <textarea id="handwriting-text" value={text} maxLength={1000} placeholder="在这里输入或粘贴文字…" onChange={event => { setText(event.target.value); setReady(false); setError(""); }} />
    <div className="handwriting-actions"><span>{text.length} / 1000 字</span><button type="button" className="primary" onClick={generate}>生成预览</button><button type="button" className="secondary" disabled={!ready} onClick={download}>下载图片</button></div>
    {error && <p className="homework-result error" role="alert">{error}</p>}
    <div className="handwriting-preview" hidden={!ready}><h4>预览</h4><canvas ref={canvasRef} aria-label="生成的字迹图片" /></div>
  </article>;
}
