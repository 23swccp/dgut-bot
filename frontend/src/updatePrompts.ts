import type { UpdateStatus } from "./updateClient";

export function updateNoticeKey(status: UpdateStatus): string | null {
  if (!status.latestVersion) return null;
  const version = `${status.currentVersion}:${status.latestVersion}`;
  if (status.state === "download_failed" || (status.state === "ready_to_install" && status.error)) return `failed:${version}`;
  if (status.state === "available" && !status.error) return `available:${version}`;
  if (status.state === "ready_to_install" && status.canInstall) return `ready:${version}`;
  if (status.state === "downloading" || status.state === "verifying") return `progress:${version}`;
  return null;
}

// 只记住已展示的通知，不触发下载或安装。关闭后可从设置页手动打开。
export class UpdateNoticeMemory {
  private seen = new Set<string>();
  constructor(private storage?: Pick<Storage, "getItem" | "setItem">) {}
  claim(notice: string): boolean {
    const key = `dgut-update-dialog:${notice}`;
    if (this.seen.has(key)) return false;
    try { if (this.storage?.getItem(key)) return false; } catch { /* 回退内存。 */ }
    this.seen.add(key);
    try { this.storage?.setItem(key, "shown"); } catch { /* 同上。 */ }
    return true;
  }
}
