import { describe, expect, it } from "vitest";
import { classifyCourses, courseSemester, currentSemester, defaultExportOptions, filterExportCourses, hasExportSelection, selectVisible, semesterOptions } from "./coursewareView";

describe("courseware course selection", () => {
  it("blocks empty filters and excludes deselected sources from chapter selection", () => {
    const courses = [{ id: 1, name: "course", teacherName: "teacher" }];
    const options = defaultExportOptions();
    expect(hasExportSelection(courses, options)).toBe(true);
    expect(hasExportSelection([], options)).toBe(false);
    expect(hasExportSelection(courses, { ...options, kinds: [] })).toBe(false);
    expect(hasExportSelection(courses, { ...options, sources: [] })).toBe(false);
    expect(hasExportSelection(courses, { ...options, chapters: { "1": [] } })).toBe(false);
    expect(hasExportSelection(courses, { ...options, sources: ["courseware"], chapters: { "1": ["resources:1"] } })).toBe(false);
    expect(hasExportSelection(courses, { ...options, sources: ["courseware"], chapters: { "1": ["c2"] } })).toBe(true);
  });
  it("handles spring, fall, January and academic-year titles", () => {
    expect(currentSemester(new Date(2026, 9, 21)).id).toBe("2026-fall");
    expect(currentSemester(new Date(2027, 0, 10)).id).toBe("2026-fall");
    expect(currentSemester(new Date(2027, 1, 10)).id).toBe("2027-spring");
    expect(courseSemester("【2026年秋】工程材料2024级")?.id).toBe("2026-fall");
    expect(courseSemester("25春国际素养英语A2")?.id).toBe("2025-spring");
    expect(courseSemester("2026-2027学年第一学期 课程")?.id).toBe("2026-fall");
    expect(courseSemester("2026-2027第二学期 课程")?.id).toBe("2027-spring");
    expect(courseSemester("管理学原理（2024工业工程）")).toBeNull();
    expect(courseSemester("可靠性工程2026")).toBeNull();
  });

  it("puts unknown semesters in the current term without selecting them or merging equal names", () => {
    const current = currentSemester(new Date(2026, 9, 21));
    const courses = classifyCourses([
      { id: 1, name: "同名课程", teacherName: "甲" }, { id: 2, name: "同名课程", teacherName: "甲" },
      { id: 3, name: "【2025秋】历史课程", teacherName: "乙" },
      { id: 4, name: "【2026秋】本期课程", teacherName: "丙" },
    ], current);
    expect(courses[0].assumedSemester).toBe(true);
    expect(filterExportCourses(courses, current.id, "").map(item => item.id)).toEqual([1, 2, 4]);
    expect(filterExportCourses(courses, "2025-fall", "乙").map(item => item.id)).toEqual([3]);
    expect(filterExportCourses(courses, "all", "2").map(item => item.id)).toEqual([2, 3, 4]);
    expect(semesterOptions(courses, current).map(item => item.id)).toEqual(["2026-fall", "2025-fall"]);
  });

  it("selects and clears only the visible list while preserving hidden selections", () => {
    let selected: number[] = [];
    expect(selected).toEqual([]);
    selected = selectVisible(selected, [1, 2], true);
    selected = selectVisible(selected, [3], true);
    selected = selectVisible(selected, [2, 3], false);
    expect(selected).toEqual([1]);
    expect(selectVisible(selected, [], true)).toEqual([1]);
  });
});
