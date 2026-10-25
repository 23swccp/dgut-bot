import { useCallback, useEffect, useRef, useState } from "react";
import "./logs.css";

export type LogEvent = {
  seq: number; time: string; level: string; module: string;
  category: string; code: string; message: string;
};

type Caller = (command: string, payload?: Record<string, unknown>) => Promise<Record<string, unknown> & { ok: boolean; error?: string }>;

const MODULE_LABELS: Record<string, string> = {
  sign: "签到", course: "刷课", courseware: "课件导出", homework: "作业", agent: "Agent", update: "更新", app: "程序",
};

const LEVEL_LABELS: Record<string, string> = { info: "信息", success: "成功", warning: "警告", error: "错误" };

function logClock(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const pad = (input: number) => String(input).padStart(2, "0");
  return `${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;
}

export function LogPanel({ call }: { call: Caller }) {
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [latestSeq, setLatestSeq] = useState(0);
  const [follow, setFollow] = useState(true);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [report, setReport] = useState<{ path: string; count: number } | null>(null);
  const [message, setMessage] = useState("");
  const listRef = useRef<HTMLDivElement | null>(null);
  const autoScrollRef = useRef(true);
  // 调用方每次渲染都会新建 call，放进依赖会让 1.5 秒轮询和初始加载无限重启。
  const callRef = useRef(call);
  useEffect(() => { callRef.current = call; }, [call]);

  const reload = useCallback(async () => {
    setLoading(true);
    const result = await callRef.current("get_log_events", { limit: 1500 });
    setLoading(false);
    if (!result.ok) { setMessage(String(result.error || "日志读取失败")); return; }
    setEvents((result.logEvents as LogEvent[]) || []);
    setLatestSeq(Number(result.latestSeq) || 0);
    setMessage("");
    autoScrollRef.current = true;
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  // 跟随最新：只增量取 afterSeq，避免整表重绘。
  useEffect(() => {
    if (!follow) return;
    let disposed = false;
    const timer = window.setInterval(async () => {
      const result = await callRef.current("get_log_events", { afterSeq: latestSeq, limit: 400 });
      if (disposed || !result.ok) return;
      const incoming = (result.logEvents as LogEvent[]) || [];
      setLatestSeq(current => Math.max(current, Number(result.latestSeq) || 0));
      if (incoming.length) setEvents(items => [...items, ...incoming].slice(-1600));
    }, 1500);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [follow, latestSeq]);

  useEffect(() => {
    const target = listRef.current;
    if (target && autoScrollRef.current) target.scrollTop = target.scrollHeight;
  }, [events]);

  async function exportReport() {
    setExporting(true);
    const result = await callRef.current("export_log_report", {});
    setExporting(false);
    if (!result.ok) { setMessage(String(result.error || "导出失败")); return; }
    const info = result.report as { path: string; count: number } | undefined;
    if (info) { setReport(info); setMessage(""); }
  }

  async function openReport() {
    if (!report) return;
    const result = await callRef.current("open_log", { path: report.path });
    if (!result.ok) setMessage(String(result.error || "无法打开导出文件"));
  }

  return <section className="settings-page log-page">
    <div className="page-intro"><div><h2>运行日志</h2></div></div>
    <div className="settings-surface">
      <article className="card log-card">
        <div className="log-actions">
          <button type="button" className={`secondary compact ${follow ? "on" : ""}`} aria-pressed={follow} onClick={() => setFollow(!follow)}>{follow ? "跟随中" : "已暂停"}</button>
          <button type="button" className="secondary compact" disabled={loading} onClick={() => void reload()}>刷新</button>
          <button type="button" className="primary compact" disabled={exporting} onClick={() => void exportReport()}>{exporting ? "导出中…" : "导出日志"}</button>
        </div>
        <p className="log-summary">
          共 {events.length} 条{report ? ` · 已导出 ${report.count} 条` : ""}
          {message && <span className="log-error"> · {message}</span>}
          {report && <button type="button" className="link-button" onClick={() => void openReport()}>打开导出文件</button>}
          {report && <code className="log-report-path">{report.path}</code>}
        </p>
        <div className="log-list" ref={listRef} onScroll={event => {
          const target = event.currentTarget;
          autoScrollRef.current = target.scrollHeight - target.scrollTop - target.clientHeight < 48;
        }} aria-label="运行日志">
          {events.length === 0 && <div className="log-empty">{loading ? "正在读取日志…" : "暂无日志。"}</div>}
          {events.map(event => <div className={`log-row ${event.level}`} key={event.seq}>
            <time>{logClock(event.time)}</time>
            <span className={`log-level ${event.level}`}>{LEVEL_LABELS[event.level] || event.level}</span>
            <span className="log-module">{MODULE_LABELS[event.module] || event.module}</span>
            <span className="log-message">{event.message}</span>
            {event.code && event.code !== "LEGACY_LOG" && <code>{event.code}</code>}
          </div>)}
        </div>
      </article>
    </div>
  </section>;
}
