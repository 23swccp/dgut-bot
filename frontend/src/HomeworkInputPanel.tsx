import { useEffect, useRef, useState } from "react";
import { HomeworkReviewPanel } from "./HomeworkReviewPanel";
import {
  defaultExpandedCourseIds,
  filterHomeworkGroups,
  homeworkCounts,
  HOMEWORK_FILTER_LABELS,
  isOverdue,
  isPeerReview,
  type HomeworkFilter,
  type HomeworkGroup,
  type HomeworkItem,
} from "./homeworkView";

type HomeworkState = "checking" | "ready" | "missing" | "ambiguous" | "disconnected";
type HomeworkStatus = { state: HomeworkState | "sent"; message: string; pasteBlocked?: boolean; count?: number };
type HomeworkScanStatus = {
  state: "idle" | "scanning" | "completed" | "cancelled" | "error" | "auth_required";
  loggedIn: boolean; scannedCourses: number; totalCourses: number;
  totalCount: number; pendingCount: number; peerReviewCount: number; pendingReviewCount: number; completedCount: number;
  archivedCount: number;
  groups: HomeworkGroup[];
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
  state: "idle", loggedIn: false, scannedCourses: 0, totalCourses: 0, totalCount: 0,
  pendingCount: 0, peerReviewCount: 0, pendingReviewCount: 0, completedCount: 0, archivedCount: 0,
  groups: [], failures: [], cachedAt: "", fromCache: false, error: "",
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

function stateBadgeClass(item: HomeworkItem): string {
  if (item.state === 0 || item.state === 1) return "pending";
  if (item.state === 2) return "active";
  if (item.state === 4) return "graded";
  return "other";
}

export function HomeworkInputPanel() {
  const [text, setText] = useState("");
  const [status, setStatus] = useState<HomeworkStatus>({ state: "checking", message: "正在识别作业输入框…" });
  const [scan, setScan] = useState<HomeworkScanStatus>(emptyScan);
  const [filter, setFilter] = useState<HomeworkFilter>("pending");
  const [busy, setBusy] = useState(false);
  const [expandedCourseIds, setExpandedCourseIds] = useState<Set<string>>(() => new Set());
  const [reviewingItemId, setReviewingItemId] = useState("");
  const [bulkSubmitting, setBulkSubmitting] = useState(false);
  const [submissionResult, setSubmissionResult] = useState<{ good: boolean; text: string } | null>(null);
  const [preflightError, setPreflightError] = useState("");
  const [openError, setOpenError] = useState("");
  const [archiveError, setArchiveError] = useState("");
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
      setPreflightError("");
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
        setPreflightError(response.error || "互评时间检查失败");
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
      if (!disposed) setPreflightError("互评时间检查失败");
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

  useEffect(() => {
    if (scan.state !== "completed" && scan.state !== "error") return;
    setExpandedCourseIds(defaultExpandedCourseIds(scan.groups));
  }, [scan.cachedAt, scan.state]);

  async function refreshScan(force = true) {
    const response = await command("start_homework_scan", { force });
    if (response.homeworkScan) setScan(response.homeworkScan);
  }

  async function cancelScan() {
    const response = await command("cancel_homework_scan");
    if (response.homeworkScan) setScan(response.homeworkScan);
  }

  function changeFilter(next: HomeworkFilter) {
    setFilter(next);
    setReviewingItemId("");
    setOpenError("");
    setArchiveError("");
  }

  function toggleCourse(group: HomeworkGroup) {
    const wasOpen = expandedCourseIds.has(group.courseId);
    setExpandedCourseIds(current => {
      const next = new Set(current);
      if (wasOpen) next.delete(group.courseId); else next.add(group.courseId);
      return next;
    });
    if (wasOpen && group.items.some(item => item.id === reviewingItemId)) setReviewingItemId("");
  }

  async function openHomework(item: HomeworkItem) {
    setOpenError("");
    try {
      const response = await command("start_browser", { url: item.url });
      if (!response.ok) throw new Error(response.error || "打开页面失败");
    } catch (error) {
      setOpenError(error instanceof Error ? error.message : "打开页面失败");
    }
  }

  async function setArchive(kind: "course" | "item", id: string, archived: boolean) {
    setArchiveError("");
    try {
      const response = await command("set_homework_archive", { kind, id, archived });
      if (!response.ok || !response.homeworkScan) throw new Error(response.error || "归档操作失败");
      setScan(response.homeworkScan);
      if (archived) setReviewingItemId("");
    } catch (error) {
      setArchiveError(error instanceof Error ? error.message : "归档操作失败");
    }
  }

  async function submitPending100(itemIds: string[]) {
    if (bulkSubmitting) return;
    setBulkSubmitting(true);
    setSubmissionResult(null);
    try {
      const response = await command("submit_pending_homework_reviews", { itemIds });
      if (!response.ok || !response.bulkReviewSubmit) throw new Error(response.error || "100 分互评提交失败");
      const value = response.bulkReviewSubmit;
      const good = value.failures.length === 0;
      const failures = value.failures.map(item => `${item.title}：${item.reason}`).join("；");
      setSubmissionResult({ good, text: failures ? `${value.message}。${failures}` : value.message });
    } catch (error) {
      setSubmissionResult({ good: false, text: error instanceof Error ? error.message : "100 分互评提交失败" });
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

  const counts = homeworkCounts(scan.groups);
  const visibleGroups = filterHomeworkGroups(scan.groups, filter);
  const summary = scan.state === "idle" ? "准备扫描全部课程"
    : scan.state === "scanning"
    ? `扫描中 ${scan.scannedCourses} / ${scan.totalCourses}`
    : scan.state === "auth_required" ? "等待登录后扫描全部课程"
      : scan.state === "error" ? (scan.error || "作业读取失败")
        : counts.all === 0 && counts.archived === 0 ? "没有发现作业"
          : `共 ${counts.all} 个作业，${counts.pending} 个待处理${counts.archived ? ` · ${counts.archived} 个已归档` : ""}`;
  const emptyMessage = scan.state === "error" ? (scan.error || "作业读取失败")
    : counts.all === 0 && counts.archived === 0 ? "没有发现作业。"
      : filter === "pending" ? "没有待处理作业。"
        : filter === "graded" ? "没有已批阅的作业。"
          : filter === "archived" ? "没有已归档的作业。" : "没有可以显示的作业。";

  return <section className="settings-page homework-page" aria-label="作业">
    <div className="page-intro"><div><h2>作业</h2></div></div>
    <article className="card homework-scan-card">
      <div className="homework-card-head">
        <div><h3>全部作业</h3><p>{summary}</p></div>
        {scan.state === "scanning"
          ? <button type="button" className="secondary compact" onClick={() => void cancelScan()}>取消</button>
          : <button type="button" className="secondary compact" onClick={() => void refreshScan(true)}>扫描全部课程</button>}
      </div>
      {scan.state === "auth_required" && <div className="homework-scan-message warning">请先在程序浏览器中登录优学院并读取课程，再重新扫描。</div>}
      {scan.state === "scanning" && <div className="homework-scan-progress"><i style={{ width: `${scan.totalCourses ? scan.scannedCourses / scan.totalCourses * 100 : 0}%` }} /></div>}
      {submissionResult && <div className={`homework-scan-message ${submissionResult.good ? "" : "warning"}`} role="status"><strong>提交结果</strong>{submissionResult.text}</div>}
      {preflightError && <div className="homework-scan-message warning" role="status">{preflightError}</div>}
      {openError && <div className="homework-scan-message warning" role="status">{openError}</div>}
      {archiveError && <div className="homework-scan-message warning" role="status">{archiveError}</div>}
      <div className="homework-filter-tabs" role="group" aria-label="按状态筛选作业">
        {(Object.keys(HOMEWORK_FILTER_LABELS) as HomeworkFilter[]).map(key => (
          <button
            key={key}
            type="button"
            className={`homework-filter-tab ${filter === key ? "active" : ""}`}
            aria-pressed={filter === key}
            onClick={() => changeFilter(key)}
          >
            {HOMEWORK_FILTER_LABELS[key]}<b>{counts[key]}</b>
          </button>
        ))}
      </div>
      <div className="peer-review-groups">
        {visibleGroups.map((group, groupIndex) => {
          const courseOpen = expandedCourseIds.has(group.courseId);
          const teachers = Array.from(new Set(group.items.map(item => item.teacherName).filter(Boolean))).join("、") || "未知教师";
          return <section className={`peer-review-group course-tone-${groupIndex % 4} ${courseOpen ? "open" : ""}`} key={group.courseId}>
          <h4><span className="course-review-identity"><i aria-hidden="true">{String(groupIndex + 1).padStart(2, "0")}</i><span><strong>{group.courseName}</strong><small>{teachers} · {group.items.length} 个作业</small></span></span><span className="course-review-actions"><button
            type="button"
            className="homework-archive"
            onClick={() => void setArchive("course", group.courseId, !group.courseArchived)}
          >{group.courseArchived ? "恢复课程" : "归档课程"}</button><button
            type="button"
            className={`course-review-toggle ${courseOpen ? "open" : ""}`}
            aria-label={courseOpen ? `收起${group.courseName}的作业` : `展开${group.courseName}的作业`}
            aria-expanded={courseOpen}
            onClick={() => toggleCourse(group)}
          ><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 6 4.5 4 4.5-4" /></svg></button></span></h4>
          {courseOpen && <div className="peer-review-course-items">{group.items.map(item => {
            const reviewable = isPeerReview(item) && !item.archived;
            return <div className={`peer-review-entry ${reviewable && reviewingItemId === item.id ? "open" : ""}`} key={item.id}>
              <div className="peer-review-row">
                <span><strong>{item.title}</strong><small>{item.teacherName} · {deadline(item.endTime)}</small></span>
                <span className="homework-badges">
                  {isOverdue(item) && <i className="homework-overdue">已截止</i>}
                  <b className={stateBadgeClass(item)}>{item.stateLabel}</b>
                </span>
                <span className="homework-item-actions">
                  <button type="button" className="homework-open" onClick={() => void openHomework(item)}>打开页面</button>
                  {!group.courseArchived && (item.archived
                    ? <button type="button" className="homework-archive-item" onClick={() => void setArchive("item", item.id, false)}>恢复</button>
                    : <button type="button" className="homework-archive-item" onClick={() => void setArchive("item", item.id, true)}>归档</button>)}
                </span>
                {reviewable
                  ? <button
                    type="button"
                    className={`peer-review-toggle ${reviewingItemId === item.id ? "open" : ""}`}
                    aria-label={reviewingItemId === item.id ? "收起评分和评语" : "展开评分和评语"}
                    aria-expanded={reviewingItemId === item.id}
                    onClick={() => setReviewingItemId(current => current === item.id ? "" : item.id)}
                  ><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 6 4.5 4 4.5-4" /></svg></button>
                  : <span className="peer-review-toggle-spacer" aria-hidden="true" />}
              </div>
              {reviewable && reviewingItemId === item.id && <HomeworkReviewPanel itemId={item.id} onSubmissionResult={value => {
                setSubmissionResult(value);
                void refreshScan(true);
              }} />}
            </div>;
          })}</div>}
        </section>})}
      </div>
      {scan.state === "completed" && !visibleGroups.length && <div className="homework-scan-message">{emptyMessage}</div>}
      {scan.failures.length > 0 && <details className="homework-scan-failures"><summary>{scan.failures.length} 门课程扫描失败</summary>{scan.failures.map(item => <p key={`${item.courseId}-${item.courseName}`}>{item.courseName}：{item.reason}</p>)}</details>}
    </article>
    <article className="card homework-card">
      <div className="homework-card-title"><h3>禁止粘贴类型作业输入</h3></div>
      <div className="homework-status-line">
        <span className={`homework-status-dot ${status.state}`} aria-hidden="true" />
        <div><strong>{status.state === "ready" ? "已识别输入框" : status.state === "checking" ? "识别中" : "未识别输入框"}</strong><small aria-live="polite">{status.message}</small></div>
      </div>
      <label className="homework-input-label" htmlFor="homework-text">要输入的内容</label>
      <textarea id="homework-text" value={text} maxLength={5000} disabled={busy} placeholder="在这里输入或粘贴作业内容…" onChange={event => { setText(event.target.value); setResult(null); }} onKeyDown={event => {
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void send(); }
      }} />
      <div className="homework-actions"><span>{text.length} / 5000 字</span><button type="button" className="primary" disabled={busy || status.state !== "ready" || !text.trim()} onClick={() => void send()}>{busy ? "输入中…" : "发送"}</button></div>
      {result && <p className={`homework-result ${result.good ? "good" : "error"}`} role="status">{result.message}</p>}
      <p className="homework-note">追加输入后，请在作业页面核对并提交。</p>
    </article>
  </section>;
}
