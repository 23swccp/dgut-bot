import { ArrowDownToLine, Check, RefreshCw } from "lucide-react";
import { stateLabel, type UpdateStatus } from "./updateClient";
import "./updates.css";

export function UpdateSettings({ status, version, onCheck, onOpen }: {
  status: UpdateStatus | null; version: string; onCheck: () => void; onOpen: () => void;
}) {
  const downloading = status?.state === "downloading" || status?.state === "verifying";
  const active = downloading || status?.state === "checking" || status?.handoff;
  const heading = !status ? "正在读取更新状态…"
    : status.error ? "更新暂未完成"
    : status.state === "idle" ? (status.messages.some(message => message.title.startsWith("已是最新版本")) ? "当前已是最新版本" : "随时检查新版本")
    : stateLabel(status.state);
  return <article className="card software-update" aria-label="软件更新">
    <div className="update-panel-heading"><span className="update-panel-icon" aria-hidden="true">{status?.canInstall ? <Check size={22}/> : <ArrowDownToLine size={22}/>}</span><div><h3>软件更新</h3><p>当前版本 v{version || status?.currentVersion || "…"}</p></div><span className={`update-panel-state ${status?.error ? "error" : ""}`}>{heading}</span></div>
    {status?.error && <p className="update-panel-error" role="status">{status.error}</p>}
    {status?.pendingFailureDialog && <p className="update-panel-error" role="status">{status.pendingFailureDialog.title}：{status.pendingFailureDialog.error} {status.pendingFailureDialog.advice}</p>}
    <div className="update-panel-actions"><button type="button" className="secondary" disabled={active || !status} onClick={onCheck}><RefreshCw size={15}/>{status?.state === "checking" ? "检查中…" : "检查更新"}</button>{status?.latestVersion && <button type="button" className="primary" onClick={onOpen}>查看更新</button>}</div>
  </article>;
}
