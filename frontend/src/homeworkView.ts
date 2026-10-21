export type HomeworkItem = {
  id: string;
  homeworkId: string;
  courseId: string;
  courseName: string;
  teacherName: string;
  title: string;
  state: number | null;
  stateLabel: string;
  pending: boolean;
  needsAction: boolean;
  archived: boolean;
  endTime: string;
  url: string;
};

export type HomeworkGroup = {
  courseId: string;
  courseName: string;
  courseArchived: boolean;
  items: HomeworkItem[];
};

export type HomeworkFilter = "pending" | "all" | "graded" | "archived";

export const HOMEWORK_FILTER_LABELS: Record<HomeworkFilter, string> = {
  pending: "待处理",
  all: "全部",
  graded: "已批阅",
  archived: "已归档",
};

export function isPeerReview(item: HomeworkItem): boolean {
  return item.state === 1 || item.state === 2;
}

export function isOverdue(item: HomeworkItem, now: number = Date.now()): boolean {
  if (item.archived || !item.pending || !item.endTime) return false;
  const deadline = Date.parse(item.endTime);
  return Number.isFinite(deadline) && deadline < now;
}

export function filterHomeworkGroups(groups: HomeworkGroup[], filter: HomeworkFilter): HomeworkGroup[] {
  return groups
    .map(group => ({
      ...group,
      items: group.items.filter(item => {
        if (filter === "archived") return item.archived;
        if (item.archived) return false;
        if (filter === "graded") return item.state === 4;
        if (filter === "pending") return item.pending;
        return true;
      }),
    }))
    .filter(group => group.items.length > 0);
}

export function homeworkCounts(groups: HomeworkGroup[]): {
  pending: number; all: number; graded: number; archived: number;
} {
  let pending = 0;
  let all = 0;
  let graded = 0;
  let archived = 0;
  for (const group of groups) {
    for (const item of group.items) {
      if (item.archived) {
        archived += 1;
        continue;
      }
      all += 1;
      if (item.pending) pending += 1;
      if (item.state === 4) graded += 1;
    }
  }
  return { pending, all, graded, archived };
}

export function defaultExpandedCourseIds(groups: HomeworkGroup[]): Set<string> {
  return new Set(
    groups
      .filter(group => group.items.some(item => !item.archived && item.pending))
      .map(group => group.courseId),
  );
}
