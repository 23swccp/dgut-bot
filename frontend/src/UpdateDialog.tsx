import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowDownToLine, Check, LoaderCircle, X } from "lucide-react";
import { downloadPercent, formatBytes, stateLabel, type UpdateStatus } from "./updateClient";
import { UpdateNoticeMemory, updateNoticeKey } from "./updatePrompts";
import { SafeMarkdown } from "./safeMarkdown";
import "./updates.css";

export function useUpdateDialog(status: UpdateStatus | null) {
  const [open, setOpen] = useState(false);
  const memory = useRef<UpdateNoticeMemory>();
  if (!memory.current) {
    let storage: Storage | undefined;
    try { storage = window.sessionStorage; } catch { /* 无存储时使用内存。 */ }
    memory.current = new UpdateNoticeMemory(storage);
  }
  const notice = status ? updateNoticeKey(status) : null;
  useEffect(() => {
    if (!notice) return;
    const show = () => {
      if (document.visibilityState === "visible" && memory.current!.claim(notice)) setOpen(true);
    };
    show();
    document.addEventListener("visibilitychange", show);
    return () => document.removeEventListener("visibilitychange", show);
  }, [notice]);
  return { open, show: () => setOpen(true), hide: () => setOpen(false) };
}

export function UpdateDialog({ open, status, onClose, onDownload, onInstall, repo = "" }: {
  open: boolean; status: UpdateStatus | null; onClose: () => void;
  onDownload: () => Promise<void>; onInstall: () => Promise<void>;
  repo?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [requestError, setRequestError] = useState("");
  const handoff = status?.handoff === true;
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  const downloading = status?.state === "downloading" || status?.state === "verifying";
  const ready = status?.canInstall === true;
  const percent = status ? downloadPercent(status) : 0;
  const title = handoff ? "正在更新" : ready ? "更新已准备好" : downloading ? "正在下载更新"
    : status?.state === "download_failed" ? "更新下载失败" : status?.latestVersion ? "发现新版本"
    : status?.state === "checking" || !status ? "正在检查更新" : "软件更新";
  const perform = async (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setRequestError("");
    try { await action(); } catch (error) { setRequestError(String(error)); }
    finally { setBusy(false); }
  };
  return createPortal(<dialog ref={ref} className="software-update-dialog" aria-labelledby="software-update-title"
    onCancel={event => { event.preventDefault(); if (!handoff) onClose(); }}>
    <header className="update-dialog-heading"><span className="update-panel-icon" aria-hidden="true">{ready ? <Check size={24}/> : <ArrowDownToLine size={24}/>}</span><div><h2 id="software-update-title">{title}</h2><div className="update-dialog-versions">{status?.latestVersion ? <><span>v{status.currentVersion}</span><span aria-hidden="true">→</span><strong>v{status.latestVersion}</strong></> : <span>莞工小皮卡</span>}</div></div><button type="button" className="update-dialog-close" aria-label="关闭更新弹窗" disabled={handoff} onClick={onClose}><X size={20}/></button></header>
    <div className="update-dialog-body">
      {status?.changelog && <section className="update-changelog" aria-label="本次更新改进说明"><SafeMarkdown source={status.changelog} repo={repo}/></section>}
      {(status?.error || requestError) && <p className="update-panel-error" role="status">{status?.error || requestError}</p>}
      {downloading && <div className="update-download-progress"><div><strong>{status?.state === "verifying" ? "校验更新包" : `下载中 · ${percent}%`}</strong><span>{formatBytes(status?.downloaded || 0)} / {formatBytes(status?.total || 0)}</span></div><progress max={100} value={percent} aria-label="更新下载进度"/></div>}
      {!status?.latestVersion && <p className="update-dialog-status" role="status">{status ? (status.state === "idle" && !status.error ? "当前没有可用的更新" : stateLabel(status.state)) : "正在读取更新状态…"}</p>}
      {handoff && <p className="update-dialog-status" role="status"><LoaderCircle className="update-spinner" size={18}/>正在退出程序，更新后自动重启…</p>}
    </div>
    <footer className="update-dialog-footer"><button type="button" className="secondary" disabled={handoff} onClick={onClose}>{downloading ? "后台下载" : "稍后"}</button>
      {status?.canRetryDownload && <button type="button" className="primary" disabled={busy} onClick={() => void perform(onDownload)}>{busy ? "正在开始…" : status.state === "download_failed" ? "重试下载" : "下载更新"}</button>}
      {ready && !handoff && <button type="button" className="primary" disabled={busy} onClick={() => void perform(onInstall)}>{busy ? "正在开始…" : "立即更新并重启"}</button>}
    </footer>
  </dialog>, document.body);
}
