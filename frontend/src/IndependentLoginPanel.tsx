import { useCallback, useEffect, useState } from "react";
import "./independentLogin.css";

type LoginStatus = {
  authenticated: boolean; userId: number | null; displayName: string; accountName: string;
  roleId: number | null; source: string; verifiedAt: string; hasToken: boolean; cookieNames: string[];
  remembered: { username: string; hasPassword: boolean };
};
type Source = { port: number; title: string; host: string; route: string };
type Result = { ok: boolean; error?: string; independentLogin?: LoginStatus; sources?: Source[]; configuredPort?: number };
const empty: LoginStatus = { authenticated: false, userId: null, displayName: "", accountName: "",
  roleId: null, source: "", verifiedAt: "", hasToken: false, cookieNames: [],
  remembered: { username: "", hasPassword: false } };

async function command(name: string, payload: Record<string, unknown> = {}): Promise<Result> {
  try {
    const response = await fetch("/api/command", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: name, payload }) });
    if (!response.ok) return { ok: false, error: "独立登录服务暂时不可用。" };
    return await response.json() as Result;
  } catch { return { ok: false, error: "无法连接本地后端，请确认小皮卡正在运行。" }; }
}

function describe(source: Source, configuredPort: number) {
  const owner = source.port === configuredPort ? "小皮卡浏览器" : "其他调试窗口";
  return `${source.port} · ${owner} · ${source.title || source.host || "学校页面"}`;
}

export function IndependentLoginPanel() {
  const [status, setStatus] = useState<LoginStatus>(empty);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [port, setPort] = useState("9223");
  const [sources, setSources] = useState<Source[]>([]);
  const [configuredPort, setConfiguredPort] = useState(0);
  const [selectedPort, setSelectedPort] = useState("");
  const [remember, setRemember] = useState(true);
  const [scanning, setScanning] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);

  const scan = useCallback(async (keepSelection = false) => {
    setScanning(true);
    const result = await command("list_independent_login_sources");
    const found = result.sources || [];
    setSources(found);
    setConfiguredPort(result.configuredPort || 0);
    setScanning(false);
    if (!result.ok) { setMessage(result.error || "无法检测可导入的浏览器。"); setFailed(true); return; }
    setSelectedPort(current => {
      if (keepSelection && current && found.some(source => String(source.port) === current)) return current;
      return found.length ? String(found[0].port) : "";
    });
  }, []);

  useEffect(() => {
    let active = true;
    void command("get_independent_login_status").then(result => {
      if (!active) return;
      if (result.independentLogin) {
        setStatus(result.independentLogin);
        setUsername(current => current || result.independentLogin?.remembered.username || "");
      }
      if (!result.ok) { setMessage(result.error || "无法读取独立登录状态。"); setFailed(true); }
    });
    void scan();
    return () => { active = false; };
  }, [scan]);

  async function run(name: string, payload: Record<string, unknown> = {}) {
    setBusy(true); setMessage(""); setFailed(false);
    const request = command(name, payload);
    setPassword("");
    const result = await request;
    const next = result.independentLogin || empty;
    setStatus(next); setFailed(!result.ok);
    if (result.ok && next.remembered.username) setUsername(next.remembered.username);
    setMessage(result.ok
      ? name === "clear_independent_login" ? "已清除独立会话。"
        : name === "clear_independent_account" ? "已清除本机保存的账号密码。"
          : "登录身份已验证，凭据保留在本次后端会话中。"
      : result.error || "登录未成功。");
    setBusy(false);
  }

  const importPort = sources.length > 0 ? selectedPort : port;
  const savedPassword = status.remembered.hasPassword;
  const canSubmit = Boolean(username.trim()) && (Boolean(password) || savedPassword);

  return <article className="card independent-login">
    <h3>独立账号登录</h3>
    <div className="card-content">
      <p className="independent-status" role="status">{status.authenticated
        ? `已验证：${status.displayName || status.accountName || status.userId}${status.roleId === 81 ? " · 教师" : ""}` : "尚未登录独立账号"}</p>
      {status.authenticated && <dl className="independent-details">
        <dt>用户编号</dt><dd>{status.userId}</dd>
        <dt>登录方式</dt><dd>{status.source === "browser" ? "调试浏览器导入" : "学校账号密码"}</dd>
        <dt>验证时间</dt><dd>{new Date(status.verifiedAt).toLocaleString("zh-CN", { hour12: false })}</dd>
        <dt>凭据状态</dt><dd>{status.hasToken ? "已取得 Token" : "未取得 Token"}</dd>
        <dt>Cookie 名称</dt><dd>{status.cookieNames.length > 0 ? `${status.cookieNames.length} 项：${status.cookieNames.join("、")}` : "未取得"}</dd>
      </dl>}
      <form className="independent-form" onSubmit={event => {
        event.preventDefault(); if (!busy) void run("login_independent_account", { username, password, remember });
      }}>
        <label>学校账号<input className="field" aria-label="独立登录学校账号" value={username} onChange={event => setUsername(event.target.value)} autoComplete="off" maxLength={256} disabled={busy} required /></label>
        <label>密码<input className="field" aria-label="独立登录密码" type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="off" maxLength={1024} disabled={busy} placeholder={savedPassword ? "已保存密码；留空即使用已保存密码" : "密码"} required={!savedPassword} /></label>
        <button className="primary" type="submit" disabled={busy || !canSubmit}>登录并验证</button>
      </form>
      <div className="independent-remember">
        <label><input type="checkbox" checked={remember} disabled={busy} onChange={event => setRemember(event.target.checked)} />记住账号密码，下次不用再输入</label>
        {savedPassword && <span>已保存账号 {status.remembered.username} 的密码
          <button className="secondary" type="button" disabled={busy} onClick={() => void run("clear_independent_account")}>清除已保存账号</button></span>}
      </div>
      <p className="independent-note">优学院注册账号或需要验证码的账号，请先在学校调试浏览器完成登录，再导入。勾选“记住账号密码”会将账号和密码明文保存在本机；取消勾选并登录会清除已保存内容。</p>
      <form className="independent-import" onSubmit={event => {
        event.preventDefault(); if (!busy) void run("import_independent_login", { port: Number(importPort) });
      }}>
        {sources.length > 0
          ? <label className="independent-source">调试浏览器
              <select className="field" aria-label="独立登录调试端口" value={selectedPort} disabled={busy || scanning}
                onChange={event => setSelectedPort(event.target.value)}>
                {sources.map(source => <option key={source.port} value={source.port}>{describe(source, configuredPort)}</option>)}
              </select>
            </label>
          : <label className="independent-source">调试端口
              <input className="field" aria-label="独立登录调试端口" type="number" min={1024} max={65535} step={1}
                value={port} onChange={event => setPort(event.target.value)} disabled={busy} required />
            </label>}
        <button className="secondary" type="button" disabled={busy || scanning} onClick={() => void scan(true)}>{scanning ? "检测中…" : "重新检测"}</button>
        <button className="secondary" type="submit" disabled={busy || scanning || !importPort}>导入并验证</button>
      </form>
      <p className="independent-note">{sources.length > 0
        ? "只列出本机开着调试端口、且已打开学校页面的浏览器；请选择你登录学校账号的那个窗口。"
        : scanning ? "正在检测可导入的浏览器…" : "没有检测到带学校页面的调试浏览器。请用小皮卡打开的浏览器登录学校页面，或手动填写调试端口。"}</p>
      <div className="independent-footer">{busy && <span>正在获取并验证登录状态…</span>}
        <button className="secondary" type="button" disabled={busy || !status.authenticated} onClick={() => void run("clear_independent_login")}>清除独立会话</button>
      </div>
      {message && <p className={`independent-message ${failed ? "error" : "good"}`} role={failed ? "alert" : "status"}>{message}</p>}
    </div>
  </article>;
}
