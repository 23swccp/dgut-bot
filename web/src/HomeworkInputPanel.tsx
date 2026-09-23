import { useEffect, useRef, useState } from "react";
import { HomeworkReviewPanel } from "./HomeworkReviewPanel";

type HomeworkState = "checking" | "ready" | "missing" | "ambiguous" | "disconnected";
type HomeworkStatus = { state: HomeworkState | "sent"; message: string; pasteBlocked?: boolean; count?: number };
type PeerReviewHomework = {
  id: string; homeworkId: string; courseId: string; courseName: string; teacherName: string;
  title: string; state: number; stateLabel: string; needsAction: boolean; endTime: string; url: string;
};
type HomeworkScanStatus = {
  state: "idle" | "scanning" | "completed" | "cancelled" | "error" | "auth_required";
  loggedIn: boolean; scannedCourses: number; totalCourses: number; peerReviewCount: number; pendingReviewCount: number;
  groups: { courseId: string; courseName: string; items: PeerReviewHomework[] }[];
  failures: { courseId: string; courseName: string; reason: string }[];
  cachedAt: string; fromCache: boolean; error: string;
};
type BulkReviewSubmit = {
  candidateAssignments: number; submittedAssignments: number; submittedReviews: number;
  skippedAssignments: number; failures: { itemId: string; title: string; reason: string }[]; message: string;
};
type BulkReviewCandidates = {
  items: { itemId: string; courseName: string; title: string; reviewCount: number }[];
  failures: { itemId: string; title: string; reason: string }[];
};
type CommandResult = {
  ok: boolean; error?: string; homework?: HomeworkStatus; homeworkScan?: HomeworkScanStatus;
  bulkReviewSubmit?: BulkReviewSubmit; bulkReviewCandidates?: BulkReviewCandidates;
};

const emptyScan: HomeworkScanStatus = {
  state: "idle", loggedIn: false, scannedCourses: 0, totalCourses: 0, peerReviewCount: 0,
  pendingReviewCount: 0, groups: [], failures: [], cachedAt: "", fromCache: false, error: "",
};

async function command(name: string, payload: Record<string, unknown> = {}): Promise<CommandResult> {
  const response = await fetch("/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command: name, payload }),
  });
  return await response.json() as CommandResult;
}

function deadline(value: string): string {
  if (!value) return "未提供截止时间";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : `截止 ${date.toLocaleString("zh-CN", { hour12: false })}`;
}

export function HomeworkInputPanel() {
  const [text, setText] = useState("");
  const [status, setStatus] = useState<HomeworkStatus>({ state: "checking", message: "正在识别作业输入框…" });
  const [scan, setScan] = useState<HomeworkScanStatus>(emptyScan);
  const [busy, setBusy] = useState(false);
  const [expandedCourseIds, setExpandedCourseIds] = useState<Set<string>>(() => new Set());
  const [reviewingItemId, setReviewingItemId] = useState("");
  const [bulkSubmitting, setBulkSubmitting] = useState(false);
  const [bulkMessage, setBulkMessage] = useState<{ good: boolean; text: string } | null>(null);
  const promptedScanKey = useRef("");
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

  useEffect(() => {
    let disposed = false;
    if (scan.state === "scanning") {
      setBulkMessage(null);
      return () => { disposed = true; };
    }
    if (scan.state !== "completed" || scan.pendingReviewCount < 1) return () => { disposed = true; };
    const pendingIds = scan.groups.flatMap(group => group.items).filter(item => item.needsAction).map(item => item.id);
    const key = `${scan.cachedAt}|${pendingIds.join(",")}`;
    if (!pendingIds.length || promptedScanKey.current === key) return () => { disposed = true; };
    promptedScanKey.current = key;
    void command("get_pending_homework_review_candidates").then(response => {
      if (disposed) return;
      if (!response.ok || !response.bulkReviewCandidates) {
        setBulkMessage({ good: false, text: response.error || "互评时间检查失败" });
        return;
      }
      const candidates = response.bulkReviewCandidates.items;
      if (!candidates.length) return;
      const list = candidates.map((item, index) => `${index + 1}. 课程：${item.courseName}\n   作业：${item.title}`).join("\n\n");
      const confirmed = window.confirm(
        `以下互评作业将填写 100 分并提交：\n\n${list}\n\n已有评分不会修改；提交后会真实影响互评结果。是否继续？`,
      );
      if (confirmed) void submitPending100(candidates.map(item => item.itemId));
    }).catch(() => {
      if (!disposed) setBulkMessage({ good: false, text: "互评时间检查失败" });
    });
    return () => { disposed = true; };
  }, [scan.state, scan.cachedAt, scan.pendingReviewCount]);

  useEffect(() => {
    let disposed = false;
    let pulling = false;
    const pull = async () => {
      if (pulling) return;
      pulling = true;
      try {
        const response = await command("get_homework_scan_status");
        if (!disposed && response.homeworkScan) setScan(response.homeworkScan);
      } finally { pulling = false; }
    };
    void command("start_homework_scan").then(response => {
      if (!disposed && response.homeworkScan) setScan(response.homeworkScan);
    }).catch(() => { if (!disposed) setScan(current => ({ ...current, state: "error", error: "本地服务连接失败" })); });
    const timer = window.setInterval(() => void pull(), 750);
    return () => { disposed = true; window.clearInterval(timer); };
  }, []);

  async function refreshScan(force = true) {
    const response = await command("start_homework_scan", { force });
    if (response.homeworkScan) setScan(response.homeworkScan);
  }

  async function cancelScan() {
    const response = await command("cancel_homework_scan");
    if (response.homeworkScan) setScan(response.homeworkScan);
  }

  function toggleCourse(group: HomeworkScanStatus["groups"][number]) {
    const wasOpen = expandedCourseIds.has(group.courseId);
    setExpandedCourseIds(current => {
      const next = new Set(current);
      if (wasOpen) next.delete(group.courseId); else next.add(group.courseId);
      return next;
    });
    if (wasOpen && group.items.some(item => item.id === reviewingItemId)) setReviewingItemId("");
  }

  async function submitPending100(itemIds: string[]) {
    if (bulkSubmitting) return;
    setBulkSubmitting(true);
    setBulkMessage(null);
    try {
      const response = await command("submit_pending_homework_reviews", { itemIds });
      if (!response.ok || !response.bulkReviewSubmit) throw new Error(response.error || "100 分互评提交失败");
      const value = response.bulkReviewSubmit;
      const good = value.failures.length === 0;
      const failures = value.failures.map(item => `${item.title}：${item.reason}`).join("；");
      setBulkMessage({ good, text: failures ? `${value.message}。${failures}` : value.message });
    } catch (error) {
      setBulkMessage({ good: false, text: error instanceof Error ? error.message : "100 分互评提交失败" });
    } finally { setBulkSubmitting(false); }
  }

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

  const summary = scan.state === "idle" ? "准备扫描全部课程"
    : scan.state === "scanning"
    ? `正在扫描 ${scan.scannedCourses} / ${scan.totalCourses} 门课程，已发现 ${scan.peerReviewCount} 个互评作业`
    : scan.state === "auth_required" ? "等待登录后扫描全部课程"
      : scan.state === "error" ? (scan.error || "作业读取失败")
        : scan.peerReviewCount === 0 ? "未发现处于互评阶段的作业"
          : scan.pendingReviewCount > 0 ? `发现 ${scan.pendingReviewCount} 个待互评作业`
            : `发现 ${scan.peerReviewCount} 个互评中的作业`;

  return <section className="settings-page homework-page" aria-label="作业">
    <div className="page-intro"><div><h2>作业</h2><p>扫描全部课程的互评作业，也可以向当前打开的作业框输入文字。</p></div></div>
    <article className="card homework-scan-card">
      <div className="homework-card-head">
        <div><h3>互评扫描</h3><p>{summary}{scan.fromCache ? " · 已显示 5 分钟内缓存" : ""}</p></div>
        {scan.state === "scanning"
          ? <button type="button" className="secondary compact" onClick={() => void cancelScan()}>取消</button>
          : <button type="button" className="secondary compact" onClick={() => void refreshScan(true)}>扫描全部课程</button>}
      </div>
      {scan.state === "auth_required" && <div className="homework-scan-message warning">请先在程序浏览器中登录优学院并读取课程，再重新扫描。</div>}
      {scan.state === "scanning" && <div className="homework-scan-progress"><i style={{ width: `${scan.totalCourses ? scan.scannedCourses / scan.totalCourses * 100 : 0}%` }} /></div>}
      {scan.state === "completed" && scan.peerReviewCount === 0 && <div className="homework-scan-empty">✓ 当前没有发现“未互评”或“互评中”的作业</div>}
      {bulkMessage && <div className={`homework-scan-message ${bulkMessage.good ? "" : "warning"}`} role="status">{bulkMessage.text}</div>}
      <div className="peer-review-groups">
        {scan.groups.map((group, groupIndex) => {
          const courseOpen = expandedCourseIds.has(group.courseId);
          const teachers = Array.from(new Set(group.items.map(item => item.teacherName).filter(Boolean))).join("、") || "未知教师";
          return <section className={`peer-review-group course-tone-${groupIndex % 4} ${courseOpen ? "open" : ""}`} key={group.courseId}>
          <h4><span className="course-review-identity"><i aria-hidden="true">{String(groupIndex + 1).padStart(2, "0")}</i><span><strong>{group.courseName}</strong><small>{teachers}</small></span></span><button
            type="button"
            className={`course-review-toggle ${courseOpen ? "open" : ""}`}
            aria-label={courseOpen ? `收起${group.courseName}的作业` : `展开${group.courseName}的作业`}
            aria-expanded={courseOpen}
            onClick={() => toggleCourse(group)}
          ><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 6 4.5 4 4.5-4" /></svg></button></h4>
          {courseOpen && <div className="peer-review-course-items">{group.items.map(item => <div className={`peer-review-entry ${reviewingItemId === item.id ? "open" : ""}`} key={item.id}>
            <div className="peer-review-row">
              <span><strong>{item.title}</strong><small>{item.teacherName} · {deadline(item.endTime)}</small></span>
              <b className={item.needsAction ? "pending" : "active"}>{item.stateLabel}</b>
              <button
                type="button"
                className={`peer-review-toggle ${reviewingItemId === item.id ? "open" : ""}`}
                aria-label={reviewingItemId === item.id ? "收起评分和评语" : "展开评分和评语"}
                aria-expanded={reviewingItemId === item.id}
                onClick={() => setReviewingItemId(current => current === item.id ? "" : item.id)}
              ><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 6 4.5 4 4.5-4" /></svg></button>
            </div>
            {reviewingItemId === item.id && <HomeworkReviewPanel itemId={item.id} onSubmitted={() => void refreshScan(true)} />}
          </div>)}</div>}
        </section>})}
      </div>
      {scan.failures.length > 0 && <details className="homework-scan-failures"><summary>{scan.failures.length} 门课程扫描失败</summary>{scan.failures.map(item => <p key={`${item.courseId}-${item.courseName}`}>{item.courseName}：{item.reason}</p>)}</details>}
    </article>
    <article className="card homework-card">
      <div className="homework-card-title"><h3>作业内容输入</h3><p>把文字输入到程序浏览器中当前打开的优学院作业框。</p></div>
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
