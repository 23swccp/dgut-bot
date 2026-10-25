import { useEffect, useRef, useState, type ReactNode } from "react";
import { quizToggle, type AccountLogin, type AppConfig, type SettingsAutosave, type SettingsCall } from "./settingsAutosave";

type Props = {
  config: AppConfig; call: SettingsCall; writer: SettingsAutosave;
  onSaved: (patch: AppConfig) => void; monitoring: boolean; children?: ReactNode;
};

function Card({ title, desc, children }: { title: string; desc?: string; children: ReactNode }) {
  return <article className="card"><h3>{title}</h3>{desc && <p>{desc}</p>}<div className="card-content">{children}</div></article>;
}
function samePath(left: string, right: string) { return left.replace(/\//g, "\\").toLowerCase() === right.replace(/\//g, "\\").toLowerCase(); }

export function SettingsPanel({ config, call, writer, onSaved, monitoring, children }: Props) {
  const [draft, setDraft] = useState(config);
  const current = useRef(config);
  const previousConfig = useRef(config);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);
  const [browsers, setBrowsers] = useState<{ name: string; path: string }[]>([]);
  const [detecting, setDetecting] = useState(false);
  const [custom, setCustom] = useState(config.browser_name === "自定义浏览器");
  const [account, setAccount] = useState<AccountLogin>({ enabled: false, username: "", has_password: false });
  const [password, setPassword] = useState("");
  const accountRef = useRef({ enabled: false, username: "", has_password: false, password: "" });
  const accountEdited = useRef(false);
  const accountVersion = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    // 首次读取配置可能晚于打开设置页；只同步没有被用户继续编辑的字段。
    const patch = Object.fromEntries(Object.entries(config).filter(([key]) =>
      (current.current as Record<string, unknown>)[key] === (previousConfig.current as Record<string, unknown>)[key])) as AppConfig;
    if ("browser_name" in patch && config.browser_name !== previousConfig.current.browser_name) {
      setCustom(config.browser_name === "自定义浏览器");
    }
    current.current = { ...current.current, ...patch };
    previousConfig.current = config;
    setDraft(current.current);
  }, [config]);

  function feedback(text: string, error = false) { if (mounted.current) { setMessage(text); setFailed(error); } }
  async function detect() {
    setDetecting(true);
    const result = await call("detect_browsers");
    if (!mounted.current) return;
    setDetecting(false);
    if (result.ok) setBrowsers(result.browsers || []); else feedback(result.error || "浏览器检测失败", true);
  }
  useEffect(() => {
    mounted.current = true;
    void detect();
    void call("get_account_login_status").then(result => {
      if (mounted.current && !accountEdited.current && result.ok && result.account) {
        setAccount(result.account); accountRef.current = { ...result.account, password: "" };
      }
    });
    return () => { mounted.current = false; void writer.flush(); };
  }, []);

  function change(patch: AppConfig) {
    current.current = { ...current.current, ...patch };
    setDraft(current.current);
  }
  async function persist(key: string, patch: AppConfig) {
    feedback("正在保存…");
    const result = await writer.save(key, "update_settings", patch);
    if (result.ok) {
      // 响应只合并本次写入的字段，避免另一项正在编辑的草稿被覆盖。
      const normalized = Object.fromEntries(Object.keys(patch).map(name => [name, (result.config as Record<string, unknown> | undefined)?.[name] ?? (patch as Record<string, unknown>)[name]])) as AppConfig;
      onSaved(normalized);
      const unchanged = Object.fromEntries(Object.entries(normalized).filter(([name]) => (current.current as Record<string, unknown>)[name] === (patch as Record<string, unknown>)[name])) as AppConfig;
      current.current = { ...current.current, ...unchanged };
      if (mounted.current) setDraft(current.current);
      if (mounted.current) feedback("已保存");
    } else if (mounted.current) feedback(result.error || "保存设置失败", true);
  }
  function savePatch(patch: AppConfig) {
    change(patch);
    void persist("browser_path" in patch ? "browser_path" : Object.keys(patch)[0], patch);
  }
  function editText(key: "browser_path" | "log_path", text: string) {
    change({ [key]: text });
    feedback("等待保存…");
    writer.defer(key, () => {
      if (key === "browser_path" && !text.trim()) { feedback("请填写自定义浏览器程序路径", true); return; }
      void persist(key, key === "browser_path" ? { browser_path: text.trim(), browser_name: "自定义浏览器" } : { log_path: text });
    });
  }
  function chooseCustomBrowser() {
    setCustom(true);
    const path = current.current.browser_path?.trim();
    if (path) savePatch({ browser_name: "自定义浏览器", browser_path: path });
    else feedback("请填写自定义浏览器程序路径");
  }
  function editAccount(patch: Partial<typeof accountRef.current>) {
    accountEdited.current = true;
    accountVersion.current += 1;
    accountRef.current = { ...accountRef.current, ...patch };
    setAccount(accountRef.current); setPassword(accountRef.current.password);
    const next = { ...accountRef.current }, version = accountVersion.current;
    const saveAccount = async () => {
      if (next.enabled && (!next.username.trim() || (!next.password && !next.has_password))) {
        feedback("请填写学号和密码后自动保存", true); return;
      }
      feedback("正在保存…");
      const result = await writer.save("account", "update_account_login", { username: next.username, password: next.password, enabled: next.enabled });
      if (!mounted.current) return;
      if (!result.ok) { feedback(result.error || "账号设置保存失败", true); return; }
      if (version === accountVersion.current && result.account) {
        accountRef.current = { ...result.account, password: "" };
        setAccount(result.account); setPassword("");
      }
      feedback("已保存");
    };
    if ("enabled" in patch) { writer.cancel("account"); void saveAccount(); }
    else writer.defer("account", () => void saveAccount());
  }
  const flush = () => { void writer.flush(); };
  const enter = (event: React.KeyboardEvent<HTMLInputElement>) => { if (event.key === "Enter") flush(); };
  function toggle(label: string, key: keyof AppConfig, disabled = false) {
    const checked = key === "campus_login_on_startup" || key === "selection_auto_leave" ? draft[key] === true : draft[key] !== false;
    return <div className="setting-line"><span>{label}</span><button type="button" role="switch" aria-checked={checked} aria-label={label} disabled={disabled} className={`switch ${checked ? "on" : ""}`} onClick={() => savePatch({ [key]: !checked })}><i /></button></div>;
  }
  function quizSwitch(label: string, key: Parameters<typeof quizToggle>[1]) {
    return <div className="setting-line"><span>{label}</span><button type="button" role="switch" aria-checked={draft[key] !== false} aria-label={label} className={`switch ${draft[key] !== false ? "on" : ""}`} onClick={() => savePatch(quizToggle(current.current, key))}><i /></button></div>;
  }

  return <section className="settings-page utility-settings"><div className="page-intro"><h2>设置</h2></div><div className="settings-surface">
    <Card title="课堂活动监测" desc="选人时自动退课，结束后恢复；重新加入可能需教师审核。">
      {toggle("签到后监测选人并自动退课／加回", "selection_auto_leave", monitoring)}
      {monitoring && <p>请先停止监测并恢复原班级，再修改此设置。</p>}
    </Card>
    <Card title="启动浏览器"><div className="browser-scan-line"><button type="button" className={`refresh-button ${detecting ? "spinning" : ""}`} aria-label="重新检测浏览器" disabled={detecting} onClick={() => void detect()}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6v5h-5"/><path d="M18.4 15a7 7 0 1 1 .1-6.1L20 11"/></svg></button></div>
      <div className="browser-list">{browsers.map(option => <button type="button" key={option.path} className={`browser-option ${!custom && samePath(draft.browser_path || "", option.path) ? "selected" : ""}`} onClick={() => { writer.cancel("browser_path"); setCustom(false); savePatch({ browser_name: option.name, browser_path: option.path }); }}><i className="radio-dot"/><strong>{option.name}</strong></button>)}<button type="button" className={`browser-option ${custom ? "selected" : ""}`} onClick={chooseCustomBrowser}><i className="radio-dot"/><strong>自定义路径</strong></button></div>
      {custom && <label className="custom-browser-path"><span>程序路径</span><input className="field" value={draft.browser_path || ""} onChange={event => editText("browser_path", event.target.value)} onBlur={flush} onKeyDown={enter}/></label>}
    </Card>
    <Card title="校园网">{toggle("开机时打开校园网登录页", "campus_login_on_startup")}</Card>
    <Card title="账号登录恢复"><div className="setting-line"><span>启用账号密码自动重新登录</span><button type="button" role="switch" aria-checked={account.enabled} aria-label="启用账号密码自动重新登录" className={`switch ${account.enabled ? "on" : ""}`} onClick={() => editAccount({ enabled: !accountRef.current.enabled })}><i /></button></div>{account.enabled && <div className="account-fields"><input className="field" value={account.username} onChange={event => editAccount({ username: event.target.value })} onBlur={flush} onKeyDown={enter} placeholder="学号" autoComplete="username" maxLength={64}/><input className="field" type="password" value={password} onChange={event => editAccount({ password: event.target.value })} onBlur={flush} onKeyDown={enter} placeholder={account.has_password ? "密码已保存；留空则不修改" : "密码"} autoComplete="current-password" maxLength={256}/></div>}</Card>
    <Card title="刷课"><label className="setting-field"><span>视频倍速</span><input className="field rate-field" type="number" min="1" max="16" step="0.5" value={draft.course_playback_rate ?? 8} onChange={event => { const rate = Math.min(16, Math.max(1, Number(event.target.value) || 1)); change({ course_playback_rate: rate }); writer.defer("course_playback_rate", () => void persist("course_playback_rate", { course_playback_rate: rate })); }} onBlur={flush} onKeyDown={enter}/></label>
      {quizSwitch("自动答题", "course_quiz_auto_answer")}
      {draft.course_quiz_auto_answer !== false && <div className="quiz-answer-options">{quizSwitch("选择题", "course_quiz_choice_enabled")}{quizSwitch("判断题", "course_quiz_judgment_enabled")}{quizSwitch("填空题", "course_quiz_blank_enabled")}</div>}
    </Card>
    <Card title="日志与数据">{toggle("保存完整运行日志", "save_log")}<div className="log-path-row"><input aria-label="日志路径" className="field" value={draft.log_path ?? "./data.md"} onChange={event => editText("log_path", event.target.value)} onBlur={flush} onKeyDown={enter}/><button className="secondary" onClick={() => { void writer.flush().then(() => call("open_log", { path: current.current.log_path })).then(result => { if (!result.ok) feedback(result.error || "无法打开日志文件", true); }); }}>打开日志</button></div></Card>
    {children}
    {message && <p role={failed ? "alert" : "status"} className="settings-save-status">{message}</p>}
  </div></section>;
}
