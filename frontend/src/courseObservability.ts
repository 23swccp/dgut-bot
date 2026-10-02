export type EventLevel = "info" | "success" | "warning" | "error";

export type CourseEvent = {
  seq: number;
  time: string;
  sessionId: string;
  code: string;
  level: EventLevel;
  category: string;
  message: string;
  page: { id?: string; name?: string; index?: number; total?: number };
  data: Record<string, unknown>;
};

export type CourseStatus = {
  sessionId?: string;
  running?: boolean;
  completed?: boolean;
  paused?: boolean;
  connected?: boolean;
  readOk?: boolean;
  readFailures?: number;
  controllerState?: string;
  courseName?: string;
  page?: { id?: string; name?: string; index?: number; total?: number; completed?: boolean };
  pageCompleted?: boolean;
  completionSource?: string;
  currentTask?: string;
  pagePlan?: Array<{ kind: string; state: string; count?: number; types?: Record<string, number> }>;
  video?: { currentTime?: number; duration?: number; rate?: number };
  playbackRate?: number;
  lastProgressTime?: string;
  retryCount?: number;
  maxRetries?: number;
  stalled?: boolean;
  stallReason?: string;
  resourceError?: { code: string; message: string; current: number; total: number } | null;
};

export const MAX_COURSE_EVENTS = 500;

export function mergeEvents(current: CourseEvent[], incoming: CourseEvent[], limit = MAX_COURSE_EVENTS) {
  const bySeq = new Map(current.map(event => [event.seq, event]));
  for (const event of incoming) if (!bySeq.has(event.seq)) bySeq.set(event.seq, event);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq).slice(-limit);
}

export function visibleCourseEvents(events: CourseEvent[], detailed: boolean) {
  return events.filter(event => detailed || event.category !== "debug");
}

export function formatClock(value?: string) {
  if (!value) return "--:--:--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "--:--:--" : date.toLocaleTimeString("zh-CN", { hour12: false });
}

export function formatDuration(value?: number) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const tail = seconds % 60;
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(tail).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(tail).padStart(2, "0")}`;
}

export function runStateLabel(status: CourseStatus) {
  if (status.resourceError) return "课件异常";
  if (status.completed || status.controllerState === "COMPLETED") return "已完成";
  if (status.paused) return "已暂停";
  if (status.stalled) return "恢复中";
  return status.running ? "运行中" : "已停止";
}

export function connectionLabel(status: CourseStatus) {
  return status.connected ? "CDP 已连接" : "CDP 未连接";
}

export function formatPagePlan(plan?: CourseStatus["pagePlan"]) {
  if (!plan?.length) return "确认中";
  const labels: Record<string, string> = { record: "平台记录", video: "视频", document: "文档", quiz: "测验", network: "网络提示", dialog: "页面提示" };
  return plan.map(task => {
    const label = labels[task.kind] || task.kind;
    if (task.state === "error") return `${label}（资源异常）`;
    if (task.state === "skipped") return `${label}（已跳过）`;
    if (task.kind === "quiz" && task.types) {
      const detail = Object.entries(task.types).map(([name, count]) => `${name} ${count}`).join("、");
      return detail ? `${label}（${detail}）` : `${label} ${task.count || 0}`;
    }
    return task.count && task.kind !== "record" ? `${label} ${task.count}` : label;
  }).join(" → ");
}
