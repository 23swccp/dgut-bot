import { describe, expect, it } from "vitest";
import { defaultExpandedCourseIds, filterHomeworkGroups, homeworkCounts, isOverdue, isPeerReview, type HomeworkGroup } from "./homeworkView";

const groups: HomeworkGroup[] = [{ courseId: "1", courseName: "系统工程", courseArchived: false, items: [
  {
    id: "a", homeworkId: "10", courseId: "1", courseName: "系统工程", teacherName: "张老师",
    title: "未提交作业", state: 0, stateLabel: "未提交", pending: true, needsAction: false, archived: false,
    endTime: "2026-01-01T00:00:00+08:00", url: "https://lms.dgut.edu.cn/courseweb/#/course/homework?courseId=1",
  },
  {
    id: "b", homeworkId: "11", courseId: "1", courseName: "系统工程", teacherName: "张老师",
    title: "等待互评", state: 1, stateLabel: "未互评", pending: true, needsAction: true, archived: false,
    endTime: "", url: "https://lms.dgut.edu.cn/courseweb/#/course/homework?courseId=1",
  },
  {
    id: "c", homeworkId: "12", courseId: "1", courseName: "系统工程", teacherName: "张老师",
    title: "正在互评", state: 2, stateLabel: "互评中", pending: false, needsAction: false, archived: false,
    endTime: "2026-01-02T00:00:00+08:00", url: "https://lms.dgut.edu.cn/courseweb/#/course/homework?courseId=1",
  },
  {
    id: "d", homeworkId: "13", courseId: "1", courseName: "系统工程", teacherName: "张老师",
    title: "已批阅作业", state: 4, stateLabel: "已批阅", pending: false, needsAction: false, archived: false,
    endTime: "2026-01-03T00:00:00+08:00", url: "https://lms.dgut.edu.cn/courseweb/#/course/homework?courseId=1",
  },
  {
    id: "e", homeworkId: "14", courseId: "1", courseName: "系统工程", teacherName: "张老师",
    title: "已归档作业", state: 0, stateLabel: "未提交", pending: true, needsAction: false, archived: true,
    endTime: "2026-01-04T00:00:00+08:00", url: "https://lms.dgut.edu.cn/courseweb/#/course/homework?courseId=1",
  },
]}];

describe("homework view", () => {
  it("filters pending, all, graded and archived without dropping course groups early", () => {
    expect(filterHomeworkGroups(groups, "pending")[0].items.map(item => item.id)).toEqual(["a", "b"]);
    expect(filterHomeworkGroups(groups, "all")[0].items.map(item => item.id)).toEqual(["a", "b", "c", "d"]);
    expect(filterHomeworkGroups(groups, "graded")[0].items.map(item => item.id)).toEqual(["d"]);
    expect(filterHomeworkGroups(groups, "archived")[0].items.map(item => item.id)).toEqual(["e"]);
  });

  it("removes courses that have no items under the active filter", () => {
    const onlyGraded: HomeworkGroup[] = [{ ...groups[0], items: [groups[0].items[3]] }];
    expect(filterHomeworkGroups(onlyGraded, "pending")).toEqual([]);
    expect(filterHomeworkGroups(onlyGraded, "archived")).toEqual([]);
  });

  it("counts active items separately from archived items", () => {
    expect(homeworkCounts(groups)).toEqual({ pending: 2, all: 4, graded: 1, archived: 1 });
  });

  it("marks only active pending items with a passed deadline as overdue", () => {
    const now = Date.parse("2026-02-01T00:00:00+08:00");
    expect(isOverdue(groups[0].items[0], now)).toBe(true);
    expect(isOverdue(groups[0].items[1], now)).toBe(false);
    expect(isOverdue(groups[0].items[2], now)).toBe(false);
    expect(isOverdue(groups[0].items[4], now)).toBe(false);
    expect(isOverdue(groups[0].items[0], Date.parse("2025-12-01T00:00:00+08:00"))).toBe(false);
  });

  it("keeps the review entry for peer-review states only", () => {
    expect(groups[0].items.map(isPeerReview)).toEqual([false, true, true, false, false]);
  });

  it("expands only courses that still have active pending work by default", () => {
    const mixed: HomeworkGroup[] = [
      { ...groups[0], items: [groups[0].items[3]] },
      { courseId: "2", courseName: "另一门课程", courseArchived: false, items: [groups[0].items[0]] },
      { courseId: "3", courseName: "只有归档", courseArchived: false, items: [groups[0].items[4]] },
    ];
    expect([...defaultExpandedCourseIds(mixed)]).toEqual(["2"]);
  });
});
