export type ExportCourse = { id: number; name: string; teacherName: string };
export const exportSources = [
  { id: "resources", label: "资源区" }, { id: "courseware", label: "课件区" }, { id: "homework", label: "作业与互评" },
] as const;
export const exportKinds = [
  { id: "text", label: "正文" }, { id: "document", label: "文档 / PPT" }, { id: "image", label: "图片" },
  { id: "audio", label: "音频" }, { id: "video", label: "视频" }, { id: "subtitle", label: "字幕" }, { id: "other", label: "其他附件" },
] as const;
export type ExportOptions = { sources: string[]; kinds: string[]; chapters: Record<string, string[]> };
export type ExportCatalog = {
  courseId: string; courseName: string; chapters: { id: string; title: string; source: string }[];
  failures: { chapter?: string; reason: string }[]; error: string;
};
export function defaultExportOptions(): ExportOptions {
  return { sources: exportSources.map(item => item.id), kinds: exportKinds.map(item => item.id), chapters: {} };
}
export function hasExportSelection(courses: ExportCourse[], options: ExportOptions): boolean {
  return options.sources.length > 0 && options.kinds.length > 0 && courses.some(course => {
    const nodes = options.chapters[String(course.id)];
    return nodes === undefined || nodes.some(node => options.sources.includes(
      node.startsWith("resources:") ? "resources" : node.startsWith("homework:") ? "homework" : "courseware",
    ));
  });
}
export type Semester = { id: string; label: string; order: number };
export type SemesterCourse = ExportCourse & { semester: Semester; assumedSemester: boolean };
export type ExportResult = {
  state: string; courseId: string; courseName: string; chapterTotal: number; chapterDone: number;
  pageTotal: number; pageWritten: number; pageUnchanged: number; fileWritten: number;
  failures: { chapter?: string; file?: string; reason: string }[];
  folder: string; error: string;
};
export type ExportStatus = ExportResult & {
  results: ExportResult[]; root: string; courseTotal: number; courseDone: number;
};

function semester(year: number, season: string): Semester {
  const fall = season === "秋";
  return { id: `${year}-${fall ? "fall" : "spring"}`, label: `${year} 年${fall ? "秋" : "春"}季`, order: year * 2 + Number(fall) };
}

export function currentSemester(date = new Date()): Semester {
  const year = date.getFullYear();
  const month = date.getMonth() + 1;
  return semester(month === 1 ? year - 1 : year, month >= 8 || month === 1 ? "秋" : "春");
}

export function courseSemester(name: string): Semester | null {
  const academic = name.match(/(20\d{2})\s*[-—~～至/]\s*(20\d{2})\s*(?:学年)?\s*第?\s*([一二12])\s*学期/);
  if (academic && Number(academic[2]) === Number(academic[1]) + 1) {
    return academic[3] === "一" || academic[3] === "1"
      ? semester(Number(academic[1]), "秋") : semester(Number(academic[2]), "春");
  }
  const season = name.match(/(?:^|[^\d])(20\d{2}|\d{2})\s*年?\s*([春秋])(?:季)?/);
  return season ? semester(Number(season[1]) + (season[1].length === 2 ? 2000 : 0), season[2]) : null;
}

export function classifyCourses(courses: ExportCourse[], current: Semester): SemesterCourse[] {
  return [...new Map(courses.map(course => [course.id, course])).values()].map(course => {
    const known = courseSemester(course.name);
    return { ...course, semester: known || current, assumedSemester: !known };
  });
}

export function semesterOptions(courses: SemesterCourse[], current: Semester): Semester[] {
  return [current, ...[...new Map(courses.map(course => [course.semester.id, course.semester])).values()]
    .filter(item => item.id !== current.id).sort((a, b) => b.order - a.order)];
}

export function filterExportCourses(courses: SemesterCourse[], term: string, query: string): SemesterCourse[] {
  const needle = query.trim().toLocaleLowerCase();
  return courses.filter(course => (term === "all" || course.semester.id === term)
    && (!needle || `${course.name} ${course.teacherName} ${course.id}`.toLocaleLowerCase().includes(needle)));
}

export function selectVisible(selected: number[], visible: number[], checked: boolean): number[] {
  const next = new Set(selected);
  visible.forEach(id => checked ? next.add(id) : next.delete(id));
  return [...next];
}

export function exportStateLabel(state: string): string {
  return ({ idle: "等待选择", queued: "等待导出", running: "导出中", cancelling: "正在停止",
    completed: "已完成", cancelled: "已取消", error: "导出失败", auth_required: "需重新登录", skipped: "未执行" } as Record<string, string>)[state] || state;
}
