import { useEffect, useState } from "react";

type HomeworkState = "checking" | "ready" | "missing" | "ambiguous" | "disconnected";
type HomeworkStatus = { state: HomeworkState | "sent"; message: string; pasteBlocked?: boolean; count?: number };
type CommandResult = { ok: boolean; error?: string; homework?: HomeworkStatus };

async function command(name: string, payload: Record<string, unknown> = {}): Promise<CommandResult> {
  const response = await fetch("/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command: name, payload }),
  });
  return await response.json() as CommandResult;
}

export function HomeworkInputPanel() {
  const [text, setText] = useState("");
  const [status, setStatus] = useState<HomeworkStatus>({ state: "checking", message: "正在识别作业输入框…" });
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ good: boolean; message: string } | null>(null);

  useEffect(() => {
    let disposed = false;
    let checking = false;
    const check = async () => {
      if (checking) return;
      checking = true;
      try {
        const response = await command("get_homework_input_status");
        if (!disposed) setStatus(response.homework || { state: "disconnected", message: response.error || "无法识别作业输入框" });
      } catch {
        if (!disposed) setStatus({ state: "disconnected", message: "本地服务连接失败" });
      } finally { checking = false; }
    };
    void check();
    const timer = window.setInterval(() => void check(), 2000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, []);

  async function send() {
    if (busy || status.state !== "ready" || !text.trim()) return;
    setBusy(true);
    setResult(null);
    try {
      const response = await command("send_homework_text", { text });
      if (!response.ok) throw new Error(response.error || "输入失败");
      setResult({ good: true, message: response.homework?.message || "内容已输入作业框" });
      setText("");
    } catch (error) {
      setResult({ good: false, message: error instanceof Error ? error.message : "输入失败，请检查作业框" });
    } finally { setBusy(false); }
  }

  return <section className="settings-page homework-page" aria-label="作业输入">
    <div className="page-intro"><div><h2>作业输入</h2><p>把文字输入到程序浏览器中打开的优学院作业框。</p></div></div>
    <article className="card homework-card">
      <div className="homework-status-line">
        <span className={`homework-status-dot ${status.state}`} aria-hidden="true" />
        <div><strong>{status.state === "ready" ? "已识别输入框" : status.state === "checking" ? "识别中" : "未识别输入框"}</strong><small aria-live="polite">{status.message}{status.state === "ready" && status.pasteBlocked ? " · 当前作业限制普通粘贴" : ""}</small></div>
      </div>
      <label className="homework-input-label" htmlFor="homework-text">要输入的内容</label>
      <textarea id="homework-text" value={text} maxLength={5000} disabled={busy} placeholder="在这里输入或粘贴作业内容…" onChange={event => { setText(event.target.value); setResult(null); }} onKeyDown={event => {
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void send(); }
      }} />
      <div className="homework-actions"><span>{text.length} / 5000 字</span><button type="button" className="primary" disabled={busy || status.state !== "ready" || !text.trim()} onClick={() => void send()}>{busy ? "输入中…" : "发送"}</button></div>
      {result && <p className={`homework-result ${result.good ? "good" : "error"}`} role="status">{result.message}</p>}
      <p className="homework-note">内容会追加到作业框末尾；请在作业页面核对并自行提交。Ctrl + Enter 也可发送。</p>
    </article>
  </section>;
}
