import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { exportKinds, exportSources, type ExportCatalog, type ExportCourse, type ExportOptions } from "./coursewareView";

type Props = {
  courses: ExportCourse[]; locked: boolean; options: ExportOptions;
  onChange: Dispatch<SetStateAction<ExportOptions>>; onLoading: (loading: boolean) => void;
};

export function CoursewareExportFilters({ courses, locked, options, onChange, onLoading }: Props) {
  const [catalog, setCatalog] = useState<ExportCatalog[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const request = useRef(0);
  const courseKey = courses.map(course => course.id).join(",");

  useEffect(() => {
    request.current += 1;
    setCatalog(null); setError(""); setLoading(false); onLoading(false);
    onChange(previous => ({ ...previous, chapters: {} }));
    return () => { request.current += 1; };
  }, [courseKey, onChange, onLoading]);

  function toggle(key: "sources" | "kinds", id: string, checked: boolean) {
    onChange(previous => ({ ...previous, [key]: checked ? [...previous[key], id] : previous[key].filter(value => value !== id) }));
  }

  async function readCatalog() {
    const current = ++request.current;
    setLoading(true); onLoading(true); setError("");
    try {
      const response = await fetch("/api/command", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: "get_courseware_export_catalog", payload: { courseIds: courses.map(course => course.id) } }),
      });
      const result = await response.json() as { ok: boolean; error?: string; catalog?: ExportCatalog[] };
      if (current !== request.current) return;
      if (!result.ok || !result.catalog) throw new Error(result.error || "读取导出目录失败");
      setCatalog(result.catalog);
      onChange(previous => ({ ...previous, chapters: Object.fromEntries(result.catalog!.map(course => [
        course.courseId, course.chapters.map(chapter => chapter.id),
      ])) }));
    } catch (failure) {
      if (current === request.current) setError(String(failure));
    } finally {
      if (current === request.current) { setLoading(false); onLoading(false); }
    }
  }

  function selectChapters(course: ExportCatalog, ids: string[], checked: boolean) {
    onChange(previous => {
      const selected = new Set(previous.chapters[course.courseId] ?? course.chapters.map(chapter => chapter.id));
      ids.forEach(id => checked ? selected.add(id) : selected.delete(id));
      return { ...previous, chapters: { ...previous.chapters, [course.courseId]: [...selected] } };
    });
  }

  return <section className="cw-filters" aria-label="导出内容筛选">
    <div className="cw-filter-heading"><h3>导出内容</h3></div>
    <fieldset disabled={locked || loading} className="cw-filter-row"><legend>资料范围</legend>
      {exportSources.map(item => <label key={item.id}><input type="checkbox" checked={options.sources.includes(item.id)}
        onChange={event => toggle("sources", item.id, event.target.checked)} />{item.label}</label>)}
    </fieldset>
    <fieldset disabled={locked || loading} className="cw-filter-row"><legend>正文与附件类型</legend>
      {exportKinds.map(item => <label key={item.id}><input type="checkbox" checked={options.kinds.includes(item.id)}
        onChange={event => toggle("kinds", item.id, event.target.checked)} />{item.label}</label>)}
    </fieldset>
    {(!options.sources.length || !options.kinds.length) && <p className="cw-alert" role="alert">请至少选择一项资料范围和一项正文或附件类型。</p>}
    <div className="cw-catalog-head"><strong>章节与作业</strong>
      <button type="button" className="secondary" disabled={locked || loading || !courses.length} onClick={() => void readCatalog()}>
        {loading ? "正在读取目录…" : catalog ? "重新读取目录" : "读取目录并选择"}
      </button>
    </div>
    {error && <p className="cw-alert" role="alert">{error}</p>}
    {catalog && <>
      <label className="cw-search"><input type="search" aria-label="搜索章节或作业" placeholder="搜索章节或作业名称" value={query} onChange={event => setQuery(event.target.value)} /></label>
      <div className="cw-catalog-list">
        {catalog.map(course => {
          const visible = course.chapters.filter(chapter => options.sources.includes(chapter.source)
            && chapter.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
          const selected = options.chapters[course.courseId] ?? course.chapters.map(chapter => chapter.id);
          return <section key={course.courseId} className="cw-catalog-course">
            <header><strong>{course.courseName}</strong><span>{visible.filter(chapter => selected.includes(chapter.id)).length} / {visible.length} 项
              <button type="button" disabled={locked || loading || !visible.length} onClick={() => selectChapters(course, visible.map(chapter => chapter.id), true)}>全选当前</button>
              <button type="button" disabled={locked || loading || !visible.length} onClick={() => selectChapters(course, visible.map(chapter => chapter.id), false)}>清空当前</button>
            </span></header>
            {course.error && <p className="cw-alert" role="alert">{course.error}</p>}
            {course.failures.map((failure, index) => <p className="cw-alert" key={index}>{failure.chapter}：{failure.reason}</p>)}
            {!visible.length && !course.error && <p className="cw-filter-note">当前范围和搜索下没有目录项。</p>}
            {visible.map(chapter => <label className="cw-catalog-item" key={chapter.id}>
              <input type="checkbox" checked={selected.includes(chapter.id)} disabled={locked || loading}
                onChange={event => selectChapters(course, [chapter.id], event.target.checked)} />
              <span>{chapter.title}</span><small>{exportSources.find(source => source.id === chapter.source)?.label}</small>
            </label>)}
          </section>;
        })}
      </div>
    </>}
  </section>;
}
