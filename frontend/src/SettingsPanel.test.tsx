import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { SettingsPanel } from "./SettingsPanel";
import { SettingsAutosave } from "./settingsAutosave";

const call = async () => ({ ok: true });
describe("settings panel", () => {
  it("has no manual save button and defaults auto-answer on", () => {
    const html = renderToStaticMarkup(<SettingsPanel config={{}} call={call} writer={new SettingsAutosave(call)} onSaved={() => {}} monitoring={false} />);
    expect(html).not.toContain("保存全部设置");
    expect(html).not.toContain("保存完整运行日志");
    expect(html).not.toContain("日志路径");
    expect(html).not.toContain("打开日志");
    expect(html).toContain('aria-checked="true" aria-label="自动答题"');
  });
  it("restores a saved disabled choice and preserves the monitoring restriction", () => {
    const html = renderToStaticMarkup(<SettingsPanel config={{ course_quiz_auto_answer: false }} call={call} writer={new SettingsAutosave(call)} onSaved={() => {}} monitoring={true} />);
    expect(html).toContain('aria-checked="false" aria-label="自动答题"');
    expect(html).not.toContain('aria-label="选择题"');
    expect(html).toContain('aria-label="签到后监测选人并自动退课／加回" disabled=""');
  });
});
