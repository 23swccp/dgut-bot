import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { LogPanel } from "./LogPanel";

const noop = async () => ({ ok: true });

describe("log page", () => {
  it("shows the complete log with only follow, refresh and export controls", () => {
    const html = renderToStaticMarkup(<LogPanel call={noop} />);
    expect(html).toContain("运行日志");
    expect(html).toContain("跟随中");
    expect(html).toContain("刷新");
    expect(html).toContain("导出日志");
  });

  it("has no filter controls and no subtitle", () => {
    const html = renderToStaticMarkup(<LogPanel call={noop} />);
    for (const gone of ["全部级别", "搜索内容或事件码", "log-chip", "签到、刷课"]) {
      expect(html).not.toContain(gone);
    }
  });

  it("shows an empty state before the first fetch resolves", () => {
    const html = renderToStaticMarkup(<LogPanel call={noop} />);
    expect(html).toContain("暂无日志");
  });
});