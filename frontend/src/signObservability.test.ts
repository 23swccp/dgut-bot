import { describe, expect, it } from "vitest";
import { addSignEvent, signEventView, type SignEvent } from "./signObservability";

const event = (message: string, id = message): SignEvent => ({
  id,
  message,
  time: "2026-09-06T19:32:57+08:00",
  level: "info",
});

describe("sign observability", () => {
  it("keeps polling ticks out of the activity stream", () => {
    expect(addSignEvent([], event("第 2 轮：正在检查《系统工程》…"))).toEqual([]);
  });

  it("deduplicates unchanged course outcomes", () => {
    const first = event("[系统工程] 本轮完成：今天没有课堂，无需签到。", "1");
    const second = event(first.message, "2");
    expect(addSignEvent(addSignEvent([], first), second)).toEqual([first]);
  });

  it("turns raw results into a concise event", () => {
    expect(signEventView(event("[系统工程] 本轮完成：今天没有课堂，无需签到。"))).toMatchObject({
      title: "今日暂无课堂",
      detail: "系统工程 · 无需签到，监测将继续",
      tone: "muted",
    });
  });

  it("shows activity monitoring after a successful sign instead of a stopped sign", () => {
    expect(signEventView(event("[测试课] 签到成功，继续监测选人活动。"))).toMatchObject({
      title: "签到完成 · 监测课堂活动", tone: "info",
    });
  });

  it("keeps temporary withdrawal visible as a warning", () => {
    expect(signEventView(event("[测试课] 发现选人活动，已临时退出课程；结束后自动恢复原班级。"))).toMatchObject({
      title: "选人活动 · 已临时退课", tone: "warning",
    });
  });

  it("shows verified restoration as success", () => {
    expect(signEventView(event("[测试课] 已确认恢复原班级。"))).toMatchObject({
      title: "已恢复原班级", tone: "success",
    });
  });
});
