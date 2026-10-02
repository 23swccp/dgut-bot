// 更新消息抽屉、进度 Toast 与一次性失败对话框。
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { downloadPercent, formatBytes, toastAnimationKey, type ToastView, type UpdateStatus } from "./updateClient";
import { SafeMarkdown } from "./safeMarkdown";

export function UpdateBell({ status, open, onToggle }: { status: UpdateStatus | null; open: boolean; onToggle: () => void }) {
  const downloading = status?.state === "downloading" || status?.state === "verifying";
  const percent = status ? downloadPercent(status) : 0;
  const unread = status?.unreadCount || 0;
  return (
    <button
      type="button"
      className={`update-bell ${open ? "active" : ""}`}
      aria-label={`更新消息${unread ? `，${unread} 条未读` : ""}`}
      title={`更新消息${unread ? `（${unread} 条未读）` : ""}`}
      onClick={onToggle}
    >
      <span className="update-bell-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none">
          <path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9Z" />
          <path d="M10 21h4" />
        </svg>
      </span>
      {downloading && <span className="update-bell-ring" style={{ "--percent": percent } as CSSProperties} />}
      {!downloading && unread > 0 && <span className="update-badge" aria-hidden="true" />}
    </button>
  );
}

export function UpdateToast({ view, onOpen }: { view: ToastView | null; onOpen: () => void }) {
  const [dismissed, setDismissed] = useState("");
  useEffect(() => {
    if (!view || view.sticky) return;
    const timer = window.setTimeout(() => setDismissed(view.key), 6000);
    return () => window.clearTimeout(timer);
  }, [view?.key, view?.sticky]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!view || dismissed === view.key) return null;
  return (
    <div className={`update-toast ${view.mode}`} role="status" key={toastAnimationKey(view)}>
      <button type="button" className="update-toast-body" onClick={onOpen}>
        <span className="update-toast-title">{view.title}</span>
        {view.mode === "progress" && (
          <span className="update-progress-line"><i style={{ width: `${view.percent}%` }} /></span>
        )}
        {view.bytes && <span className="update-toast-bytes">{view.bytes}</span>}
      </button>
      <button type="button" className="update-toast-close" aria-label="收起进度提示" onClick={() => setDismissed(view.key)}>×</button>
    </div>
  );
}

type DrawerActions = {
  onClose: () => void;
  onInstall: () => void;
  onPostpone: () => void;
  onRetryDownload: () => void;
};

export function UpdateDrawer({ status, open, scroll, actions }: {
  status: UpdateStatus | null;
  open: boolean;
  scroll: { ref: React.RefObject<HTMLDivElement>; onSaveScroll: () => void };
  actions: DrawerActions;
}) {
  const drawerRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node) || drawerRef.current?.contains(target)) return;
      if (target instanceof Element && target.closest(".update-bell, .update-toast")) return;
      actions.onClose();
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open, actions.onClose]);
  if (!open) return null;
  const downloading = status?.state === "downloading";
  const percent = status ? downloadPercent(status) : 0;
  const showProgress = downloading || status?.state === "verifying";
  return (
    <aside className="update-drawer" role="dialog" aria-label="更新消息" ref={drawerRef}>
      <div className="update-drawer-body" ref={scroll.ref} onScroll={scroll.onSaveScroll}>
        {status?.changelog ? (
          <section className="update-release">
            <h4>{status.latestVersion && `v${status.latestVersion}`}{status.publishedAt && ` · ${status.publishedAt.slice(0, 10)}`}</h4>
            <SafeMarkdown source={status.changelog} />
          </section>
        ) : null}
        {status?.messages.map(message => (
          <div className={`update-message ${message.kind}${message.read ? "" : " unread"}`} key={message.id}>
            <strong>{message.title}</strong>
            {message.body && <pre>{message.body}</pre>}
            <time>{message.time.slice(5, 16).replace("T", " ")}</time>
          </div>
        ))}
      </div>
      <footer className="update-drawer-foot">
        <div className="update-foot-status">
          {downloading && (
            <>
              <span>正在下载 v{status?.latestVersion} {percent}%</span>
              <span>{formatBytes(status?.downloaded || 0)} / {formatBytes(status?.total || 0)}</span>
            </>
          )}
          {status?.state === "verifying" && <span>正在校验更新包完整性…</span>}
          {status?.state === "ready_to_install" && <span className="ok">✓ v{status.latestVersion} 已下载并通过完整性校验</span>}
          {status?.state === "download_failed" && <span className="bad">⚠ v{status.latestVersion} 下载失败：{status.error}</span>}
          {status?.handoff && <span>正在移交给更新器……</span>}
          {status?.state === "checking" && <span>正在检查更新…</span>}
          {status?.state === "available" && <span>发现新版本 v{status.latestVersion}，正在准备下载…</span>}
        </div>
        {showProgress && (
          <span className="update-progress-line drawer-progress" aria-label={`更新进度 ${percent}%`}>
            <i style={{ width: `${percent}%` }} />
          </span>
        )}
        {status?.state === "ready_to_install" && (
          <p className="update-install-warning">安装时将停止签到监测和刷课，更新完成后程序会自动重新启动。</p>
        )}
        {status?.state === "download_failed" && status.error && <p className="update-install-warning">{status.error}</p>}
        <div className="update-foot-actions">
          {status?.state === "download_failed" && (
            <>
              <button type="button" className="secondary" onClick={actions.onClose}>关闭</button>
              <button type="button" className="primary" onClick={actions.onRetryDownload}>重新下载</button>
            </>
          )}
          {status?.state === "ready_to_install" && (
            <>
              <button type="button" className="secondary" onClick={actions.onPostpone}>暂不更新</button>
              <button type="button" className="primary" onClick={actions.onInstall}>立即更新</button>
            </>
          )}
          {downloading && <button type="button" className="secondary" onClick={actions.onClose}>后台下载中，关闭抽屉</button>}
        </div>
      </footer>
    </aside>
  );
}

export function UpdateFailureDialog({ dialog, onViewLog, onLater, onRedownload }: {
  dialog: NonNullable<UpdateStatus["pendingFailureDialog"]>;
  onViewLog: () => void;
  onLater: () => void;
  onRedownload: () => void;
}) {
  return (
    <div className="update-modal-mask" role="alertdialog" aria-label="更新未完成">
      <div className="update-modal">
        <h3>{dialog.title}</h3>
        <p>
          安装 v{dialog.failedVersion} 时发生错误，已恢复到 v{dialog.restoredVersion}。当前版本可以继续正常使用。
        </p>
        <dl>
          <dt>失败阶段</dt><dd>{dialog.stage || "安装"}</dd>
          <dt>错误原因</dt><dd>{dialog.error || "未知错误"}</dd>
          {dialog.advice && (<><dt>建议</dt><dd>{dialog.advice}</dd></>)}
        </dl>
        <div className="update-modal-actions">
          <button type="button" className="secondary" onClick={onViewLog}>查看日志</button>
          <button type="button" className="secondary" onClick={onLater}>稍后重试</button>
          <button type="button" className="primary" onClick={onRedownload}>重新下载</button>
        </div>
      </div>
    </div>
  );
}

export function useScrollRestore(open: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  const savedTop = useRef(0);
  // 关闭抽屉时节点会被卸载，必须在滚动过程中持续保存阅读位置。
  const onSaveScroll = () => { if (ref.current) savedTop.current = ref.current.scrollTop; };
  useEffect(() => {
    if (open) requestAnimationFrame(() => { if (ref.current) ref.current.scrollTop = savedTop.current; });
  }, [open]);
  return { ref, onSaveScroll };
}
