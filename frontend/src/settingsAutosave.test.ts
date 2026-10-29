import { afterEach, describe, expect, it, vi } from "vitest";
import { quizToggle, SettingsAutosave } from "./settingsAutosave";

afterEach(() => vi.useRealTimers());

describe("settings autosave", () => {
  it("serializes rapid edits and sends only each changed field", async () => {
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    const seen: unknown[] = [];
    const writer = new SettingsAutosave(async (_command, patch) => {
      seen.push(patch);
      if (seen.length === 1) await gate;
      return { ok: true };
    });
    const first = writer.save("quiz", "update_settings", { course_quiz_auto_answer: false });
    const second = writer.save("rate", "update_settings", { course_playback_rate: 6 });
    await Promise.resolve();
    expect(seen).toEqual([{ course_quiz_auto_answer: false }]);
    release();
    await Promise.all([first, second]);
    expect(seen).toEqual([{ course_quiz_auto_answer: false }, { course_playback_rate: 6 }]);
  });

  it("debounces text per field and flushes the latest text before leaving", async () => {
    vi.useFakeTimers();
    const call = vi.fn(async () => ({ ok: true }));
    const writer = new SettingsAutosave(call);
    writer.defer("path", () => { void writer.save("path", "update_settings", { browser_path: "old" }); });
    writer.defer("path", () => { void writer.save("path", "update_settings", { browser_path: "new" }); });
    writer.defer("rate", () => { void writer.save("rate", "update_settings", { course_playback_rate: 4 }); });
    await vi.advanceTimersByTimeAsync(100);
    expect(call).not.toHaveBeenCalled();
    expect(await writer.flush()).toBe(true);
    expect(call.mock.calls).toEqual([["update_settings", { browser_path: "new" }], ["update_settings", { course_playback_rate: 4 }]]);
    await vi.runAllTimersAsync();
    expect(call).toHaveBeenCalledTimes(2);
  });

  it("saves when typing stops and cancels a replaced browser draft", async () => {
    vi.useFakeTimers();
    const call = vi.fn(async () => ({ ok: true }));
    const writer = new SettingsAutosave(call);
    writer.defer("browser", () => { void writer.save("browser", "update_settings", { browser_path: "bad" }); });
    writer.cancel("browser");
    writer.defer("path", () => { void writer.save("path", "update_settings", { browser_path: "msedge.exe" }); });
    await vi.advanceTimersByTimeAsync(500);
    expect(call.mock.calls).toEqual([["update_settings", { browser_path: "msedge.exe" }]]);
  });

  it("keeps original errors, continues the queue and accepts a successful retry", async () => {
    const call = vi.fn().mockRejectedValueOnce(new Error("disk full")).mockResolvedValue({ ok: true });
    const writer = new SettingsAutosave(call);
    expect(await writer.save("quiz", "update_settings", {})).toEqual({ ok: false, error: "Error: disk full" });
    await writer.save("rate", "update_settings", {});
    expect(await writer.flush()).toBe(false);
    await writer.save("quiz", "update_settings", {});
    expect(await writer.flush()).toBe(true);
  });
});

describe("quiz setting dependencies", () => {
  it("defaults on, remembers disabling and enables question types when needed", () => {
    expect(quizToggle({}, "course_quiz_auto_answer")).toEqual({ course_quiz_auto_answer: false });
    expect(quizToggle({ course_quiz_auto_answer: false }, "course_quiz_auto_answer")).toEqual({ course_quiz_auto_answer: true });
    expect(quizToggle({ course_quiz_auto_answer: false, course_quiz_choice_enabled: false, course_quiz_judgment_enabled: false, course_quiz_blank_enabled: false }, "course_quiz_auto_answer")).toEqual({ course_quiz_auto_answer: true, course_quiz_choice_enabled: true, course_quiz_judgment_enabled: true, course_quiz_blank_enabled: true });
  });
  it("turns off the main switch together with the last enabled question type", () => {
    expect(quizToggle({ course_quiz_choice_enabled: true, course_quiz_judgment_enabled: false, course_quiz_blank_enabled: false }, "course_quiz_choice_enabled")).toEqual({ course_quiz_choice_enabled: false, course_quiz_auto_answer: false });
  });
});
