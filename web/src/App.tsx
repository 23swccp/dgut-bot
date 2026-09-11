import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { BookOpenCheck, CircleQuestionMark, ClipboardCheck, Settings, type LucideIcon } from "lucide-react";
import {
  formatClock, mergeEvents, visibleCourseEvents,
  type CourseEvent, type CourseStatus,
} from "./courseObservability";
import { stateLabel, toastFor, type UpdateStatus } from "./updateClient";
import { UpdateBell, UpdateDrawer, UpdateFailureDialog, UpdateToast, useScrollRestore } from "./UpdateDrawer";
import { AboutGuide } from "./AboutGuide";
import { addSignEvent, signEventView, type SignEvent } from "./signObservability";
import { filterLessonGroups, lessonProgress, openPhaseLabel, type CourseScanStatus, type LessonItem } from "./courseScanView";
import "./updateDrawer.css";
import "./courseScan.css";

type Page = "terminal" | "learning" | "settings" | "about";
type Phase = "ready" | "login" | "courses" | "selected" | "monitoring";
type AccountLogin = { enabled: boolean; username: string; has_password: boolean };
type Course = { id: number; name: string; teacherName: string };
type BrowserOption = { name: string; path: string };
type AppConfig = {
  browser_name?: string; browser_path?: string; save_log?: boolean; log_path?: string;
  campus_login_on_startup?: boolean;
  course_playback_rate?: number; course_auto_dismiss_dialog?: boolean; course_document_scroll_enabled?: boolean;
  course_quiz_auto_answer?: boolean; course_quiz_choice_enabled?: boolean;
  course_quiz_judgment_enabled?: boolean; course_quiz_blank_enabled?: boolean;
  course_ai_model_id?: number;
};
type AppInfo = { appName: string; version: string; repo: string };
type SignMonitorStatus = { running: boolean; state: string; courseName: string; intervalSeconds: number; round: number; startedAt: string; lastCheck: string; lastResult: string };
type BackendResult = { ok: boolean; error?: string; courses?: Course[]; course?: Course | null; config?: AppConfig; account?: AccountLogin; browsers?: BrowserOption[]; events?: CourseEvent[]; latestSeq?: number; status?: CourseStatus; signStatus?: SignMonitorStatus; scan?: CourseScanStatus; info?: AppInfo; update?: UpdateStatus };

const loginPayload = { url: "https://lms.dgut.edu.cn" };
const moduleIcons: Record<Page, LucideIcon> = {
  terminal: ClipboardCheck,
  learning: BookOpenCheck,
  settings: Settings,
  about: CircleQuestionMark,
};

function App() {
  const [page, setPage] = useState<Page>("terminal");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const reduceMotion = useReducedMotion();
  const [phase, setPhase] = useState<Phase>("ready");
  const [command, setCommand] = useState("");
  const [courseCursor, setCourseCursor] = useState(0);
  const [learningCommand, setLearningCommand] = useState("");
  const [busy, setBusy] = useState(false);
  const [signEvents, setSignEvents] = useState<SignEvent[]>(() => [{ id: "initial", time: new Date().toISOString(), message: "正在连接本地后端…", level: "info" }]);
  const [signStatus, setSignStatus] = useState<SignMonitorStatus>({ running: false, state: "idle", courseName: "", intervalSeconds: 5, round: 0, startedAt: "", lastCheck: "", lastResult: "等待启动" });
  const [selectedSignCourse, setSelectedSignCourse] = useState<Course | null>(null);
  const [learningLogs, setLearningLogs] = useState<string[]>([]);
  const [courseEvents, setCourseEvents] = useState<CourseEvent[]>([]);
  const [courseStatus, setCourseStatus] = useState<CourseStatus>({ running: false, connected: false, controllerState: "IDLE" });
  const [courses, setCourses] = useState<Course[]>([]);
  const [logging, setLogging] = useState(true);
  const [browser, setBrowser] = useState("自动检测");
  const [path, setPath] = useState("");
  const [detectedBrowsers, setDetectedBrowsers] = useState<BrowserOption[]>([]);
  const [detectingBrowsers, setDetectingBrowsers] = useState(false);
  const [logPath, setLogPath] = useState("./签到记录.md");
  const [campusLoginOnStartup, setCampusLoginOnStartup] = useState(false);
  const [accountEnabled, setAccountEnabled] = useState(false);
  const [accountName, setAccountName] = useState("");
  const [accountPassword, setAccountPassword] = useState("");
  const [hasSavedPassword, setHasSavedPassword] = useState(false);
  const [playbackRate, setPlaybackRate] = useState(8);
  const [autoDismiss, setAutoDismiss] = useState(true);
  const [documentScroll, setDocumentScroll] = useState(true);
  const [quizAutoAnswer, setQuizAutoAnswer] = useState(false);
  const [quizChoiceEnabled, setQuizChoiceEnabled] = useState(true);
  const [quizJudgmentEnabled, setQuizJudgmentEnabled] = useState(true);
  const [quizBlankEnabled, setQuizBlankEnabled] = useState(true);
  const [helperRunning, setHelperRunning] = useState(false);
  const [courseScan, setCourseScan] = useState<CourseScanStatus>({ state: "idle", loggedIn: false, scannedCourses: 0, totalCourses: 0, unfinishedCount: 0, groups: [], failures: [], cachedAt: "", fromCache: false, error: "", openPhase: "idle", openError: "", selectedId: "" });
  const [lessonQuery, setLessonQuery] = useState("");
  const [lessonCursor, setLessonCursor] = useState(0);
  const [exitingLearning, setExitingLearning] = useState(false);
  const [saved, setSaved] = useState("");
  const [updateStatus, setUpdateStatus] = useState<UpdateStatus | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [appInfo, setAppInfo] = useState<AppInfo | null>(null);
  const handoffSentRef = useRef(false);
  const autoOpenedUpdateRef = useRef("");
  const signInitializationStartedRef = useRef(false);
  const learningScanStartedRef = useRef(false);
  const drawerScroll = useScrollRestore(drawerOpen);
  const signLogRef = useRef<HTMLDivElement>(null);
  const commandRef = useRef<HTMLInputElement>(null);
  const learningEndRef = useRef<HTMLPreElement>(null);
  const learningLogRef = useRef<HTMLDivElement>(null);
  const courseLastSeqRef = useRef(0);
  const learningAutoScrollRef = useRef(true);
  const learningCommandRef = useRef<HTMLInputElement>(null);
  const coursePickerRef = useRef<HTMLDivElement>(null);
  const append = (line: string, level: CourseEvent["level"] = "info") => setSignEvents(items => addSignEvent(items, {
    id: `local-${Date.now()}-${Math.random()}`,
    time: new Date().toISOString(),
    message: line,
    level,
  }));
  const appendLearning = (line: string) => setLearningLogs(items => [...items, `[${formatClock(new Date().toISOString())}] ${line}`].slice(-80));
  const courseChoices = [...new Map<number, Course>(courses.map(course => [course.id, course])).values()];
  const courseQuery = command.trim().toLocaleLowerCase();
  const matchingCourses = courseChoices.filter(course => !courseQuery || `${course.name} ${course.teacherName} ${course.id}`.toLocaleLowerCase().includes(courseQuery));
  const filteredLessonGroups = filterLessonGroups(courseScan.groups, lessonQuery);
  const filteredLessons = filteredLessonGroups.flatMap(group => group.items);
  const selectedLesson = filteredLessons[lessonCursor] || courseScan.groups.flatMap(group => group.items).find(item => item.id === courseScan.selectedId);

  useEffect(() => { const log = signLogRef.current; log?.scrollTo({ top: log.scrollHeight, behavior: "smooth" }); }, [signEvents]);
  useEffect(() => {
    const target = learningLogRef.current;
    if (target && learningAutoScrollRef.current) target.scrollTo({ top: target.scrollHeight, behavior: "smooth" });
  }, [learningLogs, courseEvents]);
  useEffect(() => { if (phase === "courses") setCourseCursor(0); }, [command, courses, phase]);
  useEffect(() => { coursePickerRef.current?.querySelector(".course-option.active")?.scrollIntoView({ block: "nearest" }); }, [courseCursor]);
  useEffect(() => { setLessonCursor(0); }, [lessonQuery, courseScan.cachedAt]);
  useEffect(() => {
    if (busy) return;
    const target = page === "terminal" ? commandRef : page === "learning" ? learningCommandRef : null;
    if (!target) return;
    const frame = requestAnimationFrame(() => target.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [page, busy]);
  useEffect(() => {
    void call("get_app_info").then(result => { if (result.ok && result.info) setAppInfo(result.info); });
    void call("get_settings").then(result => {
      if (result.ok && result.config) {
        loadConfig(result.config);
        append("本地后端已连接，正在自动读取本地登录缓存…");
        if (!signInitializationStartedRef.current) {
          signInitializationStartedRef.current = true;
          void initializeSignIn();
        }
      }
      else append(`后端连接失败：${result.error || "未知错误"}`);
    });
    void call("get_account_login_status").then(result => { if (result.ok && result.account) loadAccount(result.account); });
    void detectInstalledBrowsers();
  }, []);
  useEffect(() => {
    if (page !== "learning" || learningScanStartedRef.current) return;
    learningScanStartedRef.current = true;
    void call("start_course_scan").then(result => { if (result.ok && result.scan) setCourseScan(result.scan); });
  }, [page]);
  useEffect(() => {
    if (page === "learning" && courseScan.state === "auth_required" && courses.length > 0) {
      void refreshCourseScan(false);
    }
  }, [page, phase, courseScan.state, courses.length]);
  useEffect(() => {
    let disposed = false;
    const pullScan = async () => {
      const result = await call("get_course_scan_status");
      if (!disposed && result.ok && result.scan) setCourseScan(result.scan);
    };
    void pullScan();
    const timer = window.setInterval(() => void pullScan(), 750);
    return () => { disposed = true; window.clearInterval(timer); };
  }, []);
  useEffect(() => {
    if (phase !== "login") return;
    let disposed = false;
    let checking = false;
    const detectLogin = async () => {
      if (checking) return;
      checking = true;
      const result = await call("load_session_and_courses", { automatic: true, waitSeconds: 1 });
      checking = false;
      if (disposed || !result.ok || !result.courses?.length) return;
      setCourses(result.courses);
      setPhase("courses");
      setBusy(false);
      append("已自动检测到登录状态，请在下方选择课程。");
    };
    void detectLogin();
    const timer = window.setInterval(() => void detectLogin(), 1500);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [phase]);
  useEffect(() => {
    let disposed = false;
    const pullEvents = async () => {
      try { const result = await call("get_events", { afterSeq: courseLastSeqRef.current }); if (!disposed) {
        const events = result.events || [];
        const learning = events.filter(event => event.sessionId.startsWith("course-") || event.category !== "general");
        const general = events.filter(event => event.category === "general");
        if (learning.length) setCourseEvents(items => mergeEvents(items, learning));
        general.forEach(event => setSignEvents(items => addSignEvent(items, {
          id: `backend-${event.seq}`,
          time: event.time,
          message: event.message,
          level: event.level,
        })));
        courseLastSeqRef.current = Math.max(courseLastSeqRef.current, result.latestSeq || 0);
      } }
      catch { /* 本地服务启动和关闭阶段安静忽略。 */ }
    };
    void pullEvents();
    const timer = window.setInterval(() => void pullEvents(), 750);
    return () => { disposed = true; window.clearInterval(timer); };
  }, []);
  useEffect(() => {
    let disposed = false;
    const pullSignStatus = async () => {
      const result = await call("get_sign_monitor_status");
      if (!disposed && result.ok && result.signStatus) {
        setSignStatus(result.signStatus);
        setPhase(current => result.signStatus?.running
          ? (current === "login" || current === "courses" ? current : "monitoring")
          : current === "monitoring" ? "selected" : current);
      }
    };
    void pullSignStatus();
    const timer = window.setInterval(() => void pullSignStatus(), 1000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, []);
  useEffect(() => {
    let disposed = false;
    const pullStatus = async () => {
      const result = await call("get_course_helper_status");
      if (!disposed && result.ok && result.status) {
        setCourseStatus(result.status);
        setHelperRunning(Boolean(result.status.running));
      }
    };
    void pullStatus();
    const timer = window.setInterval(() => void pullStatus(), 750);
    return () => { disposed = true; window.clearInterval(timer); };
  }, []);
  useEffect(() => {
    const heartbeat = () => void fetch("/api/heartbeat", { method: "POST", keepalive: true }).catch(() => undefined);
    const closing = () => {
      if (!navigator.sendBeacon("/api/client-closed", "")) {
        void fetch("/api/client-closed", { method: "POST", keepalive: true }).catch(() => undefined);
      }
    };
    heartbeat();
    const timer = window.setInterval(heartbeat, 2000);
    window.addEventListener("pagehide", closing);
    return () => { window.clearInterval(timer); window.removeEventListener("pagehide", closing); };
  }, []);
  // 更新状态轮询：页面刷新后由后端持久化状态恢复真实进度。
  useEffect(() => {
    let disposed = false;
    const pullUpdate = async () => {
      try {
        const result = await call("get_update_status");
        if (!disposed && result.ok && result.update) setUpdateStatus(result.update);
      } catch { /* 服务关闭阶段的请求失败安静忽略。 */ }
    };
    void pullUpdate();
    const timer = window.setInterval(() => void pullUpdate(), 1000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, []);
  // 每个新版本只自动展开一次；用户手动收起后，本次运行不再反复打扰。
  useEffect(() => {
    const state = updateStatus?.state;
    const version = updateStatus?.latestVersion || "";
    const canShowDrawer = page === "terminal" || page === "learning";
    const shouldOpen = state === "available"
      || state === "downloading"
      || state === "verifying"
      || state === "ready_to_install";
    if (!canShowDrawer || !shouldOpen || !version || autoOpenedUpdateRef.current === version) return;
    autoOpenedUpdateRef.current = version;
    setDrawerOpen(true);
    void call("mark_update_read");
  }, [page, updateStatus?.latestVersion, updateStatus?.state]);
  // 移交确认后由后端先接收关机请求，再通过 CDP 精确关闭助手标签页。
  useEffect(() => {
    if (!updateStatus?.readyForExit || handoffSentRef.current) return;
    handoffSentRef.current = true;
    append("正在移交给更新器……");
    window.setTimeout(() => {
      void call("shutdown_for_update");
    }, 700);
  }, [updateStatus?.readyForExit]);
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setDrawerOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  async function call(commandName: string, payload: Record<string, unknown> = {}): Promise<BackendResult> {
    try {
      const response = await fetch("/api/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ command: commandName, payload }) });
      return await response.json() as BackendResult;
    } catch (error) { return { ok: false, error: `后端连接失败：${String(error)}` }; }
  }
  function loadConfig(config: AppConfig) {
    setBrowser(config.browser_name || "自动检测"); setPath(config.browser_path || ""); setLogging(config.save_log !== false); setLogPath(config.log_path || "./签到记录.md");
    setCampusLoginOnStartup(config.campus_login_on_startup === true);
    setPlaybackRate(config.course_playback_rate || 8); setAutoDismiss(config.course_auto_dismiss_dialog !== false); setDocumentScroll(config.course_document_scroll_enabled !== false);
    const choiceEnabled = config.course_quiz_choice_enabled !== false; const judgmentEnabled = config.course_quiz_judgment_enabled !== false; const blankEnabled = config.course_quiz_blank_enabled !== false;
    setQuizAutoAnswer(config.course_quiz_auto_answer !== false && (choiceEnabled || judgmentEnabled || blankEnabled)); setQuizChoiceEnabled(choiceEnabled);
    setQuizJudgmentEnabled(judgmentEnabled); setQuizBlankEnabled(blankEnabled);
  }
  function loadAccount(account: AccountLogin) {
    setAccountEnabled(account.enabled); setAccountName(account.username); setHasSavedPassword(account.has_password); setAccountPassword("");
  }
  function samePath(left: string, right: string) { return left.replace(/\//g, "\\").toLowerCase() === right.replace(/\//g, "\\").toLowerCase(); }
  async function detectInstalledBrowsers() {
    setDetectingBrowsers(true); const result = await call("detect_browsers"); setDetectingBrowsers(false);
    if (result.ok) setDetectedBrowsers(result.browsers || []);
    else save(result.error || "浏览器检测失败");
  }
  function chooseDetectedBrowser(option: BrowserOption) { setBrowser(option.name); setPath(option.path); }
  function chooseCustomBrowser() {
    const isDetectedPath = detectedBrowsers.some(option => samePath(option.path, path));
    setBrowser("自定义浏览器");
    if (isDetectedPath) setPath("");
  }
  function toggleQuizAutoAnswer() {
    if (quizAutoAnswer) { setQuizAutoAnswer(false); return; }
    if (!quizChoiceEnabled && !quizJudgmentEnabled && !quizBlankEnabled) {
      setQuizChoiceEnabled(true); setQuizJudgmentEnabled(true); setQuizBlankEnabled(true);
    }
    setQuizAutoAnswer(true);
  }
  function toggleQuizChoice() {
    const next = !quizChoiceEnabled; setQuizChoiceEnabled(next);
    if (!next && !quizJudgmentEnabled && !quizBlankEnabled) setQuizAutoAnswer(false);
  }
  function toggleQuizJudgment() {
    const next = !quizJudgmentEnabled; setQuizJudgmentEnabled(next);
    if (!quizChoiceEnabled && !next && !quizBlankEnabled) setQuizAutoAnswer(false);
  }
  function toggleQuizBlank() {
    const next = !quizBlankEnabled; setQuizBlankEnabled(next);
    if (!quizChoiceEnabled && !quizJudgmentEnabled && !next) setQuizAutoAnswer(false);
  }
  function save(message: string) { setSaved(message); window.setTimeout(() => setSaved(""), 2200); }
  async function saveSettings(values: Record<string, unknown>, message: string) {
    setBusy(true); const result = await call("update_settings", values); setBusy(false);
    if (result.ok) { if (result.config) loadConfig(result.config); save(message); }
  }
  async function saveAllSettings() {
    if (busy) return;
    const browserPath = path.trim();
    if (browser === "自定义浏览器" && !browserPath) { save("请填写自定义浏览器程序路径"); return; }
    setPath(browserPath); setBusy(true);
    const settings = await call("update_settings", {
      browser_name: browser === "自动检测" ? "" : browser,
      browser_path: browserPath,
      save_log: logging,
      log_path: logPath,
      campus_login_on_startup: campusLoginOnStartup,
      course_playback_rate: playbackRate,
      course_auto_dismiss_dialog: autoDismiss,
      course_document_scroll_enabled: documentScroll,
      course_quiz_auto_answer: quizAutoAnswer,
      course_quiz_choice_enabled: quizChoiceEnabled,
      course_quiz_judgment_enabled: quizJudgmentEnabled,
      course_quiz_blank_enabled: quizBlankEnabled,
    });
    if (!settings.ok) { setBusy(false); save(settings.error || "保存设置失败"); return; }
    const account = await call("update_account_login", { username: accountName, password: accountPassword, enabled: accountEnabled });
    setBusy(false);
    if (!account.ok) { save(account.error || "账号设置保存失败"); return; }
    if (settings.config) loadConfig(settings.config);
    if (account.account) loadAccount(account.account);
    save("所有设置已保存");
  }
  async function openLog() {
    if (busy) return; setBusy(true); const result = await call("open_log", { path: logPath }); setBusy(false);
    if (!result.ok) save(result.error || "无法打开日志文件");
  }
  function toggleDrawer() {
    setDrawerOpen(open => {
      const next = !open;
      if (next) void call("mark_update_read");
      return next;
    });
  }
  async function checkUpdate() {
    const result = await call("check_update");
    if (!result.ok) save(result.error || "无法检查更新");
  }
  async function installUpdate() {
    const result = await call("install_update");
    if (!result.ok) save(result.error || "无法开始安装");
  }
  async function retryDownload() {
    const result = await call("download_update");
    if (!result.ok) save(result.error || "无法开始下载");
  }
  async function ackFailure(action: "later" | "redownload" | "log") {
    await call("ack_update_failure");
    if (action === "log") { await call("open_log", { path: "browser-service.log" }); return; }
    if (action === "redownload") { setDrawerOpen(true); await retryDownload(); }
  }
  async function chooseSignCourse(course: Course) {
    if (busy) return;
    setBusy(true); const result = await call("select_course", { query: String(course.id) }); setBusy(false);
    if (result.ok && result.course) {
      setSelectedSignCourse(result.course); setCommand(""); setPhase("selected"); append(`已选定：${result.course.name} · ${result.course.teacherName}`);
    } else append("选择课程失败，请重新选择。");
  }
  async function initializeSignIn() {
    setBusy(true);
    const result = await call("load_saved_courses");
    if (result.ok && result.courses?.length) {
      setCourses(result.courses);
      setPhase("courses");
      setBusy(false);
      return;
    }
    append("登录缓存不可用，正在自动打开优学院登录页…");
    setPhase("login");
    const opened = await call("start_browser", loginPayload);
    append(opened.ok ? "登录页已准备好。请在浏览器中完成登录，程序将自动继续。" : `启动浏览器失败：${opened.error || "未知错误"}`);
    setBusy(false);
  }
  async function refreshCourseScan(force = true) {
    const result = await call("start_course_scan", { force });
    if (result.ok && result.scan) setCourseScan(result.scan);
  }
  async function cancelCourseScan() {
    const result = await call("cancel_course_scan");
    if (result.ok && result.scan) setCourseScan(result.scan);
  }
  async function exitCourseLearning() {
    if (exitingLearning) return;
    setExitingLearning(true);
    const result = await call("stop_course_helper");
    setExitingLearning(false);
    if (!result.ok) {
      appendLearning(result.error || "退出刷课失败。");
      return;
    }
    setHelperRunning(false);
    setCourseEvents([]);
    setLearningLogs([]);
    setCourseScan(current => ({ ...current, openPhase: "idle", openError: "", selectedId: "" }));
  }
  async function openScannedLesson(item: LessonItem) {
    if (!item.canAutoOpen || learningActive) return;
    setLessonCursor(Math.max(0, filteredLessons.findIndex(candidate => candidate.id === item.id)));
    const result = await call("open_scanned_course", { itemId: item.id });
    if (!result.ok) appendLearning(result.error || "自动打开课件失败。");
    else if (result.scan) setCourseScan(result.scan);
  }
  function handleLessonKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") { event.preventDefault(); setLessonCursor(value => Math.min(value + 1, Math.max(0, filteredLessons.length - 1))); }
    else if (event.key === "ArrowUp") { event.preventDefault(); setLessonCursor(value => Math.max(0, value - 1)); }
    else if (event.key === "Enter" && filteredLessons[lessonCursor]) { event.preventDefault(); void openScannedLesson(filteredLessons[lessonCursor]); }
    else if (event.key === "Escape") setLessonQuery("");
  }
  async function runLearning() {
    if (busy) return;
    const input = learningCommand.trim(); const normalized = input.toLowerCase(); setLearningCommand("");
    if (normalized === "clear" || input === "清屏") { setLearningLogs([]); setCourseEvents([]); return; }
    if (normalized === "open" || input === "打开") {
      appendLearning("> open"); appendLearning("正在打开优学院课件网站…");
      await call("start_browser", { url: "https://ua.dgut.edu.cn" }); return;
    }
    if (normalized === "stop" || input === "/" || input === "停止") {
      appendLearning("> stop"); setBusy(true); const result = await call("stop_course_helper"); setBusy(false);
      if (result.ok) { setHelperRunning(false); appendLearning("刷课已停止。"); } else appendLearning(result.error || "停止刷课失败。");
      return;
    }
    const speed = input.match(/^(?:speed|倍速)\s+([0-9]+(?:\.[0-9]+)?)$/i);
    if (speed) {
      const rate = Number(speed[1]);
      if (rate < 1 || rate > 16) { appendLearning("倍速范围为 1–16。"); return; }
      appendLearning(`> speed ${rate}`); setPlaybackRate(rate); setBusy(true);
      await call("update_settings", { course_playback_rate: rate });
      if (helperRunning) await call("set_course_speed", { rate });
      setBusy(false); appendLearning(`视频倍速已设为 ${rate}×。`); return;
    }
    if (!input || normalized === "start" || input === "开始") {
      appendLearning(input ? "> start" : "> Enter"); appendLearning(`正在启动刷课（${playbackRate}×）…`); setBusy(true);
      await call("update_settings", {
        course_playback_rate: playbackRate,
        course_auto_dismiss_dialog: autoDismiss,
        course_document_scroll_enabled: documentScroll,
        course_quiz_auto_answer: quizAutoAnswer,
        course_quiz_choice_enabled: quizChoiceEnabled,
        course_quiz_judgment_enabled: quizJudgmentEnabled,
        course_quiz_blank_enabled: quizBlankEnabled,
      });
      const result = await call("start_course_helper"); setBusy(false);
      if (result.ok) {
        setHelperRunning(true);
        if (quizAutoAnswer) {
          setQuizAutoAnswer(false);
          appendLearning("刷课已启动；本次已启用自动答题，下次启动需重新开启。");
        } else appendLearning("刷课已启动。");
      }
      else appendLearning(result.error || "未找到已打开的课件学习页，请先输入 open。");
      return;
    }
    appendLearning(`> ${input}`);
    appendLearning("可用命令：start、stop、open、speed 8、clear。");
  }

  async function run() {
    if (busy) return;
    const input = command.trim();
    if (phase === "courses") {
      const course = matchingCourses[courseCursor];
      if (course) await chooseSignCourse(course); else append("没有匹配的课程，请修改搜索内容。");
      return;
    }
    setCommand(""); setBusy(true);
    if (input.toLowerCase() === "clear") { setSignEvents([]); setBusy(false); return; }
    if (input) append(`> ${input}`);
    if (input.toLowerCase() === "kill") {
      const result = await call("shutdown_app"); if (result.ok) append("正在关闭本地服务与前端进程…"); else append(`退出失败：${result.error || "未知错误"}`);
      setBusy(false); return;
    }
    if (phase === "login") {
      append("正在自动检测浏览器登录状态，请稍候。");
    } else if (phase === "ready") {
      const result = await call("load_saved_courses");
      if (result.ok && result.courses?.length) { setCourses(result.courses); setPhase("courses"); }
      else {
        append("登录缓存不可用，正在自动打开优学院登录页…"); setPhase("login");
        const opened = await call("start_browser", loginPayload);
        append(opened.ok ? "请在浏览器中完成登录，程序将自动继续。" : `启动浏览器失败：${opened.error || "未知错误"}`);
      }
    } else if (phase === "selected") {
      if (input === "/") { await call("clear_selected_course"); setSelectedSignCourse(null); setPhase("courses"); append("已取消选定。"); }
      else if (!input) { const result = await call("start_monitor"); if (result.ok) setPhase("monitoring"); }
    } else if (phase === "monitoring") {
      if (input === "/" || input.toLowerCase() === "stop") { await call("stop_monitor"); setPhase("selected"); append("已停止轮询。 "); }
      else append("正在轮询。输入 / 或 stop 可以停止。 ");
    }
    setBusy(false);
  }
  function handleSignKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (phase === "courses") {
      if (event.key === "ArrowDown") { event.preventDefault(); setCourseCursor(index => Math.min(index + 1, Math.max(0, matchingCourses.length - 1))); return; }
      if (event.key === "ArrowUp") { event.preventDefault(); setCourseCursor(index => Math.max(0, index - 1)); return; }
      if (event.key === "Escape") { event.preventDefault(); setCommand(""); return; }
    }
    if (event.key === "Enter") void run();
  }
  function openAiWorkspace() {
    const opened = window.open("/ai.html", "dgut-ai-workspace");
    if (!opened) { save("浏览器拦截了 AI 工作台窗口，请允许本站打开新窗口"); return; }
    try { opened.opener = null; opened.focus(); } catch { /* 页面仍已由浏览器打开。 */ }
  }
  const navItems: Page[] = ["terminal", "learning", "settings", "about"];
  const labels: Record<Page, string> = { terminal: "课程签到", learning: "刷课", settings: "设置", about: "关于" };
  const displayedSignEvents = signEvents.map(signEventView);
  const signCourseName = signStatus.courseName || selectedSignCourse?.name || "尚未选择课程";
  const lastCheckMs = signStatus.lastCheck ? new Date(signStatus.lastCheck).getTime() : 0;
  const nextCheckSeconds = signStatus.running && lastCheckMs
    ? Math.max(0, signStatus.intervalSeconds - Math.floor((Date.now() - lastCheckMs) / 1000))
    : 0;
  const displayedCourseEvents = visibleCourseEvents(courseEvents, true);
  const learningActive = helperRunning || ["opening", "waiting_page", "validating", "connected", "starting", "started"].includes(courseScan.openPhase);
  const handleLearningScroll = () => {
    const target = learningLogRef.current;
    if (target) learningAutoScrollRef.current = target.scrollHeight - target.scrollTop - target.clientHeight < 36;
  };

  const updateToast = updateStatus ? toastFor(updateStatus) : null;
  const showHeaderUpdate = page === "terminal" || page === "learning";
  const updateSettingLabel = updateStatus?.state === "checking"
    ? stateLabel("checking")
    : updateStatus?.error
      ? updateStatus.error
      : updateStatus
        ? stateLabel(updateStatus.state)
        : "正在读取更新状态…";
  return <div className="app-shell"><main className="content">
    <button className="settings" aria-label={sidebarOpen ? "收起侧边面板" : "打开侧边面板"} title={sidebarOpen ? "收起侧边面板" : "打开侧边面板"} onClick={() => setSidebarOpen(open => !open)} />
    {saved && <div className="toast">✓ {saved}</div>}
    <UpdateToast view={updateToast} onOpen={toggleDrawer} />
    <UpdateDrawer status={updateStatus} open={drawerOpen && showHeaderUpdate} scroll={drawerScroll} actions={{
      onClose: () => setDrawerOpen(false),
      onInstall: () => void installUpdate(),
      onPostpone: () => setDrawerOpen(false),
      onRetryDownload: () => void retryDownload(),
    }} />
    {updateStatus?.pendingFailureDialog && <UpdateFailureDialog
      dialog={updateStatus.pendingFailureDialog}
      onViewLog={() => void ackFailure("log")}
      onLater={() => void ackFailure("later")}
      onRedownload={() => void ackFailure("redownload")}
    />}
    <section className={`workspace ${sidebarOpen ? "sidebar-open" : ""}`}>
      <AnimatePresence initial={false}>
        {sidebarOpen && <motion.aside
          key="module-sidebar"
          className="settings-nav module-nav"
          initial={reduceMotion ? false : { width: 0, x: -18, opacity: 0 }}
          animate={{ width: 260, x: 0, opacity: 1 }}
          exit={reduceMotion ? { width: 0 } : { width: 0, x: -18, opacity: 0 }}
          transition={reduceMotion ? { duration: 0 } : { duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
        ><div className="module-brand">优学院助手</div><p>模块</p>{navItems.map(item => { const ModuleIcon = moduleIcons[item]; return <button key={item} onClick={() => { setPage(item); if (item === "settings" || item === "about") setDrawerOpen(false); }} className={`module-link ${page === item ? "active" : ""}`}><ModuleIcon className="module-icon" size={19} strokeWidth={2}/><span>{labels[item]}</span></button>; })}<div className="module-divider"/><button type="button" className="ai-workspace-launch" onClick={openAiWorkspace}><b>✦</b><span>AI 工作台</span><i>↗</i></button></motion.aside>}
      </AnimatePresence>
      <div className="workspace-content">
      {showHeaderUpdate && <UpdateBell status={updateStatus} open={drawerOpen} onToggle={toggleDrawer} />}
      {page === "terminal" && <section className={`terminal sign-terminal ${phase === "courses" ? "course-picking" : ""}`}>
        <div className="sign-event-log" ref={signLogRef} aria-label="签到关键事件">
          <div className="sign-day-label">今天</div>
          {displayedSignEvents.length === 0 && <div className="event-empty">暂无关键事件</div>}
          {displayedSignEvents.map(event => <article className={`sign-event ${event.tone}`} key={event.id}>
            <time>{formatClock(event.time)}</time><span className="sign-event-mark" aria-hidden="true">{event.symbol}</span>
            <div><strong>{event.title}</strong>{event.detail && <small>{event.detail}</small>}</div>
          </article>)}
          {signStatus.running && <article className="sign-event live" aria-label="持续监测中">
            <time>{formatClock(signStatus.lastCheck)}</time><span className="sign-event-mark" aria-hidden="true"><i /></span>
            <div><strong>{signStatus.round ? `持续监测中 · 第 ${signStatus.round} 轮已完成` : "正在进行首次检查"}</strong><small>{signStatus.round ? `${signStatus.lastResult} · ${nextCheckSeconds} 秒后再次检查` : "正在读取课堂和签到活动"}</small></div>
          </article>}
        </div>
        <div className="command"><b>›</b><input ref={commandRef} aria-label="课程签到命令" autoFocus disabled={busy || phase === "login"} value={command} onChange={event => setCommand(event.target.value)} onKeyDown={handleSignKeyDown} placeholder={busy ? "正在自动读取登录缓存…" : phase === "login" ? "等待浏览器登录，完成后自动继续…" : phase === "courses" ? "搜索课程，↑↓ 选择，Enter 确认…" : phase === "selected" ? "按 Enter 开始监测，输入 / 重新选课…" : phase === "monitoring" ? "正在监测；输入 / 停止…" : "正在准备签到模块…"}/></div>
        {phase === "courses" && <motion.div className="course-quick-pick" ref={coursePickerRef} initial={reduceMotion ? false : { opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }}><div className="course-picker-head"><strong>选择签到课程</strong><span>{matchingCourses.length} / {courseChoices.length} 门</span></div><div className="course-options" role="listbox" aria-label="课程列表">{matchingCourses.length ? matchingCourses.map((course, index) => <button key={course.id} type="button" role="option" aria-selected={index === courseCursor} className={`course-option ${index === courseCursor ? "active" : ""}`} onMouseEnter={() => setCourseCursor(index)} onClick={() => void chooseSignCourse(course)}><span><strong>{course.name}</strong><small>{course.teacherName || "未知教师"}</small></span><code>ID {course.id}</code></button>) : <div className="course-empty">没有匹配课程，请换个关键词。</div>}</div><div className="course-picker-help">↑↓ 移动　Enter 选择　Esc 清空搜索</div></motion.div>}
        <div className="sign-overview" aria-label="签到监测状态">
          <div className="sign-overview-main">
            <span className={`sign-radar ${signStatus.running ? "active" : ""}`} aria-hidden="true">◎</span>
            <div><div className="sign-course-line"><strong>{signCourseName}</strong><span className={`sign-state ${signStatus.running ? "running" : ""}`}><i />{signStatus.running ? "监测中" : phase === "selected" ? "等待启动" : phase === "courses" ? "等待选择" : "未运行"}</span></div><p>{signStatus.running ? signStatus.lastResult : phase === "selected" ? "按 Enter 开始监测" : "选择课程后即可开始监测签到"}</p></div>
          </div>
          <div className="sign-stats">
            <StatusItem label="最近检查" value={formatClock(signStatus.lastCheck)} />
            <StatusItem label="下次检查" value={signStatus.running ? `${nextCheckSeconds} 秒` : "--"} />
            <StatusItem label="已检查" value={`${signStatus.round} 次`} />
          </div>
        </div>
      </section>}
      {page === "learning" && <section className="terminal learning-terminal">
        <div className="learning-flow" aria-label="未完成课件">
          <div className="lesson-scan-head"><div className="lesson-scan-summary"><strong>未完成课件</strong><span>
            {courseScan.state === "scanning" ? `正在扫描 ${courseScan.scannedCourses} / ${courseScan.totalCourses} 门课程，已发现 ${courseScan.unfinishedCount} 个未完成课件` :
              courseScan.state === "auth_required" ? "等待登录" : `共 ${courseScan.unfinishedCount} 个${courseScan.fromCache ? " · 已显示 5 分钟内缓存" : ""}`}
          </span></div><div className="lesson-scan-actions">
            {courseScan.state === "scanning" ? <button className="secondary compact" onClick={() => void cancelCourseScan()}>取消</button> : <button className="secondary compact" onClick={() => void refreshCourseScan(true)}>重新扫描</button>}
            <button className="secondary compact learning-exit-button" disabled={!learningActive || exitingLearning} onClick={() => void exitCourseLearning()}>{exitingLearning ? "退出中…" : "退出刷课"}</button>
          </div></div>
          {courseScan.state === "auth_required" ? <div className="lesson-scan-message warning">登录状态不可用。请在程序浏览器中完成登录，再点击“重新扫描”。</div> : <>
            <div className="learning-search-line"><i aria-hidden="true">›</i><input className="lesson-search" aria-label="搜索未完成课件" value={lessonQuery} onChange={event => setLessonQuery(event.target.value)} onKeyDown={handleLessonKeyDown} placeholder="搜索课程；↑↓ 选择；Enter 确认…" /></div>
            {courseScan.openPhase !== "idle" && <div className={`lesson-open-phase ${courseScan.openError ? "warning" : ""}`} role="status"><strong>{openPhaseLabel(courseScan.openPhase)}</strong>{courseScan.openError && <span>{courseScan.openError}</span>}{["failed", "timeout"].includes(courseScan.openPhase) && selectedLesson && <button className="secondary compact" onClick={() => void openScannedLesson(selectedLesson)}>重试打开</button>}</div>}
            <div className="lesson-groups" role="listbox" aria-label="未完成课件列表">
              {courseScan.state === "scanning" && !courseScan.groups.length && <div className="lesson-scan-message">正在通过学校页面读取课程目录…</div>}
              {courseScan.state !== "scanning" && !filteredLessonGroups.length && <div className="lesson-scan-message">{courseScan.state === "error" ? (courseScan.error || "课程目录读取失败") : courseScan.unfinishedCount === 0 ? "没有发现未完成课件，或当前课程响应尚未提供可解析目录。" : "没有匹配的课件。"}</div>}
              {filteredLessonGroups.map(group => <section className="lesson-group" key={group.courseId}>
                <h3><strong>{group.courseName}</strong><span>{group.items.length} 个未完成章节</span></h3>
                <div className="lesson-children">{group.items.map(item => {
                  const index = filteredLessons.findIndex(candidate => candidate.id === item.id);
                  const chapterPath = item.chapterPath.join(" / ");
                  const runningHere = helperRunning && courseScan.selectedId === item.id;
                  return <div className={`lesson-entry ${runningHere ? "running" : ""}`} key={item.id}>
                    <button type="button" role="option" aria-selected={index === lessonCursor} className={`lesson-row ${index === lessonCursor ? "active" : ""}`} onMouseEnter={() => setLessonCursor(index)} onClick={() => void openScannedLesson(item)} disabled={!item.canAutoOpen || learningActive}>
                      <span><strong>{item.title}</strong>{chapterPath && chapterPath !== item.title && <small>{chapterPath}</small>}</span><em>{item.type}</em><b>{lessonProgress(item)}</b><i>{item.canAutoOpen ? "打开并启动" : item.unavailableReason}</i>
                    </button>
                    {runningHere && <section className="lesson-inline-record" aria-label={`${item.title}运行记录`}>
                      <header><i aria-hidden="true"/><strong>运行记录</strong><span>实时更新</span></header>
                      <div className="course-event-log" ref={learningLogRef} onScroll={handleLearningScroll}>
                        {displayedCourseEvents.length === 0 && learningLogs.length === 0 && <div className="event-empty">正在等待运行事件…</div>}
                        {displayedCourseEvents.map(event => <div className={`course-event ${event.level}`} key={event.seq}><time>{formatClock(event.time)}</time><span>{event.message}</span><code>{event.code}</code></div>)}
                        {learningLogs.map((line, logIndex) => <div className="course-local-log" key={`${logIndex}-${line}`}>{line}</div>)}
                        <span ref={learningEndRef}/>
                      </div>
                    </section>}
                  </div>;
                })}</div>
              </section>)}
            </div>
            {courseScan.failures.length > 0 && <details className="lesson-failures"><summary>{courseScan.failures.length} 门课程扫描失败</summary>{courseScan.failures.map(item => <p key={`${item.courseId}-${item.courseName}`}>{item.courseName}：{item.reason}</p>)}</details>}
          </>}
        </div>
      </section>}
      {page !== "terminal" && page !== "learning" && <div className={`settings-body ${page === "about" ? "about-settings-body" : ""}`}>
      {page === "settings" && <SettingsSection className="utility-settings" title="设置">
        <div className="settings-surface">
        <Card title="启动浏览器"><div className="browser-scan-line"><button type="button" className={`refresh-button ${detectingBrowsers ? "spinning" : ""}`} aria-label={detectingBrowsers ? "正在重新检测浏览器" : "重新检测浏览器"} title={detectingBrowsers ? "检测中…" : "重新检测"} disabled={detectingBrowsers} onClick={detectInstalledBrowsers}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6v5h-5"/><path d="M18.4 15a7 7 0 1 1 .1-6.1L20 11"/></svg></button></div><div className="browser-list">{detectedBrowsers.map(option => { const selected = browser !== "自定义浏览器" && samePath(path, option.path); return <button type="button" key={option.path} className={`browser-option ${selected ? "selected" : ""}`} onClick={() => chooseDetectedBrowser(option)}><i className="radio-dot"/><strong>{option.name}</strong></button>; })}<button type="button" className={`browser-option ${browser === "自定义浏览器" ? "selected" : ""}`} onClick={chooseCustomBrowser}><i className="radio-dot"/><strong>自定义路径</strong></button></div>{browser === "自定义浏览器" && <label className="custom-browser-path"><span>程序路径</span><input className="field" value={path} onChange={event => setPath(event.target.value)}/></label>}</Card>
        <Card title="校园网"><div className="setting-line"><span>开机时打开校园网登录页</span><button type="button" aria-label="开机时打开校园网登录页" onClick={() => setCampusLoginOnStartup(!campusLoginOnStartup)} className={`switch ${campusLoginOnStartup ? "on" : ""}`}><i /></button></div></Card>
        <Card title="账号登录恢复"><div className="setting-line"><span>启用账号密码自动重新登录</span><button type="button" aria-label="启用账号密码自动重新登录" onClick={() => setAccountEnabled(!accountEnabled)} className={`switch ${accountEnabled ? "on" : ""}`}><i /></button></div>{accountEnabled && <div className="account-fields"><input className="field" value={accountName} onChange={event => setAccountName(event.target.value)} placeholder="学号" autoComplete="username" disabled/><input className="field" type="password" value={accountPassword} onChange={event => setAccountPassword(event.target.value)} placeholder={hasSavedPassword ? "密码已保存；留空则不修改" : "密码"} autoComplete="current-password" disabled/></div>}</Card>
        <Card title="刷课"><label className="setting-field"><span>视频倍速</span><input className="field rate-field" type="number" min="1" max="16" step="0.5" value={playbackRate} onChange={event => setPlaybackRate(Math.min(16, Math.max(1, Number(event.target.value) || 1)))}/></label><div className="setting-line"><span>自动答题</span><button type="button" aria-label="自动答题" onClick={toggleQuizAutoAnswer} className={`switch ${quizAutoAnswer ? "on" : ""}`}><i /></button></div>{quizAutoAnswer && <div className="quiz-answer-options"><div className="setting-line"><span>选择题</span><button type="button" aria-label="自动回答选择题" onClick={toggleQuizChoice} className={`switch ${quizChoiceEnabled ? "on" : ""}`}><i /></button></div><div className="setting-line"><span>判断题</span><button type="button" aria-label="自动回答判断题" onClick={toggleQuizJudgment} className={`switch ${quizJudgmentEnabled ? "on" : ""}`}><i /></button></div><div className="setting-line"><span>填空题</span><button type="button" aria-label="自动回答填空题" onClick={toggleQuizBlank} className={`switch ${quizBlankEnabled ? "on" : ""}`}><i /></button></div></div>}</Card>
        <Card title="日志与数据"><div className="setting-line"><span>保存签到与错误详情</span><button type="button" aria-label="保存签到与错误详情" onClick={() => setLogging(!logging)} className={`switch ${logging ? "on" : ""}`}><i /></button></div><div className="log-path-row"><input className="field" value={logPath} onChange={event => setLogPath(event.target.value)}/><button className="secondary" disabled={busy} onClick={openLog}>打开日志</button></div></Card>
        <Card title="软件更新"><div className="setting-line update-setting-line"><span><strong>当前版本 v{appInfo?.version || updateStatus?.currentVersion || "…"}</strong><small>{updateSettingLabel}</small><small>请开启VPN或加速器再检查更新</small></span><button type="button" className="secondary" disabled={updateStatus?.state === "checking"} onClick={() => void checkUpdate()}>{updateStatus?.state === "checking" ? "检查中…" : "检查更新"}</button></div></Card>
        </div>
        <div className="actions settings-save"><button className="primary" disabled={busy} onClick={saveAllSettings}>{busy ? "保存中…" : "保存全部设置"}</button></div>
      </SettingsSection>}
      {page === "about" && <AboutGuide version={appInfo?.version || ""} repo={appInfo?.repo || ""} />}
      </div>}
      </div>
    </section>
  </main></div>;
}

function SettingsSection({ title, subtitle, children, className = "" }: { title: string; subtitle?: string; children: React.ReactNode; className?: string }) { return <section className={`settings-page ${className}`}><div className="page-intro"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div></div>{children}</section>; }
function Card({ title, desc, children }: { title: string; desc?: string; children: React.ReactNode }) { return <article className="card"><h3>{title}</h3>{desc && <p>{desc}</p>}<div className="card-content">{children}</div></article>; }
function StatusItem({ label, value, tone = "" }: { label: string; value: string; tone?: string }) { return <div className={`status-item ${tone}`}><span>{label}</span><strong title={value}>{value}</strong></div>; }

export default App;
