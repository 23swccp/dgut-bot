import { describe, expect, it } from "vitest";
import { formatBytes, downloadPercent, stateLabel, toastFor, toastAnimationKey, type UpdateStatus } from "./updateClient";
import { parseInline, parseSafeMarkdown, safeHref } from "./safeMarkdown";

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
  it("formats byte counts like the progress toast", () => {
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

describe("update toast derivation", () => {
  it("shows a progress toast with percent while downloading", () => {
    const view = toastFor(status({ state: "downloading", downloaded: 8_100_000, total: 19_400_000 }))!;
    expect(view.mode).toBe("progress");
    expect(view.title).toBe("正在下载 v0.3.0");
    expect(view.percent).toBe(42);
    expect(view.bytes).toBe("7.7 MB / 18.5 MB");
  });

  it("turns into ready state without progress replay", () => {
    const downloading = toastFor(status({ state: "downloading", downloaded: 1, total: 2 }))!;
    const again = toastFor(status({ state: "downloading", downloaded: 2, total: 2 }))!;
    expect(toastAnimationKey(downloading)).toBe(toastAnimationKey(again));
    const done = toastFor(status({ state: "ready_to_install" }))!;
    expect(done.mode).toBe("done");
    expect(done.title).toContain("更新已准备好");
    expect(toastAnimationKey(done)).not.toBe(toastAnimationKey(downloading));
  });

  it("switches to error toast on download failure", () => {
    const view = toastFor(status({ state: "download_failed" }))!;
    expect(view.mode).toBe("error");
    expect(view.title).toBe("⚠ v0.3.0 下载失败");
  });

  it("stays quiet when idle or available", () => {
    expect(toastFor(status({ state: "idle" }))).toBeNull();
    expect(toastFor(status({ state: "available" }))).toBeNull();
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
