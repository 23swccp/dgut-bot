import type { EventLevel } from "./courseObservability";

export type SignEvent = {
  id: string;
  time: string;
  message: string;
  level: EventLevel;
};

export type SignEventView = SignEvent & {
  title: string;
  detail: string;
  symbol: string;
  tone: "info" | "success" | "warning" | "error" | "muted";
};

const pollLine = /^第\s*\d+\s*轮：正在检查《.+》…?$/;
const courseResult = /^\[([^\]]+)]\s*本轮完成：(.+)$/;

export function addSignEvent(current: SignEvent[], incoming: SignEvent, limit = 120) {
  const message = incoming.message.trim();
  if (!message || pollLine.test(message)) return current;

  const result = message.match(courseResult);
  if (result) {
    const key = `${result[1]}:${result[2]}`;
    const alreadyShown = current.some(event => {
      const previous = event.message.match(courseResult);
      return previous && `${previous[1]}:${previous[2]}` === key;
    });
    if (alreadyShown) return current;
  }
  return [...current, { ...incoming, message }].slice(-limit);
}

export function signEventView(event: SignEvent): SignEventView {
  const message = event.message;
  const result = message.match(courseResult);
  if (result) {
    const summary = result[2].replace(/。$/, "");
    if (summary.includes("今天没有课堂")) {
      return { ...event, title: "今日暂无课堂", detail: `${result[1]} · 无需签到，监测将继续`, symbol: "–", tone: "muted" };
    }
    if (summary.includes("未发现进行中的签到")) {
      return { ...event, title: "暂未发现签到", detail: `${result[1]} · 监测将继续`, symbol: "·", tone: "muted" };
    }
    if (summary.includes("签到活动已处理")) {
      return { ...event, title: "签到活动已处理", detail: `${result[1]} · 继续等待新的活动`, symbol: "✓", tone: "success" };
    }
  }

  const started = message.match(/^开始轮询，每\s*(\d+)\s*秒检查一次。?$/);
  if (started) return { ...event, title: "开始监测", detail: `每 ${started[1]} 秒检查一次`, symbol: "▶", tone: "success" };

  const selected = message.match(/^已选定：(.+)$/);
  if (selected) return { ...event, title: "已选择课程", detail: selected[1], symbol: "✓", tone: "info" };

  if (message.includes("签到成功")) return { ...event, title: "签到成功", detail: message.replace(/^✓\s*/, ""), symbol: "✓", tone: "success" };
  if (message.includes("发现") && message.includes("签到")) return { ...event, title: "发现签到活动", detail: message, symbol: "!", tone: "info" };
  if (event.level === "error") return { ...event, title: message, detail: "", symbol: "×", tone: "error" };
  if (event.level === "warning") return { ...event, title: message, detail: "", symbol: "!", tone: "warning" };
  return { ...event, title: message, detail: "", symbol: "·", tone: event.level === "success" ? "success" : "info" };
}
