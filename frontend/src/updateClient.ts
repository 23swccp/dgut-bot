// 更新状态的纯逻辑：格式化、标签映射与 Toast 派生；全部可单测。

export type UpdateState =
  | "idle"
  | "checking"
  | "available"
  | "downloading"
  | "download_failed"
  | "verifying"
  | "ready_to_install"
  | "handoff"
  | "waiting_for_exit"
  | "backing_up"
  | "installing"
  | "restarting"
  | "failed_rolled_back"
  | "failed_recovery_required"
  | "completed";

export type UpdateMessage = {
  id: number;
  kind: "info" | "success" | "warning" | "error";
  title: string;
  body: string;
  time: string;
  read: boolean;
};

export type UpdateStatus = {
  currentVersion: string;
  state: UpdateState;
  latestVersion: string;
  publishedAt: string;
  changelog: string;
  downloaded: number;
  total: number;
  percent: number;
  error: string;
  messages: UpdateMessage[];
  unreadCount: number;
  downloading: boolean;
  handoff: boolean;
  readyForExit: boolean;
  canInstall: boolean;
  canRetryDownload: boolean;
  pendingFailureDialog: {
    title: string;
    restoredVersion: string;
    failedVersion: string;
    stage: string;
    error: string;
    advice: string;
  } | null;
};

export const HANDOFF_STATES: readonly UpdateState[] = ["handoff", "waiting_for_exit"];

export function isHandoff(state: UpdateState): boolean {
  return HANDOFF_STATES.includes(state);
}

export function formatBytes(value: number): string {
  let amount = Number(value) || 0;
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (amount < 1024 || unit === "GB") return `${amount.toFixed(1)} ${unit}`;
    amount /= 1024;
  }
  return `${amount.toFixed(1)} GB`;
}

export function downloadPercent(status: Pick<UpdateStatus, "downloaded" | "total" | "state">): number {
  if (status.state === "verifying" || status.state === "ready_to_install") return 100;
  if (!status.total) return 0;
  return Math.min(100, Math.round((status.downloaded * 100) / status.total));
}

export function stateLabel(state: UpdateState): string {
  const labels: Record<UpdateState, string> = {
    idle: "暂无更新",
    checking: "正在检查更新…",
    available: "发现新版本",
    downloading: "正在下载更新…",
    download_failed: "下载失败",
    verifying: "正在校验更新包…",
    ready_to_install: "更新已准备好",
    handoff: "正在移交给更新器……",
    waiting_for_exit: "正在移交给更新器……",
    backing_up: "正在备份…",
    installing: "正在安装…",
    restarting: "正在重启…",
    failed_rolled_back: "更新未完成",
    failed_recovery_required: "更新未完成，需要手动恢复",
    completed: "已是最新版本",
  };
  return labels[state] || state;
}

export type ToastView = {
  key: string;
  mode: "progress" | "done" | "error" | "handoff";
  title: string;
  percent: number;
  bytes: string;
  sticky: boolean;
};

const ACTIVE_TOAST_STATES: readonly UpdateState[] = [
  "downloading", "verifying", "ready_to_install", "download_failed", "handoff", "waiting_for_exit", "completed",
];

export function toastFor(status: UpdateStatus): ToastView | null {
  if (!ACTIVE_TOAST_STATES.includes(status.state)) return null;
  const version = status.latestVersion || "";
  const key = `${status.state}-${version}`;
  switch (status.state) {
    case "downloading":
      return {
        key, mode: "progress", sticky: true,
        title: `正在下载 v${version}`, percent: downloadPercent(status),
        bytes: `${formatBytes(status.downloaded)} / ${formatBytes(status.total)}`,
      };
    case "verifying":
      return { key, mode: "progress", sticky: true, title: `正在校验 v${version}…`, percent: 100, bytes: "" };
    case "ready_to_install":
      return { key, mode: "done", sticky: true, title: `✓ v${version} 更新已准备好`, percent: 100, bytes: "" };
    case "download_failed":
      return { key, mode: "error", sticky: false, title: `⚠ v${version} 下载失败`, percent: 0, bytes: "" };
    case "handoff":
    case "waiting_for_exit":
      return { key, mode: "handoff", sticky: true, title: "正在移交给更新器……", percent: 100, bytes: "" };
    case "completed":
      return { key, mode: "done", sticky: false, title: `✓ 已更新到 v${version || status.currentVersion}`, percent: 100, bytes: "" };
    default:
      return null;
  }
}

// 下载中只在进度条宽度上变化，不重放进场动画：key 只随模式与版本变化。
export function toastAnimationKey(view: ToastView): string {
  const version = view.key.split("-").slice(1).join("-");
  return `${view.mode}-${version}`;
}
