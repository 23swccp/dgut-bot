import { useEffect, useId, useRef, type CSSProperties, type KeyboardEvent } from "react";
import { BookOpen, RefreshCw } from "lucide-react";
import { lessonProgress, lessonStudyAction, lessonStudyTime, openPhaseLabel, type CourseScanStatus, type LessonItem } from "./courseScanView";
import { formatDuration, runStateLabel, type CourseStatus } from "./courseObservability";

type Props = {
  visible: boolean;
  scan: CourseScanStatus;
  status: CourseStatus | null;
  courseId: string;
  active: boolean;
  exiting: boolean;
  error: string;
  loginPending: boolean;
  loginError: string;
  onOpenLogin: () => void;
  onCourse: (id: string) => void;
  onScan: (force: boolean) => void;
  onCancelScan: () => void;
  onOpen: (item: LessonItem) => void;
  onExit: () => void;
};

export function CourseLearningPanel({
  visible, scan, status, courseId, active, exiting, error, loginPending, loginError, onOpenLogin,
  onCourse, onScan, onCancelScan, onOpen, onExit,
}: Props) {
  const id = useId();
  const panelRef = useRef<HTMLElement>(null);
  const groups = scan.groups;
  const currentGroup = groups.find(group => group.courseId === courseId) ?? groups[0];
  const items = currentGroup?.items ?? [];
  const selected = scan.groups.flatMap(group => group.items).find(item => item.id === scan.selectedId);
  const scanning = scan.state === "scanning";
  const needsLogin = loginPending || scan.state === "auth_required";
  const tabId = (groupId: string) => `${id}-tab-${groupId}`;
  const pageId = (groupId: string) => `${id}-course-${groupId}`;

  const firstCourseId = scan.groups[0]?.courseId;
  useEffect(() => {
    if (!courseId && firstCourseId) onCourse(firstCourseId);
  }, [courseId, firstCourseId, onCourse]);

  function selectCourse(groupId: string) {
    if (groupId !== currentGroup?.courseId) {
      panelRef.current?.scrollTo({ top: 0 });
    }
    onCourse(groupId);
  }

  function handleCourseKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const last = groups.length - 1;
    const next = event.key === "ArrowDown" ? (index === last ? 0 : index + 1)
      : event.key === "ArrowUp" ? (index === 0 ? last : index - 1)
      : event.key === "Home" ? 0 : event.key === "End" ? last : -1;
    if (next < 0) return;
    event.preventDefault();
    const groupId = groups[next].courseId;
    selectCourse(groupId);
    document.getElementById(tabId(groupId))?.focus();
  }

  const resultCount = groups.reduce((total, group) => total + group.items.length, 0);
  const totalLabel = `${groups.length} 门课程 · ${resultCount} 章`;
  const scanLabel = scan.stopping ? "正在取消扫描…" : `扫描中 · ${scan.scannedCourses} / ${scan.totalCourses} 门`;
  const runningStatus = status?.running || (scan.openPhase === "started" && status?.completed) ? status : null;
  const taskError = scan.openError || runningStatus?.resourceError?.message;
  const taskLabel = runningStatus
    ? runStateLabel(runningStatus)
    : openPhaseLabel(scan.openPhase) || (active ? "正在启动" : "");
  const courseName = runningStatus?.courseName || selected?.courseName;
  const pageName = runningStatus?.page?.name || selected?.title;
  const taskTitle = courseName && pageName && courseName !== pageName ? `${courseName} / ${pageName}` : pageName || courseName;
  const canRetry = !active && selected?.canAutoOpen && ["failed", "timeout"].includes(scan.openPhase);
  const video = runningStatus?.video;
  const videoTime = !runningStatus?.completed && video?.duration && video.duration > 0
    ? `${formatDuration(video.currentTime)} / ${formatDuration(video.duration)}`
    : "";
  const taskDetail = [runningStatus?.currentTask === "完成" ? "" : runningStatus?.currentTask, videoTime].filter(Boolean).join(" · ");
  const emptyMessage = scanning ? "正在读取课件…" : "暂无课件。";

  return <section ref={panelRef} className="learning-panel" hidden={!visible} aria-labelledby={`${id}-title`}>
    <div className="learning-content">
      <div className="learning-heading">
        <h1 id={`${id}-title`}>课件</h1>
        {!needsLogin && <span>{totalLabel}</span>}
      </div>
      <div className="learning-toolbar">
        <div className="learning-reminder">
          <img src="./course-learning-reminder.jpg" alt="" />
          <span>刷完课要停够时长哦~不然老师会看出来的</span>
        </div>
        <div className="learning-scan-actions">
          {needsLogin ? <button type="button" onClick={onOpenLogin}>打开优学院登录页</button> : scanning ? <>
            <span role="status">{scanLabel}</span>
            <button type="button" disabled={scan.stopping} onClick={onCancelScan}>取消扫描</button>
          </> : <button type="button" disabled={scan.stopping} onClick={() => onScan(true)}>
            <RefreshCw size={15} aria-hidden="true" />{scan.stopping ? "正在停止…" : "重新扫描"}
          </button>}
        </div>
      </div>
      {(taskLabel || error) && <div className={`learning-task${taskError || error ? " warning" : ""}`}>
        <div className="learning-task-main">
          <div className="learning-task-summary">
            {taskLabel && <span className="learning-task-state" role="status">{taskLabel}</span>}
            {taskTitle && <strong>{taskTitle}</strong>}
            {taskDetail && <span className="learning-task-detail">{taskDetail}</span>}
          </div>
          {active && <button type="button" className="learning-stop" disabled={exiting} onClick={onExit}>
            {exiting ? "退出中…" : "退出刷课"}
          </button>}
          {canRetry && <button type="button" onClick={() => onOpen(selected)}>重试打开</button>}
        </div>
        {taskError && <p role="alert">{taskError}</p>}
        {error && error !== taskError && <p role="alert">{error}</p>}
      </div>}
      {needsLogin && <p className="learning-notice" role="status">
        {loginError || scan.error || "正在检测优学院登录状态…"}
      </p>}
      {scan.state === "error" && <p className="learning-notice" role="alert">{scan.error || "课程目录读取失败。"}</p>}
      {!needsLogin && scan.failures.length > 0 && <details className="learning-failures">
        <summary>{scan.failures.length} 门课程扫描失败</summary>
        <ul>{scan.failures.map(failure => <li key={failure.courseId}>{failure.courseName} · {failure.reason}</li>)}</ul>
        {!scanning && <button type="button" disabled={scan.stopping} onClick={() => onScan(false)}>重试失败课程</button>}
      </details>}
      {!needsLogin && currentGroup && <div className="learning-courses">
        <div className="learning-course-tabs" role="tablist" aria-label="课程" aria-orientation="vertical">
          {groups.map((group, index) => {
            const isCurrent = group.courseId === currentGroup.courseId;
            return <button key={group.courseId} type="button" role="tab" id={tabId(group.courseId)}
              aria-controls={pageId(group.courseId)} aria-selected={isCurrent} tabIndex={isCurrent ? 0 : -1}
              onClick={() => selectCourse(group.courseId)} onKeyDown={event => handleCourseKey(event, index)}
              title={group.courseName}>
              <BookOpen size={17} aria-hidden="true" />
              <span>{group.courseName}</span>
            </button>;
          })}
        </div>
        {groups.map(group => {
          const isCurrent = group.courseId === currentGroup.courseId;
          return <section key={group.courseId} className="learning-chapters" role="tabpanel" id={pageId(group.courseId)}
            aria-labelledby={tabId(group.courseId)} hidden={!isCurrent}>
            {isCurrent && <>
              <div className="learning-course-title">{group.courseName}</div>
              {(() => {
                const allItems = scan.groups.find(entry => entry.courseId === group.courseId)?.items || [];
                const textbookIds = new Set(allItems.map(item => item.courseId));
                const summary = textbookIds.size === 1 ? allItems[0]?.textbookSummary : null;
                return <div className="learning-overview">
                  <div className="learning-cover">{summary?.cover && <img src={summary.cover} alt={group.courseName} referrerPolicy="no-referrer" />}</div>
                  <div className="learning-metric progress"><strong>{summary?.progress == null ? "--" : `${Math.round(summary.progress)}%`}</strong><span>进度</span></div>
                  <div className="learning-metric time"><strong>{lessonStudyTime(summary?.studySeconds)}</strong><span>累计学习时间</span></div>
                  <div className="learning-metric score"><strong>{summary?.score == null ? "--" : Math.round(summary.score)}<small>分</small></strong><span>分数</span></div>
                </div>;
              })()}
              <div className="learning-table-scroll">
              <table className="learning-table" aria-label={`${group.courseName}章节`}>
                <colgroup><col /><col /><col /><col /><col /><col /></colgroup>
                <thead><tr>
                  <th scope="col">名称</th><th scope="col">进度</th><th scope="col">累计学习时间</th>
                  <th scope="col">分数</th><th scope="col">可学时间</th><th scope="col">操作</th>
                </tr></thead>
                <tbody>{items.map(item => {
                  const current = active && item.id === scan.selectedId;
                  const progress = lessonProgress(item);
                  const action = current ? "正在学习" : lessonStudyAction(item);
                  return <tr key={item.id} className={current ? "highlighted" : undefined}>
                    <th scope="row">{item.title}</th>
                    <td><span className="learning-progress">
                      <span className="learning-progress-ring" aria-hidden="true"
                        style={{ "--lesson-progress": `${progress}%` } as CSSProperties} />
                      {progress}%
                    </span></td>
                    <td>{lessonStudyTime(item.studySeconds)}</td>
                    <td>{item.score == null ? "--" : Math.round(item.score)}</td>
                    <td>{item.startDate ? `${item.startDate} 起` : "--"}
                      {item.endDate && <><br />{item.endDate} 止</>}
                    </td>
                    <td>{item.canAutoOpen
                      ? <button type="button" className="learning-start" disabled={active}
                        aria-label={`${action} ${item.title}`} onClick={() => onOpen(item)}>{action}</button>
                      : <span className="learning-unavailable" title={item.unavailableReason}>无法打开</span>}</td>
                  </tr>;
                })}</tbody>
              </table>
            </div></>}
          </section>;
        })}
      </div>}
      {!needsLogin && scan.state !== "error" && !items.length && <p className="learning-empty" role="status">{emptyMessage}</p>}
    </div>
  </section>;
}
