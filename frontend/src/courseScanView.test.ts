import { describe, expect, it } from "vitest";
import { lessonProgress, lessonStudyAction, lessonStudyTime, openPhaseLabel, type LessonGroup } from "./courseScanView";

const groups: LessonGroup[] = [{ courseId: "1", courseName: "系统工程", items: [{
  id: "x", courseId: "1", classId: "", courseName: "系统工程", teacherName: "教师",
  chapterPath: ["第一章", "概论"], title: "课程简介", type: "视频", completionStatus: "in_progress",
  completionPercent: 35, nodeId: "n", pageId: "p", url: "https://ua.dgut.edu.cn/learnCourse?id=p",
  canAutoOpen: true, unavailableReason: "",
}]}];

describe("course scan view", () => {
  it("formats progress and every auto-open stage", () => {
    expect(lessonProgress(groups[0].items[0])).toBe(35);
    expect(openPhaseLabel("waiting_page")).toBe("等待页面加载");
    expect(openPhaseLabel("started")).toBe("刷课已启动");
    expect(openPhaseLabel("failed")).toBe("打开失败");
    expect(openPhaseLabel("timeout")).toBe("页面加载超时");
  });

  it("formats chapter time and continues chapters with study time even at 0 percent", () => {
    const item = { ...groups[0].items[0], completionStatus: "not_started", completionPercent: 0 };
    expect(lessonStudyTime(2904)).toBe("00:48:24");
    expect(lessonStudyTime(0)).toBe("00:00:00");
    expect(lessonStudyTime(undefined)).toBe("--");
    expect(lessonStudyAction({ ...item, studySeconds: 611 })).toBe("继续学习");
    expect(lessonStudyAction({ ...item, studySeconds: 0 })).toBe("开始学习");
  });
});
