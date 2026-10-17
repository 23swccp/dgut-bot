// 更新状态、下载进度和展示标签。

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
