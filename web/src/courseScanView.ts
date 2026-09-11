export type LessonItem = {
  id: string; courseId: string; ocId?: string; classId: string; chapterId?: string; courseName: string; teacherName: string;
  chapterPath: string[]; title: string; type: string; completionStatus: string;
  completionPercent: number | null; nodeId: string; pageId: string; url: string;
  canAutoOpen: boolean; unavailableReason: string;
};

export type LessonGroup = { courseId: string; courseName: string; items: LessonItem[] };
export type CourseScanStatus = {
  state: "idle" | "scanning" | "completed" | "cancelled" | "error" | "auth_required";
  loggedIn: boolean; scannedCourses: number; totalCourses: number; unfinishedCount: number;
  groups: LessonGroup[]; failures: { courseId: string; courseName: string; reason: string }[];
  cachedAt: string; fromCache: boolean; error: string; openPhase: string; openError: string; selectedId: string;
};

export function filterLessonGroups(groups: LessonGroup[], query: string): LessonGroup[] {
  const keyword = query.trim().toLocaleLowerCase();
  if (!keyword) return groups;
  return groups.map(group => ({ ...group, items: group.items.filter(item =>
    `${item.courseName} ${item.chapterPath.join(" ")} ${item.title}`.toLocaleLowerCase().includes(keyword),
  ) })).filter(group => group.items.length > 0);
}

export function lessonProgress(item: LessonItem): string {
  if (typeof item.completionPercent === "number") return `${Math.round(item.completionPercent)}%`;
  return item.completionStatus === "in_progress" ? "进行中" : "未开始";
}

export function openPhaseLabel(phase: string): string {
  return ({
    opening: "正在打开课件", waiting_page: "等待页面加载", validating: "正在校验课件",
    connected: "已连接课件", starting: "正在启动", started: "刷课已启动",
    failed: "打开失败", timeout: "页面加载超时", auth_required: "登录已失效",
  } as Record<string, string>)[phase] || "";
}
