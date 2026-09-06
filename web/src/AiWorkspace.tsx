import { useEffect, useRef, useState } from "react";
import { SafeMarkdown } from "./safeMarkdown";

type Role = "user" | "assistant";
type Message = { id: string; role: Role; content: string; reasoning?: string };
type ChatResult = { ok: boolean; error?: string; answer?: string; reasoning?: string; upstreamToolCallCount?: number };
type AiModel = { id: number; name: string; vision: boolean; online: boolean; thinking: boolean };
type ModelsResult = { ok: boolean; error?: string; models?: AiModel[]; selectedModelId?: number };

export function AiWorkspace() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [models, setModels] = useState<AiModel[]>([]);
  const [modelId, setModelId] = useState(1);
  const [modelsLoading, setModelsLoading] = useState(true);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const heartbeat = () => void fetch("/api/heartbeat", { method: "POST", keepalive: true }).catch(() => undefined);
    heartbeat();
    const timer = window.setInterval(heartbeat, 2000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    void command<ModelsResult>("ai_models", {}).then(result => {
      if (!result.ok) throw new Error(result.error || "无法读取 AI 模型");
      setModels(result.models || []);
      setModelId(result.selectedModelId || result.models?.[0]?.id || 1);
    }).catch(reason => {
      setError(reason instanceof Error ? reason.message : "无法读取 AI 模型");
    }).finally(() => setModelsLoading(false));
  }, []);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, busy]);

  async function command<T>(name: string, payload: Record<string, unknown>): Promise<T> {
    const response = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: name, payload }),
    });
    return await response.json() as T;
  }

  async function chooseModel(value: number) {
    const previous = modelId;
    setModelId(value);
    setError("");
    try {
      const result = await command<{ ok: boolean; error?: string }>("update_settings", { course_ai_model_id: value });
      if (!result.ok) throw new Error(result.error || "模型设置保存失败");
    } catch (reason) {
      setModelId(previous);
      setError(reason instanceof Error ? reason.message : "模型设置保存失败");
    }
  }

  async function send() {
    const content = input.trim();
    if (!content || busy) return;
    const userMessage: Message = { id: crypto.randomUUID(), role: "user", content };
    const history = [...messages, userMessage];
    setMessages(history);
    setInput("");
    setError("");
    setBusy(true);
    try {
      const result = await command<ChatResult>("ai_chat", {
        modelId,
        messages: history.map(({ role, content: text }) => ({ role, content: text })),
      });
      if (!result.ok) throw new Error(result.error || "AI 请求失败");
      setMessages(items => [...items, {
        id: crypto.randomUUID(),
        role: "assistant",
        content: result.answer || "",
        reasoning: result.reasoning || "",
      }]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "AI 请求失败");
    } finally {
      setBusy(false);
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  }

  return <main className="ai-shell">
    <header className="ai-header">
      <div className="ai-brand"><span>✦</span><div><strong>AI 工作台</strong><small>优学院课程 AI · 实验功能</small></div></div>
      <div className="ai-header-actions">
        <label className="ai-model-select"><span>模型</span><select aria-label="AI 模型" value={modelId} disabled={busy || modelsLoading || models.length === 0} onChange={event => void chooseModel(Number(event.target.value))}>
          {modelsLoading && <option value={modelId}>读取中…</option>}
          {!modelsLoading && models.length === 0 && <option value={modelId}>不可用</option>}
          {models.map(model => <option key={model.id} value={model.id}>{model.name}</option>)}
        </select></label>
        <span className="ai-status">本地安全中继</span><button type="button" onClick={() => window.close()}>关闭</button>
      </div>
    </header>
    <section className="ai-conversation" aria-live="polite">
      {messages.length === 0 && <div className="ai-empty">
        <div className="ai-orb">✦</div>
        <h1>从课程上下文开始</h1>
        <p>只要程序中存在有效登录缓存，就能直接连接课程 AI，无需打开优学院。缓存失效时，请重新登录后让程序读取课程。</p>
        <div className="ai-suggestions">
          {["梳理这门课的重点", "解释一个课程概念", "制定本周复习计划"].map(text => <button key={text} type="button" onClick={() => { setInput(text); inputRef.current?.focus(); }}>{text}</button>)}
        </div>
      </div>}
      {messages.map(message => <article key={message.id} className={`ai-message ${message.role}`}>
        <div className="ai-avatar">{message.role === "user" ? "你" : "✦"}</div>
        <div className="ai-bubble">
          {message.reasoning && <details><summary>思考过程</summary><pre>{message.reasoning}</pre></details>}
          {message.role === "assistant" ? <SafeMarkdown source={message.content}/> : <p>{message.content}</p>}
        </div>
      </article>)}
      {busy && <article className="ai-message assistant"><div className="ai-avatar">✦</div><div className="ai-bubble ai-thinking"><i/><i/><i/></div></article>}
      <div ref={endRef}/>
    </section>
    <footer className="ai-composer-wrap">
      {error && <div className="ai-error">{error}</div>}
      <div className="ai-composer">
        <textarea ref={inputRef} autoFocus value={input} disabled={busy} maxLength={12000} rows={1} placeholder="向课程 AI 提问…" onChange={event => setInput(event.target.value)} onKeyDown={event => {
          if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); }
        }}/>
        <button type="button" aria-label="发送" disabled={busy || !input.trim()} onClick={() => void send()}>➤</button>
      </div>
      <small>Enter 发送 · Shift + Enter 换行 · 模型回答可能不准确</small>
    </footer>
  </main>;
}
