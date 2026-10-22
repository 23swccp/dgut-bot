import { useEffect, useMemo, useRef, useState } from "react";
import {
  BookOpen, CheckCheck, CircleSlash, Clock, FolderDown, Loader2,
  RefreshCw, Search, SearchX, Square, TriangleAlert,
} from "lucide-react";
import {
  classifyCourses, currentSemester, exportStateLabel, filterExportCourses, selectVisible, semesterOptions,
  type ExportCourse, type ExportStatus, type SemesterCourse,
} from "./coursewareView";
import "./coursewareExport.css";

const TERM_STORAGE_KEY = "dgut_bot_cw_last_term";

type CommandResult = { ok: boolean; error?: string; coursewareExport?: ExportStatus };
async function command(name: string, payload: Record<string, unknown> = {}): Promise<CommandResult> {
  try {
    const response = await fetch("/api/command", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ command: name, payload }),
    });
    return await response.json() as CommandResult;
  } catch (error) { return { ok: false, error: String(error) }; }
}

type Group = { id: string; label: string; items: SemesterCourse[] };

function groupBySemester(courses: SemesterCourse[], grouped: boolean): Group[] {
  if (!grouped) return [{ id: "visible", label: "", items: courses }];
  const groups = new Map<string, Group>();
  courses.forEach(course => {
    const group = groups.get(course.semester.id);
    if (group) group.items.push(course);
    else groups.set(course.semester.id, { id: course.semester.id, label: course.semester.label, items: [course] });
  });
  return [...groups.values()].sort((left, right) => right.items[0].semester.order - left.items[0].semester.order);
}

export function CoursewareExportPanel({ courses, onRefresh }: { courses: ExportCourse[]; onRefresh: () => Promise<string> }) {
  const [current] = useState(currentSemester);
  const [term, setTerm] = useState(() => {
    try {
      const saved = localStorage.getItem(TERM_STORAGE_KEY);
      if (saved) return saved;
    } catch { /* ignore */ }
    return currentSemester().id;
  });
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<number[]>([]);
  const [status, setStatus] = useState<ExportStatus | null>(null);
  const [pending, setPending] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [lastClickedIndex, setLastClickedIndex] = useState<number | null>(null);
  const [canScrollDown, setCanScrollDown] = useState(false);

  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const progressRef = useRef<HTMLElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  const classified = classifyCourses(courses, current);
  const visible = filterExportCourses(classified, term, query);
  const chosen = classified.filter(course => selected.includes(course.id));
  const groups = groupBySemester(visible, term === "all");
  const allVisible = visible.length > 0 && visible.every(course => selected.includes(course.id));
  const someVisible = visible.some(course => selected.includes(course.id));
  const visibleSelectedCount = visible.filter(course => selected.includes(course.id)).length;
  const active = status?.state === "running" || status?.state === "cancelling";
  const locked = active || pending;
  const failedCourses = status?.results.filter(item => item.state === "error" || item.failures.length > 0).length || 0;
  const emptyCourses = status?.results.filter(
    item => item.state === "completed" && item.pageTotal === 0 && item.fileWritten === 0,
  ).length || 0;
  const termOptions = semesterOptions(classified, current);
  const progressPercent = status && status.courseTotal > 0
    ? Math.round((status.courseDone / status.courseTotal) * 100) : 0;

  const visibleIndexMap = useMemo(() => new Map(visible.map((course, index) => [course.id, index])), [visible]);

  function handleTermChange(newTerm: string) {
    setTerm(newTerm);
    try {
      localStorage.setItem(TERM_STORAGE_KEY, newTerm);
    } catch { /* ignore */ }
  }

  function checkListScroll() {
    const el = listRef.current;
    if (!el) return;
    const hasMore = el.scrollHeight - el.scrollTop - el.clientHeight > 8;
    setCanScrollDown(hasMore);
  }

  useEffect(() => {
    checkListScroll();
  }, [visible, groups]);

  useEffect(() => {
    setLastClickedIndex(null);
  }, [term, query]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "/" && document.activeElement !== searchInputRef.current) {
        const tagName = (document.activeElement?.tagName || "").toLowerCase();
        if (tagName !== "input" && tagName !== "textarea" && !(document.activeElement as HTMLElement)?.isContentEditable) {
          event.preventDefault();
          searchInputRef.current?.focus();
        }
      } else if (event.key === "Escape" && document.activeElement === searchInputRef.current) {
        if (query) {
          event.preventDefault();
          setQuery("");
        } else {
          searchInputRef.current?.blur();
        }
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [query]);

  useEffect(() => {
    let disposed = false;
    let timer: number;
    async function poll() {
      const result = await command("get_courseware_export_status");
      if (disposed) return;
      if (result.ok && result.coursewareExport) { setStatus(result.coursewareExport); setConnectionError(""); }
      else setConnectionError(result.error || "读取导出进度失败");
      timer = window.setTimeout(() => void poll(), 1000);
    }
    void poll();
    return () => { disposed = true; window.clearTimeout(timer); };
  }, []);

  useEffect(() => {
    if (active) progressRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [active]);

  async function start() {
    setPending(true); setError("");
    const result = await command("start_courseware_export", { courseIds: chosen.map(course => course.id) });
    if (result.ok && result.coursewareExport) {
      setStatus(result.coursewareExport);
      window.setTimeout(() => progressRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }), 100);
    } else {
      setError(result.error || "启动导出失败");
    }
    setPending(false);
  }

  async function cancel() {
    setPending(true); setError("");
    const result = await command("cancel_courseware_export");
    if (result.ok && result.coursewareExport) setStatus(result.coursewareExport);
    else setError(result.error || "停止导出失败");
    setPending(false);
  }

  async function refresh() {
    setRefreshing(true); setError(await onRefresh()); setRefreshing(false);
  }

  function handleCourseMouseDown(event: React.MouseEvent) {
    if (event.shiftKey) {
      event.preventDefault();
    }
  }

  function handleCourseClick(courseId: number, index: number, event: React.MouseEvent<HTMLLabelElement>) {
    if (locked) return;

    if (event.shiftKey && lastClickedIndex !== null && lastClickedIndex >= 0 && lastClickedIndex < visible.length) {
      event.preventDefault();
      const isCurrentlyChecked = selected.includes(courseId);
      const targetState = !isCurrentlyChecked;
      const startIdx = Math.min(lastClickedIndex, index);
      const endIdx = Math.max(lastClickedIndex, index);
      const rangeIds = visible.slice(startIdx, endIdx + 1).map(c => c.id);
      setSelected(items => selectVisible(items, rangeIds, targetState));
      setLastClickedIndex(index);
      return;
    }

    setLastClickedIndex(index);
  }

  return <section className="cw-page" aria-label="课件导出">
    <header className="cw-head">
      <span className="cw-head-icon" aria-hidden="true"><FolderDown size={16} /></span>
      <div className="cw-head-text">
        <h2>课件导出</h2>
        <p>批量保存课件与作业：正文转成 Markdown 与 HTML，图片、附件、本人提交及可见互评一并下载。测验不会读取。</p>
      </div>
      <button className="secondary cw-refresh" disabled={refreshing || locked} onClick={() => void refresh()}>
        <RefreshCw size={14} className={refreshing ? "cw-spin" : ""} />{refreshing ? "读取中…" : "刷新课程"}
      </button>
    </header>

    <div className="cw-toolbar">
      <div className="cw-terms" role="group" aria-label="学期筛选">
        {termOptions.map(item => <button key={item.id} type="button" aria-pressed={term === item.id}
          className={`cw-term ${term === item.id ? "is-current" : ""}`} onClick={() => handleTermChange(item.id)}>
          {item.label}{item.id === current.id && <span>本学期</span>}
        </button>)}
        <button type="button" aria-pressed={term === "all"} className={`cw-term ${term === "all" ? "is-current" : ""}`}
          onClick={() => handleTermChange("all")}>全部学期</button>
      </div>
      <label className="cw-search">
        <Search size={14} aria-hidden="true" />
        <input
          ref={searchInputRef}
          type="search"
          aria-label="搜索课程"
          value={query}
          onChange={event => setQuery(event.target.value)}
          placeholder="搜索课程名或教师"
        />
        {query ? (
          <button
            type="button"
            className="cw-search-clear"
            aria-label="清空搜索"
            title="清空搜索 (Esc)"
            onClick={() => {
              setQuery("");
              searchInputRef.current?.focus();
            }}
          >
            ×
          </button>
        ) : (
          <kbd className="cw-search-kbd" title="按下 / 快速聚焦搜索">/</kbd>
        )}
      </label>
    </div>

    <div className="cw-body">
      <div className="cw-list-container">
        <div className="cw-list-header">
          <label className="cw-selectall-bar">
            <input
              type="checkbox"
              aria-label="全选当前列表"
              checked={allVisible}
              disabled={locked || !visible.length}
              ref={node => { if (node) node.indeterminate = someVisible && !allVisible; }}
              onChange={event => setSelected(items => selectVisible(items, visible.map(course => course.id), event.target.checked))}
            />
            <span className="cw-selectall-label">
              全选当前{visible.length ? ` ${visible.length} 门` : ""}
            </span>
          </label>
          <div className="cw-list-header-meta">
            {visible.length > 0 && (
              <span className="cw-list-header-count">
                {someVisible ? (
                  <>已选 <b>{visibleSelectedCount}</b> / {visible.length} 门</>
                ) : (
                  <>共 {visible.length} 门</>
                )}
              </span>
            )}
          </div>
        </div>

        <div className="cw-list" role="group" aria-label="可导出课程" ref={listRef} onScroll={checkListScroll}>
          {!visible.length && <div className="cw-empty" role="status">
            <div className="cw-empty-icon" aria-hidden="true">
              {courses.length ? <SearchX size={32} /> : <BookOpen size={32} />}
            </div>
            <strong>{courses.length ? "没有匹配的课程" : "还没有读到课程"}</strong>
            <span>{courses.length ? "换一个学期或搜索关键词试试。" : "请先在程序浏览器里完成学校登录，再点「刷新课程」。"}</span>
          </div>}
          {groups.map(group => <section className="cw-group" key={group.id}>
            {group.label && <h3>{group.label}<span>{group.items.length} 门</span></h3>}
            {group.items.map(course => {
              const index = visibleIndexMap.get(course.id) ?? 0;
              const isChecked = selected.includes(course.id);
              return <label
                key={course.id}
                className={`cw-course ${isChecked ? "is-checked" : ""}`}
                onMouseDown={handleCourseMouseDown}
                onClick={event => handleCourseClick(course.id, index, event)}
              >
                <input
                  type="checkbox"
                  aria-label={`选择 ${course.name}`}
                  checked={isChecked}
                  disabled={locked}
                  onChange={event => setSelected(items => selectVisible(items, [course.id], event.target.checked))}
                />
                <span className="cw-course-text">
                  <strong>{course.name}</strong>
                  <small>{course.teacherName || "未知教师"}{course.assumedSemester ? " · 学期未注明" : ""}</small>
                </span>
                <span className="cw-course-id" title={`课程 ID ${course.id}`}>#{course.id}</span>
              </label>;
            })}
          </section>)}
        </div>

        <div className={`cw-list-fade ${canScrollDown ? "is-visible" : ""}`} aria-hidden="true">
          <span>向下滚动查看更多 ↓</span>
        </div>
      </div>

      <aside className="cw-rail" aria-label="本次导出">
        <div className="cw-rail-head">
          <span className="cw-rail-tag">已选清单</span>
          {chosen.length > 0 && (
            <button
              type="button"
              className="cw-rail-clear-link"
              disabled={locked}
              onClick={() => setSelected([])}
            >
              清空全部
            </button>
          )}
        </div>
        <div className="cw-rail-count">
          <b>{chosen.length}</b>
          <span>门课程已选择</span>
        </div>

        {chosen.length ? <>
          <ul className="cw-chosen" aria-label="已选课程列表">
            {chosen.map(course => <li key={course.id} title={course.name}>
              <span className="cw-chosen-name">{course.name}</span>
              <button
                type="button"
                className="cw-chosen-remove"
                disabled={locked}
                aria-label={`移除 ${course.name}`}
                onClick={() => setSelected(items => items.filter(id => id !== course.id))}
              >
                ×
              </button>
            </li>)}
          </ul>
          <p className="cw-rail-hint">切换学期或搜索不会清空勾选；导出时按列表顺序逐门执行。</p>
        </> : <div className="cw-rail-empty">
          <div className="cw-rail-empty-icon" aria-hidden="true"><FolderDown size={24} /></div>
          <strong>从左侧勾选课程开始</strong>
          <p>可按学期筛选或搜索，点击「全选当前」可快速批量加入导出队列。</p>
        </div>}

        {active && status && (
          <div
            className="cw-rail-active-banner"
            role="button"
            tabIndex={0}
            onClick={() => progressRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" })}
            onKeyDown={e => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                progressRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
              }
            }}
            aria-label="查看正在进行的导出进度"
          >
            <span className="cw-rail-active-dot" />
            <div className="cw-rail-active-info">
              <strong>正在导出 ({status.courseDone}/{status.courseTotal})</strong>
              <span>点击查看详细进度 ↓</span>
            </div>
          </div>
        )}

        <div className="cw-rail-actions">
          <button className="primary" disabled={locked || !chosen.length} onClick={() => void start()}>
            {pending ? "处理中…" : chosen.length ? `导出所选 ${chosen.length} 门` : "选择课程后导出"}
          </button>
          {chosen.length > 0 && (
            <button className="secondary" disabled={locked} onClick={() => setSelected([])}>清空选择</button>
          )}
        </div>
        <p className="cw-rail-path">
          保存至本机 <span className="cw-nobr"><code>courseware</code></span> 目录；每门课包含 <span className="cw-nobr"><code>index.md</code> 与 <code>manifest.json</code></span>。
        </p>
      </aside>
    </div>

    {(error || connectionError) && <p className="cw-alert" role="alert">
      <TriangleAlert size={14} aria-hidden="true" />{error || connectionError}
    </p>}

    {status && status.state !== "idle" && <section className="cw-progress" aria-label="导出进度" ref={progressRef}>
      <header className="cw-progress-head">
        <div className="cw-progress-title">
          <strong>{exportStateLabel(status.state)}</strong>
          <span>{status.courseDone} / {status.courseTotal} 门</span>
        </div>
        <div className="cw-progress-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100}
          aria-valuenow={progressPercent} aria-label="总体进度">
          <i
            className={`cw-progress-bar-fill ${active ? "is-active" : ""} ${failedCourses > 0 ? "has-warn" : ""}`}
            style={{ width: `${progressPercent}%` }}
          />
        </div>
        <span className="cw-progress-percent">{progressPercent}%</span>
        {failedCourses > 0 && <span className="cw-progress-warn"><TriangleAlert size={12} aria-hidden="true" />{failedCourses} 门存在失败项</span>}
        {emptyCourses > 0 && <span className="cw-progress-muted">{emptyCourses} 门未发现资料</span>}
        {active && <button className="secondary cw-stop" disabled={pending || status.state === "cancelling"} onClick={() => void cancel()}>
          <Square size={12} aria-hidden="true" />{status.state === "cancelling" ? "停止中…" : "停止队列"}
        </button>}
      </header>
      {active && <p className="cw-progress-note">停止后保留已保存内容：当前文件处理结束后停止，后续课程不再执行。</p>}
      {status.error && <p className="cw-alert" role="alert"><TriangleAlert size={14} aria-hidden="true" />{status.error}</p>}
      <ul className="cw-jobs">
        {status.results.map(item => {
          const empty = item.state === "completed" && item.pageTotal === 0 && item.fileWritten === 0;
          const itemProgress = item.chapterTotal > 0
            ? Math.min(100, Math.round((item.chapterDone / item.chapterTotal) * 100))
            : (item.state === "completed" ? 100 : 0);

          return <li className={`cw-job ${empty ? "empty" : item.state}`} key={item.courseId}>
            <div className="cw-job-head">
              <strong title={item.courseName}>{item.courseName}</strong>
              <span className={`cw-pill ${empty ? "empty" : item.state}`}>
                {empty ? <>
                  <CheckCheck size={11} aria-hidden="true" />
                  未发现资料
                </> : <>
                  {item.state === "completed" && <CheckCheck size={11} aria-hidden="true" />}
                  {item.state === "running" && <Loader2 size={11} className="cw-spin" aria-hidden="true" />}
                  {item.state === "queued" && <Clock size={11} aria-hidden="true" />}
                  {item.state === "cancelling" && <Square size={11} aria-hidden="true" />}
                  {item.state === "cancelled" && <CircleSlash size={11} aria-hidden="true" />}
                  {(item.state === "error" || item.state === "auth_required") && <TriangleAlert size={11} aria-hidden="true" />}
                  {exportStateLabel(item.state)}{item.failures.length ? ` · ${item.failures.length} 项失败` : ""}
                </>}
              </span>
            </div>

            {item.state !== "queued" && item.chapterTotal > 0 && (
              <div className="cw-job-bar-row">
                <div
                  className="cw-job-bar"
                  role="progressbar"
                  aria-valuenow={itemProgress}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-label={`${item.courseName} 章节进度`}
                >
                  <i
                    className={`cw-job-bar-fill is-${empty ? "empty" : item.state}`}
                    style={{ width: `${itemProgress}%` }}
                  />
                </div>
                <span className="cw-job-bar-percent">{itemProgress}%</span>
              </div>
            )}

            <div className="cw-job-metrics">
              <span>章节/作业 <b>{item.chapterDone}</b>/{item.chapterTotal}</span>
              <span>页面 <b>{item.pageTotal}</b></span>
              <span>新写 <b>{item.pageWritten}</b></span>
              <span>未变 <b>{item.pageUnchanged}</b></span>
              <span>附件 <b>{item.fileWritten}</b></span>
            </div>
            {empty && <p className="cw-job-empty">资源区、学习页与作业中未发现可导出的正文或附件；目录与清单已保存。</p>}
            {item.folder && <code className="cw-job-folder">{item.folder}</code>}
            {item.error && <p className="cw-alert" role="alert"><TriangleAlert size={13} aria-hidden="true" />{item.error}</p>}
            {item.failures.length > 0 && <details className="cw-job-failures">
              <summary>查看失败详情（{item.failures.length}）</summary>
              {item.failures.map((failure, index) => <p key={index}>
                {[failure.chapter, failure.file].filter(Boolean).join(" / ") || "未分类"}：{failure.reason}
              </p>)}
            </details>}
          </li>;
        })}
      </ul>
      {status.root && <p className="cw-root">保存位置 <code>{status.root}</code></p>}
    </section>}
  </section>;
}
