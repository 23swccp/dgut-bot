import { formatDuration } from "./courseObservability";

export type LessonItem = {
  id: string; courseId: string; ocId?: string; classId: string; chapterId?: string; courseName: string; teacherName: string;
  chapterPath: string[]; title: string; type: string; completionStatus: string;
  completionPercent: number | null; nodeId: string; pageId: string; url: string;
  canAutoOpen: boolean; unavailableReason: string;
  studySeconds?: number | null; score?: number | null; startDate?: string | null; endDate?: string | null;
  textbookSummary?: { cover: string; progress: number | null; studySeconds: number | null; score: number | null } | null;
};

export type LessonGroup = { courseId: string; courseName: string; items: LessonItem[] };
export type CourseScanStatus = {
  state: "idle" | "scanning" | "completed" | "cancelled" | "error" | "auth_required";
  loggedIn: boolean; scannedCourses: number; totalCourses: number; unfinishedCount: number;
  groups: LessonGroup[]; failures: { courseId: string; courseName: string; reason: string }[];
  cachedAt: string; fromCache: boolean; stopping?: boolean; error: string; openPhase: string; openError: string; selectedId: string;
};

export function lessonProgress(item: LessonItem): number {
  return Math.round(item.completionPercent ?? (item.completionStatus === "completed" ? 100 : 0));
}

export function lessonStudyTime(seconds?: number | null): string {
  if (seconds == null) return "--";
  const duration = formatDuration(seconds);
  return duration.length === 5 ? `00:${duration}` : duration;
}

export function lessonStudyAction(item: LessonItem): string {
  return item.completionStatus !== "not_started" || (item.studySeconds ?? 0) > 0 || lessonProgress(item) > 0
    ? "继续学习" : "开始学习";
}

export function openPhaseLabel(phase: string): string {
  return ({
    opening: "正在打开课件", waiting_page: "等待页面加载", validating: "正在校验课件",
    connected: "已连接课件", starting: "正在启动", started: "刷课已启动",
    failed: "打开失败", timeout: "页面加载超时", auth_required: "登录已失效",
  } as Record<string, string>)[phase] || "";
}
