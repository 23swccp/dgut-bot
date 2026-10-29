export type AppConfig = {
  browser_name?: string; browser_path?: string;
  campus_login_on_startup?: boolean; selection_auto_leave?: boolean;
  course_playback_rate?: number; course_auto_dismiss_dialog?: boolean; course_document_scroll_enabled?: boolean;
  course_quiz_auto_answer?: boolean; course_quiz_choice_enabled?: boolean;
  course_quiz_judgment_enabled?: boolean; course_quiz_blank_enabled?: boolean;
  course_ai_model_id?: number;
};
export type AccountLogin = { enabled: boolean; username: string; has_password: boolean };
export type SettingsResult = { ok: boolean; error?: string; config?: AppConfig; account?: AccountLogin; browsers?: { name: string; path: string }[] };
export type SettingsCall = (command: string, payload?: Record<string, unknown>) => Promise<SettingsResult>;

/** 串行写入，并在离开设置页或启动任务前提交尚未到期的文本编辑。 */
export class SettingsAutosave {
  private tail: Promise<unknown> = Promise.resolve();
  private pending = new Map<string, { timer: ReturnType<typeof setTimeout>; action: () => void }>();
  private errors = new Set<string>();

  constructor(private call: SettingsCall) {}

  save(key: string, command: string, payload: Record<string, unknown>): Promise<SettingsResult> {
    const result = this.tail.then(async () => {
      let response: SettingsResult;
      try { response = await this.call(command, payload); }
      catch (error) { response = { ok: false, error: String(error) }; }
      if (response.ok) this.errors.delete(key); else this.errors.add(key);
      return response;
    });
    this.tail = result;
    return result;
  }

  defer(key: string, action: () => void) {
    this.cancel(key);
    const timer = setTimeout(() => { this.pending.delete(key); action(); }, 500);
    this.pending.set(key, { timer, action });
  }

  cancel(key: string) {
    const item = this.pending.get(key);
    if (item) clearTimeout(item.timer);
    this.pending.delete(key);
  }

  async flush(): Promise<boolean> {
    for (const [key, item] of this.pending) {
      this.cancel(key);
      item.action();
    }
    await this.tail;
    return this.errors.size === 0;
  }
}

export function quizToggle(config: AppConfig, key: "course_quiz_auto_answer" | "course_quiz_choice_enabled" | "course_quiz_judgment_enabled" | "course_quiz_blank_enabled"): AppConfig {
  const patch: AppConfig = { [key]: config[key] === false };
  const types = ["course_quiz_choice_enabled", "course_quiz_judgment_enabled", "course_quiz_blank_enabled"] as const;
  if (key === "course_quiz_auto_answer" && patch[key] && types.every(type => config[type] === false)) {
    for (const type of types) patch[type] = true;
  } else if (key !== "course_quiz_auto_answer" && types.every(type => (type === key ? patch[type] : config[type]) === false)) {
    patch.course_quiz_auto_answer = false;
  }
  return patch;
}
