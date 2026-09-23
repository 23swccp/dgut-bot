import { useEffect, useState } from "react";

type ReviewRule = { key: string; title: string; maxScore: number; score: number | string };
type ReviewTask = {
  key: string; label: string; content: string; attachments: string[]; score: number;
  comment: string; wasScored: boolean; editable: boolean; rules: ReviewRule[];
};
type HomeworkReview = {
  itemId: string; title: string; fullScore: number; defaultScore: number; tasks: ReviewTask[];
};
type ReviewResult = { submitted: number; total: number; message: string };
type CommandResult = { ok: boolean; error?: string; homeworkReview?: HomeworkReview; reviewSubmit?: ReviewResult };

async function command(name: string, payload: Record<string, unknown>): Promise<CommandResult> {
  const response = await fetch("/api/command", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command: name, payload }),
  });
  return await response.json() as CommandResult;
}

export function HomeworkReviewPanel({ itemId, onSubmitted }: {
  itemId: string; onSubmitted: () => void;
}) {
  const [review, setReview] = useState<HomeworkReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<{ good: boolean; text: string } | null>(null);

  async function load() {
    setLoading(true);
    setMessage(null);
    try {
      const response = await command("get_homework_review", { itemId });
      if (!response.ok || !response.homeworkReview) throw new Error(response.error || "互评详情读取失败");
      setReview(response.homeworkReview);
    } catch (error) {
      setMessage({ good: false, text: error instanceof Error ? error.message : "互评详情读取失败" });
    } finally { setLoading(false); }
  }

  useEffect(() => { void load(); }, [itemId]);

  function updateTask(key: string, changes: Partial<ReviewTask>) {
    setReview(current => current ? {
      ...current, tasks: current.tasks.map(task => task.key === key ? { ...task, ...changes } : task),
    } : current);
    setMessage(null);
  }

  function updateRule(taskKey: string, ruleKey: string, value: string) {
    setReview(current => {
      if (!current) return current;
      return { ...current, tasks: current.tasks.map(task => {
        if (task.key !== taskKey) return task;
        const rules = task.rules.map(rule => rule.key === ruleKey ? { ...rule, score: value } : rule);
        const score = rules.reduce((sum, rule) => sum + (Number(rule.score) || 0), 0);
        return { ...task, rules, score };
      }) };
    });
    setMessage(null);
  }

  async function submitAll() {
    if (!review || submitting) return;
    const editable = review.tasks.filter(task => task.editable);
    if (!editable.length) { setMessage({ good: false, text: "当前没有可提交或修改的互评" }); return; }
    const newCount = editable.filter(task => !task.wasScored).length;
    const modifyCount = editable.length - newCount;
    const summary = [newCount ? `${newCount} 份新评分` : "", modifyCount ? `${modifyCount} 份修改评分` : ""].filter(Boolean).join("、");
    if (!window.confirm(`即将向优学院提交 ${summary}。提交后会真实影响互评结果，是否继续？`)) return;
    setSubmitting(true);
    setMessage(null);
    try {
      const drafts = editable.map(task => ({
        key: task.key, score: task.score, comment: task.comment,
        ruleScores: Object.fromEntries(task.rules.map(rule => [rule.key, rule.score])),
      }));
      const response = await command("submit_homework_reviews", { itemId, drafts });
      if (!response.ok) throw new Error(response.error || "互评提交失败");
      const success = response.reviewSubmit?.message || `已提交 ${editable.length} 份互评`;
      await load();
      setMessage({ good: true, text: success });
      onSubmitted();
    } catch (error) {
      setMessage({ good: false, text: error instanceof Error ? error.message : "互评提交失败" });
    } finally { setSubmitting(false); }
  }

  return <section className="review-editor" aria-label="程序内互评">
    <header><div><strong>{review?.title || "互评评分"}</strong><small>{loading ? "正在读取作业内容…" : `共 ${review?.tasks.length || 0} 份 · 未评分默认 ${review?.defaultScore ?? 100} 分`}</small></div></header>
    {loading && <div className="review-loading">正在通过学校页面读取互评详情…</div>}
    {!loading && review && review.tasks.length === 0 && <div className="review-loading">没有读取到可评分的互评作业。</div>}
    {!loading && review?.tasks.map(task => <article className={`review-task ${task.editable ? "" : "locked"}`} key={task.key}>
      <div className="review-task-head"><strong>{task.label}</strong>{!task.wasScored && <span className="default">未评分 · 默认 {review.defaultScore} 分</span>}</div>
      <pre>{task.content || "该作业没有可显示的文字正文"}</pre>
      {task.attachments.length > 0 && <p className="review-attachments">附件：{task.attachments.join("、")}</p>}
      {task.rules.length > 0 && <div className="review-rules">{task.rules.map(rule => <label key={rule.key}><span>{rule.title}<small>满分 {rule.maxScore}</small></span><input type="number" min="0" max={rule.maxScore} step="1" disabled={!task.editable || submitting} value={rule.score} onChange={event => updateRule(task.key, rule.key, event.target.value)} /></label>)}</div>}
      <div className="review-fields">
        <label><span>评分</span><input type="number" min="0" max={review.fullScore} step="0.1" disabled={!task.editable || submitting || task.rules.length > 0} value={task.score} onChange={event => updateTask(task.key, { score: Number(event.target.value) })} /><small>/ {review.fullScore}</small></label>
        <label className="review-comment"><span>评语（可选）</span><textarea maxLength={2000} disabled={!task.editable || submitting} value={task.comment} onChange={event => updateTask(task.key, { comment: event.target.value })} placeholder="可填写评语，最多 2000 字" /></label>
      </div>
      {!task.editable && <p className="review-locked-note">该条互评当前不允许修改。</p>}
    </article>)}
    {message && <p className={`review-message ${message.good ? "good" : "error"}`} role="status">{message.text}</p>}
    {!loading && review && <footer><span>提交前会再次确认；若中途失败，会显示已成功提交的份数。</span><button type="button" className="primary" disabled={submitting || !review.tasks.some(task => task.editable)} onClick={() => void submitAll()}>{submitting ? "提交中…" : "一键提交"}</button></footer>}
  </section>;
}
