import { describe, expect, it } from "vitest";
import { connectionLabel, formatPagePlan, runStateLabel } from "./courseObservability";

describe("course observability", () => {
  it("restores running and recovery labels from backend status", () => {
    expect(runStateLabel({ running: true, resourceError: { code: "parse-failed", message: "解析失败", current: 1, total: 111 } })).toBe("课件异常");
    expect(runStateLabel({ running: true })).toBe("运行中");
    expect(runStateLabel({ running: true, stalled: true })).toBe("恢复中");
    expect(runStateLabel({ running: true, paused: true })).toBe("已暂停");
    expect(runStateLabel({ running: false, completed: true })).toBe("已完成");
    expect(connectionLabel({ connected: false })).toBe("CDP 未连接");
    expect(connectionLabel({ connected: true })).toBe("CDP 已连接");
  });

  it("renders a composed page plan without exposing internal selectors", () => {
    expect(formatPagePlan([{ kind: "document", state: "error", count: 1 }])).toBe("文档（资源异常）");
    expect(formatPagePlan([{ kind: "quiz", state: "skipped", count: 18, types: { "单选题": 18 } }])).toBe("测验（已跳过）");
    expect(formatPagePlan([{ kind: "dialog", state: "pending", count: 1 }])).toBe("页面提示 1");
    expect(formatPagePlan([
      { kind: "video", state: "completed", count: 1 },
      { kind: "document", state: "pending", count: 1 },
      { kind: "quiz", state: "pending", count: 2, types: { "单选题": 1, "填空题": 1 } },
    ])).toBe("视频 1 → 文档 1 → 测验（单选题 1、填空题 1）");
  });
});
