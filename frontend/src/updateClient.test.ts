import { describe, expect, it } from "vitest";
import { formatBytes, downloadPercent, stateLabel, type UpdateStatus } from "./updateClient";
import { parseInline, parseSafeMarkdown, safeHref } from "./safeMarkdown";
import { updateNoticeKey, UpdateNoticeMemory } from "./updatePrompts";

const status = (patch: Partial<UpdateStatus>): UpdateStatus => ({
  currentVersion: "0.2.0",
  state: "idle",
  latestVersion: "0.3.0",
  publishedAt: "2026-08-29T10:00:00+08:00",
  changelog: "",
  downloaded: 0,
  total: 0,
  percent: 0,
  error: "",
  messages: [],
  unreadCount: 0,
  downloading: false,
  handoff: false,
  readyForExit: false,
  canInstall: false,
  canRetryDownload: false,
  pendingFailureDialog: null,
  ...patch,
});

describe("update formatting helpers", () => {
  it("formats byte counts for the update settings", () => {
    expect(formatBytes(0)).toBe("0.0 B");
    expect(formatBytes(8_500_000)).toBe("8.1 MB");
    expect(formatBytes(19_400_000)).toBe("18.5 MB");
    expect(formatBytes(1024)).toBe("1.0 KB");
  });

  it("computes download percent and maps state labels", () => {
    expect(downloadPercent({ downloaded: 42, total: 100, state: "downloading" })).toBe(42);
    expect(downloadPercent({ downloaded: 0, total: 0, state: "downloading" })).toBe(0);
    expect(downloadPercent({ downloaded: 0, total: 0, state: "ready_to_install" })).toBe(100);
    expect(stateLabel("ready_to_install")).toBe("更新已准备好");
    expect(stateLabel("download_failed")).toBe("下载失败");
    expect(stateLabel("handoff")).toBe("正在移交给更新器……");
  });
});

describe("update dialog notices", () => {
  it("separates discovery, progress, readiness and failure without executing actions", () => {
    const available = updateNoticeKey(status({ state: "available" }));
    const ready = updateNoticeKey(status({ state: "ready_to_install", canInstall: true }));
    expect(available).toContain("available:");
    expect(ready).toContain("ready:");
    expect(updateNoticeKey(status({ state: "downloading" }))).toBe(updateNoticeKey(status({ state: "verifying" })));
    expect(updateNoticeKey(status({ state: "download_failed", error: "连接中断" }))).toContain("failed:");
    for (const state of ["idle", "checking", "handoff"] as const) expect(updateNoticeKey(status({ state }))).toBeNull();
    expect(updateNoticeKey(status({ state: "available", latestVersion: "" }))).toBeNull();
  });

  it("remembers a displayed stage across polling and page refresh", () => {
    const values = new Map<string, string>();
    const storage = { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value); } };
    const key = updateNoticeKey(status({ state: "available" }))!;
    const memory = new UpdateNoticeMemory(storage);
    expect(memory.claim(key)).toBe(true);
    expect(memory.claim(key)).toBe(false);
    expect(new UpdateNoticeMemory(storage).claim(key)).toBe(false);
    expect(memory.claim(updateNoticeKey(status({ state: "ready_to_install", canInstall: true }))!)).toBe(true);
    expect(memory.claim(updateNoticeKey(status({ state: "available", latestVersion: "0.4.0" }))!)).toBe(true);
  });

  it("deduplicates notifications even when storage is unavailable", () => {
    const memory = new UpdateNoticeMemory({ getItem: () => { throw Error("blocked"); }, setItem: () => { throw Error("blocked"); } });
    expect(memory.claim("available:0.2:0.3")).toBe(true);
    expect(memory.claim("available:0.2:0.3")).toBe(false);
  });
});

describe("safe markdown", () => {
  it("rejects dangerous link protocols", () => {
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,x")).toBeNull();
    expect(safeHref(" https://example.com/a ")).toBe("https://example.com/a");
  });

  it("renders links, bold and code but never raw HTML", () => {
    const blocks = parseSafeMarkdown("## 标题\n\n**加粗** 与 `代码` 和 [链接](https://example.com)\n\n<img src=x onerror=alert(1)>");
    expect(blocks[0].type).toBe("h2");
    // 原始 HTML 一律按纯文本保留，交给 React 转义渲染，不生成任何元素。
    const imgParagraph = blocks.find(block => JSON.stringify(block).includes("<img"));
    expect(imgParagraph).toBeDefined();
    if (!imgParagraph || !("inlines" in imgParagraph)) throw new Error("expected paragraph block");
    expect(imgParagraph.inlines.every(inline => inline.type === "text")).toBe(true);
  });

  it("parses lists and code blocks", () => {
    const blocks = parseSafeMarkdown("- 第一项\n- 第二项\n\n```\ncode\nblock\n```");
    expect(blocks.filter(block => block.type === "li")).toHaveLength(2);
    expect(blocks.find(block => block.type === "code")).toMatchObject({ type: "code", text: "code\nblock" });
  });

  it("keeps javascript links as plain text", () => {
    const inlines = parseInline("点击 [这里](javascript:alert(1)) 继续");
    expect(inlines.some(item => item.type === "link")).toBe(false);
    expect(inlines.some(item => item.type === "text" && item.text === "这里")).toBe(true);
  });
});
