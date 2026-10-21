export type SignChoice = { id: number; title: string; detail: string; disabled?: boolean };

export function filterSignChoices(items: SignChoice[], query: string): SignChoice[] {
  const keyword = query.trim().toLocaleLowerCase();
  return items.filter(item => !keyword || `${item.title} ${item.detail} ${item.id}`.toLocaleLowerCase().includes(keyword));
}

/** 从 from 起按 step 方向找到最近的可选项；全部不可选时停在原处。 */
export function stepSignChoiceCursor(items: SignChoice[], from: number, step: number): number {
  for (let index = from + step; index >= 0 && index < items.length; index += step) {
    if (!items[index].disabled) return index;
  }
  return from;
}

export function firstSignChoiceCursor(items: SignChoice[]): number {
  return Math.max(0, items.findIndex(item => !item.disabled));
}

/** 列表项与回执卡片共用的排版：标题 / 状态与时间 / 课堂编号。 */
export function SignChoiceBody({ item }: { item: SignChoice }) {
  return <><span><strong>{item.title}</strong><small>{item.detail}</small></span><code>ID {item.id}</code></>;
}

/** 已选定课堂的回执卡片，与列表项保持同一套排版，避免同一个课堂出现两种说法。 */
export function SelectedSignChoice({ item, busy, onChange }: {
  item: SignChoice; busy: boolean; onChange?: () => void;
}) {
  return <div className="sign-classroom-card" role="group" aria-label="已选择的课堂">
    <div className="course-option active"><SignChoiceBody item={item} /></div>
    {onChange && <button type="button" className="secondary compact" disabled={busy} onClick={onChange}>更换课堂</button>}
  </div>;
}

export function SignChoicePicker({ label, items, total, cursor, busy, onCursor, onChoose }: {
  label: string; items: SignChoice[]; total: number; cursor: number; busy: boolean;
  onCursor: (index: number) => void; onChoose: (id: number) => void;
}) {
  return <>
    <div className="course-picker-head"><strong>{label}</strong><span>{items.length} / {total}</span></div>
    <div className="course-options" role="listbox" aria-label={label}>
      {items.length ? items.map((item, index) => <button key={item.id} type="button" role="option"
        disabled={busy || item.disabled} aria-selected={index === cursor} aria-disabled={item.disabled || undefined}
        title={item.disabled ? "已结束的课堂不能作为监测目标" : undefined}
        className={`course-option ${index === cursor ? "active" : ""} ${item.disabled ? "is-disabled" : ""}`}
        onMouseEnter={() => { if (!item.disabled) onCursor(index); }} onClick={() => onChoose(item.id)}>
        <SignChoiceBody item={item} />
      </button>) : <div className="course-empty">暂无可选项，请刷新或换个关键词。</div>}
    </div>
    <div className="course-picker-help">↑↓ 移动　Enter 选择　Esc 清空搜索</div>
  </>;
}