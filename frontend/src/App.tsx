import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { BookOpenCheck, CircleQuestionMark, ClipboardCheck, PenLine, ScrollText, Settings, type LucideIcon } from "lucide-react";
import {
  formatClock, mergeEvents, visibleCourseEvents,
  type CourseEvent, type CourseStatus,
} from "./courseObservability";
import { type UpdateStatus } from "./updateClient";
import { UpdateSettings } from "./UpdateSettings";
import { UpdateDialog, useUpdateDialog } from "./UpdateDialog";
import { AboutGuide } from "./AboutGuide";
import { SignUnavailablePage } from "./SignUnavailablePage";
import { HomeworkInputPanel } from "./HomeworkInputPanel";
import { LogPanel } from "./LogPanel";
import { SettingsPanel } from "./SettingsPanel";
import { SettingsAutosave, type AppConfig } from "./settingsAutosave";
import { CoursewareExportPanel } from "./CoursewareExportPanel";
import { addSignEvent, signEventView, type SignEvent } from "./signObservability";
import { filterSignChoices, firstSignChoiceCursor, SelectedSignChoice, SignChoicePicker, stepSignChoiceCursor, type SignChoice } from "./SignChoicePicker";
import { filterLessonGroups, lessonProgress, openPhaseLabel, type CourseScanStatus, type LessonItem } from "./courseScanView";
import "./courseScan.css";
import "./homeworkInput.css";

type Page = "terminal" | "learning" | "homework" | "logs" | "settings" | "about";
type Phase = "ready" | "login" | "courses" | "classrooms" | "selected" | "monitoring";
type LoginStatus = { authenticated: boolean; displayName: string; accountName: string; userId: number | null; greeting: string };
type Course = { id: number; name: string; teacherName: string };
type Classroom = { id: number; title: string; beginTime: string; statusLabel: string; ended?: boolean };
type AppInfo = { appName: string; version: string; repo: string };
type SignMonitorStatus = { running: boolean; state: string; courseName: string; intervalSeconds: number; round: number; startedAt: string; lastCheck: string; lastResult: string; classroomId?: number | null; classroomName?: string; classrooms?: Classroom[]; classroomSelectionRequired?: boolean; activityMonitoring?: boolean; recoveryPending?: boolean; membershipPhase?: string; recoveryCourseName?: string; recoveryError?: string };
type BackendResult = { ok: boolean; error?: string; courses?: Course[]; course?: Course | null; config?: AppConfig; quizAutoAnswerArmed?: boolean; login?: LoginStatus; events?: CourseEvent[]; latestSeq?: number; status?: CourseStatus; signStatus?: SignMonitorStatus; scan?: CourseScanStatus; info?: AppInfo; update?: UpdateStatus };

const loginPayload = { url: "https://lms.dgut.edu.cn" };
const moduleIcons: Record<Page, LucideIcon> = {
  terminal: ClipboardCheck,
  learning: BookOpenCheck,
  homework: PenLine,
  logs: ScrollText,
  settings: Settings,
  about: CircleQuestionMark,
};

/** 课堂列表项的唯一构造点：列表、搜索与回执卡片都读这份结果，状态判断只做一次。 */
function classroomChoices(rooms?: Classroom[]): SignChoice[] {
  return (rooms || []).map(room => ({
    id: room.id,
    title: room.title,
    detail: `${room.statusLabel} · ${room.beginTime ? new Date(room.beginTime).toLocaleString("zh-CN", { hour12: false }) : "开始时间未知"}`,
    disabled: room.ended ?? room.statusLabel === "已结束",
  }));
}

function App() {
  const [page, setPage] = useState<Page>("terminal");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [courseSection, setCourseSection] = useState<"learning" | "export">("learning");
  const [exportVisited, setExportVisited] = useState(false);
  const reduceMotion = useReducedMotion();
  const [phase, setPhase] = useState<Phase>("ready");
  const [loginStatus, setLoginStatus] = useState<LoginStatus | null>(null);
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
  const [config, setConfig] = useState<AppConfig>({});
  const playbackRate = config.course_playback_rate ?? 8;
  const [settingsWriter] = useState(() => new SettingsAutosave(call));
  const [helperRunning, setHelperRunning] = useState(false);
  const [courseScan, setCourseScan] = useState<CourseScanStatus>({ state: "idle", loggedIn: false, scannedCourses: 0, totalCourses: 0, unfinishedCount: 0, groups: [], failures: [], cachedAt: "", fromCache: false, error: "", openPhase: "idle", openError: "", selectedId: "" });
  const [lessonQuery, setLessonQuery] = useState("");
  const [lessonCursorId, setLessonCursorId] = useState("");
  const [exitingLearning, setExitingLearning] = useState(false);
  const [notice, setNotice] = useState("");
  const [updateStatus, setUpdateStatus] = useState<UpdateStatus | null>(null);
  const [appInfo, setAppInfo] = useState<AppInfo | null>(null);
  const handoffSentRef = useRef(false);
  const signInitializationStartedRef = useRef(false);
  const greetedLoginRef = useRef("");
  const learningScanStartedRef = useRef(false);
  const updateDialog = useUpdateDialog(updateStatus);
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
  const acceptLoginStatus = (login?: LoginStatus) => {
    if (!login) return;
    setLoginStatus(login);
    const identity = login.displayName || login.accountName || String(login.userId || "");
    if (login.authenticated && login.greeting && greetedLoginRef.current !== identity) {
      greetedLoginRef.current = identity;
      append(login.greeting, "success");
    }
  };
  const courseChoices = [...new Map<number, Course>(courses.map(course => [course.id, course])).values()];
  const coursePickChoices = courseChoices.map(course => ({ id: course.id, title: course.name, detail: course.teacherName || "未知教师" }));
  const matchingCourses = filterSignChoices(coursePickChoices, command);
  const classroomPickChoices = classroomChoices(signStatus.classrooms);
  const matchingClassrooms = filterSignChoices(classroomPickChoices, command);
  const selectedClassroom = signStatus.classroomId == null ? null
    : classroomPickChoices.find(room => room.id === signStatus.classroomId)
      ?? { id: signStatus.classroomId, title: signStatus.classroomName || "已选择课堂", detail: "课堂详情暂不可用" };
  const filteredLessonGroups = filterLessonGroups(courseScan.groups, lessonQuery);
  const filteredLessons = filteredLessonGroups.flatMap(group => group.items);
  const lessonCursor = Math.max(0, filteredLessons.findIndex(item => item.id === lessonCursorId));
  const selectedLesson = courseScan.groups.flatMap(group => group.items).find(item => item.id === courseScan.selectedId) || filteredLessons[lessonCursor];

  useEffect(() => { const log = signLogRef.current; log?.scrollTo({ top: log.scrollHeight, behavior: "smooth" }); }, [signEvents]);
  useEffect(() => {
    const target = learningLogRef.current;
    if (target && learningAutoScrollRef.current) target.scrollTo({ top: target.scrollHeight, behavior: "smooth" });
  }, [learningLogs, courseEvents]);
  useEffect(() => {
    if (phase === "courses") setCourseCursor(firstSignChoiceCursor(matchingCourses));
    if (phase === "classrooms") setCourseCursor(firstSignChoiceCursor(matchingClassrooms));
  }, [command, courses, phase]);
  useEffect(() => { coursePickerRef.current?.querySelector(".course-option.active")?.scrollIntoView({ block: "nearest" }); }, [courseCursor]);
  useEffect(() => { setLessonCursorId(""); }, [lessonQuery]);
  useEffect(() => {
    if (busy) return;
    const target = page === "terminal" ? commandRef : page === "learning" && courseSection === "learning" ? learningCommandRef : null;
    if (!target) return;
    const frame = requestAnimationFrame(() => target.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [page, courseSection, busy]);
  useEffect(() => {
    void call("get_app_info").then(result => { if (result.ok && result.info) setAppInfo(result.info); });
    void call("get_settings").then(result => {
      if (result.ok && result.config) {
        // 初次读取可能晚于用户的自动保存；已经保存的新值优先。
        setConfig(current => ({ ...result.config, ...current }));
        append("本地后端已连接，正在准备读取浏览器当前登录状态…");
        if (!signInitializationStartedRef.current) {
          signInitializationStartedRef.current = true;
          void initializeSignIn();
        }
      }
      else append(`后端连接失败：${result.error || "未知错误"}`);
    });
    void call("get_login_status").then(result => { if (result.ok) acceptLoginStatus(result.login); });
  }, []);
  useEffect(() => {
    if (page !== "learning" || learningScanStartedRef.current) return;
    learningScanStartedRef.current = true;
    void call("start_course_scan").then(result => { if (result.ok && result.scan) setCourseScan(result.scan); });
  }, [page]);
  useEffect(() => {
    if (page === "learning" && courseScan.state === "auth_required" && !courseScan.stopping && courses.length > 0) {
      void refreshCourseScan(false);
    }
  }, [page, phase, courseScan.state, courseScan.stopping, courses.length]);
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
      if (disposed) return;
      acceptLoginStatus(result.login);
      if (!result.ok || !result.courses?.length) return;
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
        const learning = events.filter(event => (event.sessionId.startsWith("course-") || event.category !== "general") && event.category !== "command");
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
        setPhase(current => result.signStatus?.running ? "monitoring" : current === "monitoring"
          ? result.signStatus?.classroomSelectionRequired ? "classrooms" : "selected" : current);
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
  useEffect(() => { if (page === "settings") void call("mark_update_read"); }, [page, updateStatus?.messages[0]?.id]);
  // 移交确认后由后端先接收关机请求，再通过 CDP 精确关闭助手标签页。
  useEffect(() => {
    if (!updateStatus?.readyForExit || handoffSentRef.current) return;
    handoffSentRef.current = true;
    append("正在移交给更新器……");
    window.setTimeout(() => {
      void call("shutdown_for_update");
    }, 700);
  }, [updateStatus?.readyForExit]);

  async function call(commandName: string, payload: Record<string, unknown> = {}): Promise<BackendResult> {
    try {
      const response = await fetch("/api/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ command: commandName, payload }) });
      return await response.json() as BackendResult;
    } catch (error) { return { ok: false, error: `后端连接失败：${String(error)}` }; }
  }
  function loadConfig(patch: AppConfig) { setConfig(current => ({ ...current, ...patch })); }
  async function checkUpdate() {
    const result = await call("check_update");
    if (!result.ok) setUpdateStatus(current => current && { ...current, error: result.error || "无法检查更新" });
  }
  async function installUpdate() {
    const result = await call("install_update");
    if (!result.ok) { setUpdateStatus(current => current && { ...current, error: result.error || "无法开始安装" }); setPage("settings"); }
  }
  async function retryDownload() {
    const result = await call("download_update");
    if (!result.ok) { setUpdateStatus(current => current && { ...current, error: result.error || "无法开始下载" }); setPage("settings"); }
  }
  async function chooseSignCourse(course: Course) {
    if (busy) return;
    setBusy(true); const result = await call("select_course", { query: String(course.id) }); setBusy(false);
    if (result.ok && result.course) {
      setSelectedSignCourse(result.course); setCommand(""); append(`已选定：${result.course.name} · ${result.course.teacherName}`);
      await loadSignClassrooms();
    } else append(result.error || "选择课程失败，请重新选择。", "warning");
  }
  async function loadSignClassrooms() {
    setBusy(true); setPhase("classrooms"); setCommand(""); setCourseCursor(0);
    const result = await call("get_sign_classrooms");
    setBusy(false);
    if (!result.ok || !result.signStatus) {
      setSignStatus(current => ({ ...current, classroomId: null, classroomName: "", classrooms: [], classroomSelectionRequired: false }));
      append(result.error || "课堂读取失败，请刷新重试。", "warning");
      return;
    }
    setSignStatus(result.signStatus);
    const choices = classroomChoices(result.signStatus.classrooms);
    setCourseCursor(firstSignChoiceCursor(choices));
    if (result.signStatus.classroomId != null && result.signStatus.classrooms?.length === 1) {
      setPhase("selected"); append(`已选择课堂：${result.signStatus.classroomName}`);
    } else if (!choices.length) append("该课程暂无课堂，可稍后刷新。", "warning");
    else if (choices.every(item => item.disabled)) append("该课程暂无进行中的课堂，开课后点“刷新课堂”。", "warning");
    else append("请选择需要监测的课堂。");
  }
  async function chooseSignClassroom(id: number) {
    if (busy) return;
    const target = classroomChoices(signStatus.classrooms).find(item => item.id === id);
    if (target?.disabled) { append("该课堂已结束，请选择进行中的课堂。", "warning"); return; }
    setBusy(true);
    const result = await call("select_sign_classroom", { classroomId: id });
    setBusy(false);
    if (result.ok && result.signStatus) {
      setSignStatus(result.signStatus); setCommand(""); setPhase("selected");
      append(`已选择课堂：${result.signStatus.classroomName}`);
    } else append(result.error || "选择课堂失败，请刷新重试。", "warning");
  }
  async function initializeSignIn() {
    setBusy(true);
    append("正在恢复登录状态…");
    const restored = await call("load_session_and_courses", { automatic: true, waitSeconds: 1 });
    acceptLoginStatus(restored.login);
    if (restored.ok && restored.courses?.length) {
      setCourses(restored.courses); setPhase("courses"); setBusy(false);
      append("登录状态已恢复，请选择课程。");
      return;
    }
    setPhase("login");
    const opened = await call("start_browser", loginPayload);
    append(opened.ok ? "学校页面已准备好。如未登录，请在浏览器中完成登录，程序将自动继续。" : `启动浏览器失败：${opened.error || "未知错误"}`);
    setBusy(false);
  }
  async function refreshCourseScan(force = true) {
    const result = await call("start_course_scan", { force });
    if (result.ok && result.scan) setCourseScan(result.scan);
  }
  async function refreshExportCourses(): Promise<string> {
    const result = await call("load_session_and_courses", { waitSeconds: 1 });
    acceptLoginStatus(result.login);
    if (result.ok && result.courses) { setCourses(result.courses); return ""; }
    return result.error || "读取课程失败，请先在程序浏览器中完成学校登录。";
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
    setLessonCursorId(item.id);
    const result = await call("open_scanned_course", { itemId: item.id });
    if (!result.ok) appendLearning(result.error || "自动打开课件失败。");
    else if (result.scan) setCourseScan(result.scan);
  }
  function handleLessonKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") { event.preventDefault(); setLessonCursorId(filteredLessons[Math.min(lessonCursor + 1, Math.max(0, filteredLessons.length - 1))]?.id || ""); }
    else if (event.key === "ArrowUp") { event.preventDefault(); setLessonCursorId(filteredLessons[Math.max(0, lessonCursor - 1)]?.id || ""); }
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
      appendLearning(`> speed ${rate}`); setBusy(true);
      const saved = await settingsWriter.save("course_playback_rate", "update_settings", { course_playback_rate: rate });
      if (!saved.ok) { setBusy(false); appendLearning(saved.error || "保存倍速失败"); return; }
      loadConfig(saved.config || { course_playback_rate: rate });
      if (helperRunning) await call("set_course_speed", { rate });
      setBusy(false); appendLearning(`视频倍速已设为 ${rate}×。`); return;
    }
    if (!input || normalized === "start" || input === "开始") {
      appendLearning(input ? "> start" : "> Enter"); appendLearning("正在启动刷课…"); setBusy(true);
      if (!await settingsWriter.flush()) { setBusy(false); appendLearning("设置尚未保存成功，请返回设置页检查。"); return; }
      const result = await call("start_course_helper"); setBusy(false);
      if (result.ok) {
        setHelperRunning(true);
        if (result.quizAutoAnswerArmed) {
          appendLearning("刷课已启动；自动答题已启用。");
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
      const course = courseChoices.find(item => item.id === matchingCourses[courseCursor]?.id);
      if (course) await chooseSignCourse(course); else append("没有匹配的课程，请修改搜索内容。");
      return;
    }
    if (phase === "classrooms") {
      if (input === "/") {
        await call("clear_selected_course"); setSelectedSignCourse(null); setCommand(""); setPhase("courses");
      } else if (matchingClassrooms[courseCursor]) await chooseSignClassroom(matchingClassrooms[courseCursor].id);
      else append("暂无匹配课堂，请刷新或修改搜索内容。");
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
      await initializeSignIn();
    } else if (phase === "selected") {
      if (input === "/") { await call("clear_selected_course"); setSelectedSignCourse(null); setPhase("courses"); append("已取消选定。"); }
      else if (!input) { const result = await call("start_monitor"); if (result.ok) setPhase("monitoring"); else append(result.error || "启动失败", "warning"); }
    } else if (phase === "monitoring") {
      if (input === "/" || input.toLowerCase() === "stop") {
        await call("stop_monitor");
        if (signStatus.recoveryPending) append("已请求停止，正在先恢复原班级；恢复记录会保留到核对成功。");
        else { setPhase("selected"); append("已停止轮询。 "); }
      }
      else append("正在轮询。输入 / 或 stop 可以停止。 ");
    }
    setBusy(false);
  }
  function handleSignKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (phase === "courses" || phase === "classrooms") {
      const items = phase === "courses" ? matchingCourses : matchingClassrooms;
      if (event.key === "ArrowDown") { event.preventDefault(); setCourseCursor(index => stepSignChoiceCursor(items, index, 1)); return; }
      if (event.key === "ArrowUp") { event.preventDefault(); setCourseCursor(index => stepSignChoiceCursor(items, index, -1)); return; }
      if (event.key === "Escape") { event.preventDefault(); setCommand(""); return; }
    }
    if (event.key === "Enter") void run();
  }
  function openAiWorkspace() {
    const opened = window.open("/ai.html", "dgut-ai-workspace");
    if (!opened) { setNotice("浏览器拦截了 AI 工作台窗口，请允许本站打开新窗口"); return; }
    try { opened.opener = null; opened.focus(); } catch { /* 页面仍已由浏览器打开。 */ }
  }
  const navItems: Page[] = ["terminal", "learning", "homework", "logs", "settings", "about"];
  const labels: Record<Page, string> = { terminal: "课程签到", learning: "课件", homework: "作业", logs: "运行日志", settings: "设置", about: "关于" };
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

  return <div className="app-shell"><main className="content">
    {notice && <div className="toast" role="alert" onClick={() => setNotice("")}>{notice}</div>}
    <button className="settings" aria-label={sidebarOpen ? "收起侧边面板" : "打开侧边面板"} title={sidebarOpen ? "收起侧边面板" : "打开侧边面板"} onClick={() => setSidebarOpen(open => !open)} />
    <UpdateDialog open={updateDialog.open} status={updateStatus} onClose={updateDialog.hide} onDownload={retryDownload} onInstall={installUpdate} repo={appInfo?.repo || ""}/>
    <section className={`workspace ${sidebarOpen ? "sidebar-open" : ""}`}>
      <AnimatePresence initial={false}>
        {sidebarOpen && <motion.aside
          key="module-sidebar"
          className="settings-nav module-nav"
          initial={reduceMotion ? false : { width: 0, x: -18, opacity: 0 }}
          animate={{ width: 260, x: 0, opacity: 1 }}
          exit={reduceMotion ? { width: 0 } : { width: 0, x: -18, opacity: 0 }}
          transition={reduceMotion ? { duration: 0 } : { duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
        ><div className="module-brand"><strong>优学院助手</strong><small className={loginStatus?.authenticated ? "online" : ""}><i />{loginStatus?.authenticated ? (loginStatus.greeting || "已登录") : phase === "login" ? "等待登录…" : "未登录"}</small></div>{navItems.map(item => { const ModuleIcon = moduleIcons[item]; return <button key={item} onClick={() => setPage(item)} className={`module-link ${page === item ? "active" : ""}`}><ModuleIcon className="module-icon" size={19} strokeWidth={2}/><span>{labels[item]}</span></button>; })}<div className="module-divider"/><button type="button" className="ai-workspace-launch" onClick={openAiWorkspace}><b>✦</b><span>AI 工作台</span><i>↗</i></button></motion.aside>}
      </AnimatePresence>
      <div className="workspace-content">
      {page === "terminal" && <SignUnavailablePage />}
      {page === "learning" && <section className="course-page" aria-label="课件">
        <div className="course-section-switch" role="group" aria-label="课件功能">
          <button type="button" className={courseSection === "learning" ? "active" : ""} aria-pressed={courseSection === "learning"} onClick={() => setCourseSection("learning")}>刷课</button>
          <button type="button" className={courseSection === "export" ? "active" : ""} aria-pressed={courseSection === "export"} onClick={() => { setExportVisited(true); setCourseSection("export"); }}>课件导出</button>
        </div>
        <section className="terminal learning-terminal" hidden={courseSection !== "learning"}>
        <div className="learning-flow" aria-label="未完成课件">
          <div className="lesson-scan-head"><div className="lesson-scan-summary"><strong>未完成课件</strong><span>
            {courseScan.state === "scanning" ? `扫描中 ${courseScan.scannedCourses} / ${courseScan.totalCourses}` :
              courseScan.state === "auth_required" ? "等待登录" : `共 ${courseScan.unfinishedCount} 个`}
          </span></div><div className="lesson-scan-actions">
            {courseScan.state === "scanning" ? <button className="secondary compact" onClick={() => void cancelCourseScan()}>取消</button> : <button className="secondary compact" disabled={courseScan.stopping} onClick={() => void refreshCourseScan(true)}>{courseScan.stopping ? "正在停止…" : "重新扫描"}</button>}
            {courseScan.state !== "scanning" && courseScan.failures.length > 0 && <button className="secondary compact" disabled={courseScan.stopping} onClick={() => void refreshCourseScan(false)}>重试失败课程</button>}
            <button className="secondary compact learning-exit-button" disabled={!learningActive || exitingLearning} onClick={() => void exitCourseLearning()}>{exitingLearning ? "退出中…" : "退出刷课"}</button>
          </div></div>
          {courseScan.state === "auth_required" ? <div className="lesson-scan-message warning">登录状态不可用。请在程序浏览器中完成登录，再点击“重新扫描”。</div> : <>
            <div className="learning-search-line"><i aria-hidden="true">›</i><input className="lesson-search" aria-label="搜索未完成课件" value={lessonQuery} onChange={event => setLessonQuery(event.target.value)} onKeyDown={handleLessonKeyDown} placeholder="搜索课程或课件" /></div>
            {courseScan.openPhase !== "idle" && <div className={`lesson-open-phase ${courseScan.openError ? "warning" : ""}`} role="status"><strong>{openPhaseLabel(courseScan.openPhase)}</strong>{courseScan.openError && <span>{courseScan.openError}</span>}{["failed", "timeout"].includes(courseScan.openPhase) && selectedLesson && <button className="secondary compact" onClick={() => void openScannedLesson(selectedLesson)}>重试打开</button>}</div>}
            <div className="lesson-groups" role="listbox" aria-label="未完成课件列表">
              {courseScan.state === "scanning" && !courseScan.groups.length && <div className="lesson-scan-message">正在读取课件…</div>}
              {courseScan.state !== "scanning" && !filteredLessonGroups.length && <div className="lesson-scan-message">{courseScan.state === "error" ? (courseScan.error || "课程目录读取失败") : courseScan.unfinishedCount === 0 ? "没有未完成课件。" : "没有匹配的课件。"}</div>}
              {filteredLessonGroups.map(group => <section className="lesson-group" key={group.courseId}>
                <h3><strong>{group.courseName}</strong><span>{group.items.length} 个章节</span></h3>
                <div className="lesson-children">{group.items.map(item => {
                  const index = filteredLessons.findIndex(candidate => candidate.id === item.id);
                  const chapterPath = item.chapterPath.join(" / ");
                  const runningHere = helperRunning && courseScan.selectedId === item.id;
                  return <div className={`lesson-entry ${runningHere ? "running" : ""}`} key={item.id}>
                    <button type="button" role="option" aria-selected={index === lessonCursor} aria-label={item.canAutoOpen ? `${item.title}，${item.type}，${lessonProgress(item)}，打开` : `${item.title}：${item.unavailableReason}`} className={`lesson-row ${index === lessonCursor ? "active" : ""} ${item.canAutoOpen ? "" : "unavailable"}`} onMouseEnter={() => setLessonCursorId(item.id)} onClick={() => void openScannedLesson(item)} disabled={!item.canAutoOpen || learningActive}>
                      <span><strong>{item.title}</strong>{chapterPath && chapterPath !== item.title && <small>{chapterPath}</small>}</span><em>{item.type}</em><b>{lessonProgress(item)}</b>{!item.canAutoOpen && <i>{item.unavailableReason}</i>}
                    </button>
                    {runningHere && <section className="lesson-inline-record" aria-label={`${item.title}运行记录`}>
                      <header><i aria-hidden="true"/><strong>运行记录</strong></header>
                      <div className="course-event-log" ref={learningLogRef} onScroll={handleLearningScroll}>
                        {displayedCourseEvents.length === 0 && learningLogs.length === 0 && <div className="event-empty">正在等待运行事件…</div>}
                        {displayedCourseEvents.map(event => <div className={`course-event ${event.level}`} key={event.seq}><time>{formatClock(event.time)}</time><span>{event.message}</span></div>)}
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
        </section>
        {exportVisited && <div className="settings-body course-export-body" hidden={courseSection !== "export"}><CoursewareExportPanel courses={courseChoices} onRefresh={refreshExportCourses} isVisible={courseSection === "export"} /></div>}
      </section>}
      {page !== "terminal" && page !== "learning" && <div className={`settings-body ${page === "about" ? "about-settings-body" : ""}`}>
      {page === "homework" && <HomeworkInputPanel />}
      {page === "logs" && <LogPanel call={call} />}
      {page === "settings" && <SettingsPanel config={config} call={call} writer={settingsWriter} onSaved={loadConfig} monitoring={signStatus.running || Boolean(signStatus.recoveryPending)}>
        <UpdateSettings status={updateStatus} version={appInfo?.version || ""} onCheck={() => { updateDialog.show(); void checkUpdate(); }} onOpen={updateDialog.show}/>
      </SettingsPanel>}
      {page === "about" && <AboutGuide version={appInfo?.version || ""} repo={appInfo?.repo || ""} />}
      </div>}
      </div>
    </section>
  </main></div>;
}

function StatusItem({ label, value, tone = "" }: { label: string; value: string; tone?: string }) { return <div className={`status-item ${tone}`}><span>{label}</span><strong title={value}>{value}</strong></div>; }

export default App;
