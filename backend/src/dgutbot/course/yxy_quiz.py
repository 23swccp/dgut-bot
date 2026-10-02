"""优学院测验自动作答模块。

依据《测验页面结构调研.md》的实测结构工作：测验渲染在 learnCourse 主文档
（无 iframe），已答判据是 .question-wrapper 的 finished 类，提交按钮是卷尾
.question-operation-area > .btn-submit（整套提交，点一次全部判卷），未答完
翻页会弹 .modal.fade.in 确认框。本模块只通过 CDP Input 域产生真实点击
（isTrusted 事件），不调用任何作答接口。

作答策略是固定占位：选择题点 C、判断题点"错误"、每个填空输入英文逗号。
无法识别的题型会记录并跳过，未答完离开页面由弹窗放行。

QuizHandler 不持有任何连接：通过构造参数注入 evaluate/click/type_text 等
回调，既能被 CourseController 复用，也能用底部的 CLI 后端独立运行。
"""

from __future__ import annotations

import json
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.request import urlopen

from websocket import create_connection
from dgutbot.agent.agent_protocol import AgentError
from dgutbot.course.course_dialogs import DIALOG_POLICY_JS, AUTOMATIC_DIALOG_POLICIES, handle_dialog

PORT = 9222
TAB_KEYWORD = "ua.dgut.edu.cn/learnCourse"

# 页面状态读取脚本：一次 evaluate 拿回题目、坐标、提交按钮与弹窗。
# 只读；坐标为视口坐标，与 Input.dispatchMouseEvent 直接兼容。
QUIZ_STATE_JS = r"""
(function() {
  /* COURSE_DIALOG_POLICY */
  function visible(el) {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function center(el) {
    const r = el.getBoundingClientRect();
    const x = Math.round(r.x + r.width / 2), y = Math.round(r.y + r.height / 2);
    const hit = document.elementFromPoint(x, y);
    return {x: x, y: y, pointMatches: Boolean(hit && (hit === el || el.contains(hit))),
      enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true'};
  }
  const view = document.querySelector('.question-view');
  if (!view || !visible(view)) return JSON.stringify({present: false});
  const mask = Array.from(view.querySelectorAll('.limit-time-mask')).find(visible);
  let startButton = null;
  if (mask) {
    const buttons = Array.from(mask.querySelectorAll('.limit-time-question-info > button')).filter(function(b) {
      return visible(b) && (b.textContent || '').trim() === '开始答题' &&
        /\bclick\s*:\s*startQuizTimer\b/.test(b.getAttribute('data-bind') || '');
    });
    if (buttons.length === 1) {
      const b = buttons[0], pos = center(b);
      const hit = document.elementFromPoint(pos.x, pos.y);
      startButton = Object.assign(pos, {
        enabled: !b.disabled && b.getAttribute('aria-disabled') !== 'true',
        pointMatches: Boolean(hit && (hit === b || b.contains(hit)))
      });
    }
  }
  const questions = [];
  document.querySelectorAll('.question-wrapper').forEach(function(w) {
    const tagEl = w.querySelector('.question-type-tag');
    const titleEl = w.querySelector('.question-title-html');
    const promptEl = titleEl ? titleEl.cloneNode(true) : null;
    if (promptEl) promptEl.querySelectorAll('.answer-width').forEach(function(blank) {
      blank.replaceWith(document.createTextNode('[blank]'));
    });
    const choices = [];
    let hasMedia = Boolean(promptEl && promptEl.querySelector('img, svg, math, canvas, .katex, .MathJax'));
    w.querySelectorAll('a.choice-item').forEach(function(a) {
      if (!visible(a)) return;
      const label = ((a.querySelector('.option') || {}).innerText || '').trim().replace(/[.。]+\s*$/, '');
      const content = a.querySelector('.content-wrapper') || a;
      hasMedia = hasMedia || Boolean(content.querySelector('img, svg, math, canvas, .katex, .MathJax'));
      choices.push({label: label, text: (content.innerText || '').trim(), selected: !!a.querySelector('.checkbox.selected'), pos: center(a)});
    });
    const judgment = [];
    w.querySelectorAll('.checking-type .choice-btn').forEach(function(b) {
      if (!visible(b)) return;
      judgment.push({label: /right-btn/.test(b.className) ? '正确' : '错误', selected: b.classList.contains('selected'), pos: center(b)});
    });
    const blanks = [];
    w.querySelectorAll('.answer-width').forEach(function(s) {
      if (visible(s)) {
        const input = s.matches('input, textarea, [contenteditable="true"]')
          ? s : s.querySelector('input, textarea, [contenteditable="true"]');
        const target = input && visible(input) ? input : s;
        blanks.push({pos: center(target), focused: target === document.activeElement || target.contains(document.activeElement), value: String(input ? input.value || input.innerText || '' : s.innerText || '')});
      }
    });
    let submit = null;
    const area = document.querySelector('.question-operation-area .btn-submit');
    if (area && visible(area)) submit = center(area);
    questions.push({
      qid: w.id,
      finished: w.classList.contains('finished'),
      type: (tagEl ? tagEl.innerText : '').trim(),
      title: (promptEl ? promptEl.textContent : '').trim(),
      hasMedia: hasMedia,
      choices: choices,
      judgment: judgment,
      blanks: blanks,
      submit: submit
    });
  });
  const modal = courseDialogState();
  if (modal) modal.text = modal.title;
  return JSON.stringify({
    present: true,
    pageId: String((window.__yxy_controller && window.__yxy_controller.get_page_state() || {}).page || ''),
    viewport: {w: window.innerWidth, h: window.innerHeight},
    questions: questions,
    modal: modal,
    startRequired: Boolean(mask),
    startButton: startButton
  });
})()
""".replace('/* COURSE_DIALOG_POLICY */', DIALOG_POLICY_JS)

SCROLL_QUESTION_JS = """
(function(id) {
  const w = document.getElementById(id);
  if (!w) return 'missing';
  w.scrollIntoView({block: 'center', behavior: 'instant'});
  return 'ok';
})(%s)
"""

SCROLL_QUIZ_START_JS = r"""
(function() {
  const buttons = Array.from(document.querySelectorAll('.question-view .limit-time-mask .limit-time-question-info > button'));
  const matches = buttons.filter(function(b) {
    const r = b.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && (b.textContent || '').trim() === '开始答题' &&
      /\bclick\s*:\s*startQuizTimer\b/.test(b.getAttribute('data-bind') || '');
  });
  if (matches.length !== 1) return 'missing';
  matches[0].scrollIntoView({block: 'center', behavior: 'instant'});
  return 'ok';
})()
"""

# 一页卷模式下卷尾提交按钮在全部题目之后，长页时必然在视口外，点击前先滚动
SCROLL_SUBMIT_JS = """
(function() {
  const b = document.querySelector('.question-operation-area .btn-submit');
  if (!b) return 'missing';
  b.scrollIntoView({block: 'center', behavior: 'instant'});
  return 'ok';
})()
"""

SCROLL_BLANK_JS = """
(function(id, index) {
  const w = document.getElementById(id);
  if (!w) return 'missing-question';
  const blanks = w.querySelectorAll('.answer-width');
  const blank = blanks[index];
  if (!blank) return 'missing-blank';
  const input = blank.matches('input, textarea, [contenteditable="true"]')
    ? blank : blank.querySelector('input, textarea, [contenteditable="true"]');
  (input || blank).scrollIntoView({block: 'center', behavior: 'instant'});
  return 'ok';
})(%s, %d)
"""


@dataclass
class Question:
    qid: str
    finished: bool
    type: str
    title: str
    has_media: bool = False
    choices: list = field(default_factory=list)
    judgment: list = field(default_factory=list)
    blanks: list = field(default_factory=list)
    submit: dict | None = None


@dataclass
class QuizState:
    present: bool
    viewport: dict = field(default_factory=dict)
    questions: list[Question] = field(default_factory=list)
    modal: dict | None = None
    page_id: str = ""
    start_required: bool = False
    start_button: dict | None = None


class QuizHandler:
    """测验作答核心；全部页面动作通过注入的回调完成，便于复用与测试。"""

    def __init__(
        self,
        *,
        evaluate: Callable[[str], Any],
        click: Callable[[float, float], bool],
        is_running: Callable[[], bool] = lambda: True,
        type_text: Callable[[str], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[..., None] = print,
        dry_run: bool = True,
        jitter: float = 3.0,
        start_attempts: set[str] | None = None,
        dialog_attempts: dict | None = None,
        auto_dismiss_dialog: bool = True,
    ) -> None:
        self._evaluate = evaluate
        self._click = click
        self._is_running = is_running
        self._type_text = type_text
        self._sleep = sleep
        self._log = log
        self.dry_run = dry_run
        self._jitter = max(0.0, jitter)
        self._start_attempts = start_attempts if start_attempts is not None else set()
        self._dialog_attempts = dialog_attempts if dialog_attempts is not None else {}
        self._auto_dismiss_dialog = auto_dismiss_dialog

    # ---- 页面读取 ----

    def read_state(self) -> QuizState:
        value = self._evaluate(QUIZ_STATE_JS)
        if not isinstance(value, str):
            return QuizState(present=False)
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            self._log("读取测验页面状态失败，稍后重试", "warn")
            return QuizState(present=False)
        questions = [Question(
            qid=item["qid"], finished=item["finished"], type=item["type"], title=item["title"],
            has_media=bool(item.get("hasMedia", False)), choices=item.get("choices") or [],
            judgment=item.get("judgment") or [], blanks=item.get("blanks") or [], submit=item.get("submit"),
        ) for item in data.get("questions", [])]
        return QuizState(
            present=bool(data.get("present")),
            viewport=data.get("viewport") or {},
            questions=questions,
            modal=data.get("modal"),
            page_id=str(data.get("pageId") or ""),
            start_required=bool(data.get("startRequired")),
            start_button=data.get("startButton"),
        )

    @staticmethod
    def _quiz_identity(state: QuizState) -> str:
        return state.page_id or "|".join(q.qid for q in state.questions)

    def ensure_started(self) -> bool:
        """限时测验先点击已确认的启动入口，验证同页遮罩消失后才能答题。"""
        state = self.read_state()
        if not state.present or not self._is_running():
            return False
        if not state.start_required:
            return True
        if self.dry_run:
            self._log("  [dry-run] 检测到限时测验，将先点击“开始答题”")
            return False
        identity = self._quiz_identity(state)
        if not identity or identity in self._start_attempts:
            self._log("  限时测验启动尚未确认，本轮不重复点击", "warn")
            return False
        if self._evaluate(SCROLL_QUIZ_START_JS) != "ok":
            return False
        fresh = self.read_state()
        if not fresh.present or self._quiz_identity(fresh) != identity or not self._is_running():
            return False
        if not fresh.start_required:
            return True
        target = fresh.start_button
        if not target or not target.get("enabled") or not target.get("pointMatches"):
            self._log("  限时测验启动按钮暂不可操作，稍后重试", "warn")
            return False
        self._start_attempts.add(identity)
        self._log("  检测到限时测验，自动点击“开始答题”")
        # 命中校验针对按钮中心，启动时不叠加随机偏移。
        if not self._click(float(target["x"]), float(target["y"])):
            return False
        for _ in range(40):
            self._sleep(0.25)
            if not self._is_running():
                return False
            check = self.read_state()
            if not check.present or self._quiz_identity(check) != identity:
                return False
            if not check.start_required and check.questions:
                self._log("  限时测验已开始，继续自动作答")
                return True
        self._log("  未确认限时测验启动，暂不操作题目", "warn")
        return False

    def _scroll_question_into_view(self, qid: str) -> bool:
        return self._evaluate(SCROLL_QUESTION_JS % json.dumps(qid)) == "ok"

    # ---- 点击原语 ----

    def _click_point(self, pos: dict, label: str = "") -> bool:
        x = float(pos["x"]) + random.uniform(-self._jitter, self._jitter)
        y = float(pos["y"]) + random.uniform(-self._jitter, self._jitter)
        return self._click(x, y)

    def _click_in_viewport(self, pos: dict, viewport: dict, label: str = "") -> bool:
        if pos.get("pointMatches") is False or pos.get("enabled") is False:
            return False
        vw = float(viewport.get("w", 0))
        vh = float(viewport.get("h", 0))
        if vw and not (0 <= float(pos["x"]) <= vw):
            return False
        if vh and not (0 <= float(pos["y"]) <= vh):
            self._log(f"  {label}不在视口内（y={pos['y']}），放弃", "warn")
            return False
        return self._click_point(pos, label)

    # ---- 弹窗 ----

    def handle_modal(self, modal: dict, summary: dict, advance: bool = True) -> bool:
        """按共享语义策略处理，重读目标并验证关闭，不把确认框当作交卷。"""
        if not self._auto_dismiss_dialog:
            return False
        allowed = AUTOMATIC_DIALOG_POLICIES | {'navigation'}
        if self.dry_run:
            self._log(f"  [dry-run] 弹窗策略：{modal.get('type')} / {modal.get('policy')}")
            return False
        outcome, fresh = handle_dialog(self._evaluate, self._click, self._sleep, self._is_running,
                                       self._dialog_attempts, allowed=allowed, expected=modal.get('signature'), prefer_stay=not advance)
        if outcome == 'dismissed':
            summary['modals'] += 1
            self._log(f"  已处理弹窗：{fresh.get('title')}")
            return True
        self._log(f"  弹窗未处理：{fresh.get('type', modal.get('type'))} / {outcome}", "warn")
        return False

    # ---- 单题选择（不提交） ----

    def _fresh_question(self, qid: str) -> Question | None:
        state = self.read_state()
        if state.start_required or state.modal:
            return None
        return next((q for q in state.questions if q.qid == qid), None)

    def _select_answer(self, state: QuizState, q: Question, option_label: str, judgment_label: str) -> str:
        """点击一道未完成题的选项（不提交）。返回 'done' / 'skipped' / 'failed' / 'planned'。"""
        target = judgment_label if q.type == "判断题" else option_label
        if q.judgment:
            entry = next((j for j in q.judgment if j["label"] == target), None)
        else:
            entry = next((c for c in q.choices if c["label"].upper() == target.upper()), None)
        if entry is None:
            self._log(f"  题{q.qid} 找不到选项「{target}」，跳过")
            return "skipped"

        if self.dry_run:
            self._log(f"  [dry-run] 题{q.qid} {q.type} 将点「{target}」")
            return "planned"

        # 滚动到题目并等待坐标稳定；组件刚挂载时框架可能重置滚动位置，需重试
        fresh = None
        entry = None
        for attempt in range(3):
            if not self._scroll_question_into_view(q.qid):
                self._log(f"  题{q.qid} 滚动失败，跳过", "warn")
                return "skipped"
            self._sleep(random.uniform(0.4, 0.9) * (attempt + 1))
            fresh = self._fresh_question(q.qid)
            if fresh is None:
                return "failed"
            if fresh.finished:
                return "done"
            if fresh.judgment:
                entry = next((j for j in fresh.judgment if j["label"] == target), None)
            else:
                entry = next((c for c in fresh.choices if c["label"].upper() == target.upper()), None)
            if entry is not None and 0 <= float(entry["pos"]["y"]) <= float(state.viewport.get("h", 0)):
                break
            self._log(f"  题{q.qid} 第{attempt + 1}次滚动后选项仍在视口外，重试")
            entry = None
        if entry is None:
            self._log(f"  题{q.qid} 多次滚动后仍无法进入视口，跳过该题", "warn")
            return "skipped"
        if not self._click_in_viewport(entry["pos"], state.viewport, f"题{q.qid}选项{target}"):
            return "failed"
        self._sleep(random.uniform(0.5, 1.2))
        self._log(f"  题{q.qid} 已选择「{target}」")
        return "done"

    def _fill_blanks(self, state: QuizState, q: Question, answers: list[str]) -> str:
        if self._type_text is None or not q.blanks:
            self._log(f"  题{q.qid} 未找到可输入的填空控件，跳过", "warn")
            return "skipped"
        if self.dry_run:
            self._log(f"  [dry-run] 题{q.qid} {q.type} 将填写 {len(q.blanks)} 个空")
            return "planned"
        if not self._scroll_question_into_view(q.qid):
            return "skipped"
        self._sleep(random.uniform(0.3, 0.8))
        fresh = self._fresh_question(q.qid)
        if fresh is None:
            return "failed"
        blank_count = len(fresh.blanks)
        for index in range(blank_count):
            if index >= len(answers):
                break
            if self._evaluate(SCROLL_BLANK_JS % (json.dumps(q.qid), index)) != "ok":
                self._log(f"  题{q.qid} 第{index + 1}空无法滚动到视口", "warn")
                return "skipped"
            self._sleep(random.uniform(0.3, 0.7))
            fresh = self._fresh_question(q.qid)
            if fresh is None or index >= len(fresh.blanks):
                return "failed"
            blank = fresh.blanks[index]
            pos = blank.get("pos") if isinstance(blank, dict) else blank
            if not isinstance(pos, dict) or not self._click_in_viewport(pos, state.viewport, f"题{q.qid}空{index + 1}"):
                return "failed"
            self._sleep(random.uniform(0.2, 0.5))
            if not self._type_text(str(answers[index])):
                self._log(f"  题{q.qid} 第{index + 1}空输入失败", "warn")
                return "failed"
            self._sleep(random.uniform(0.3, 0.8))
        self._log(f"  题{q.qid} 已填写 {min(blank_count, len(answers))} 个空")
        return "done"

    # ---- 提交与验证 ----

    def _submit_and_wait(self, state: QuizState, qid_sample: str, attempted_qids: set[str]) -> str:
        """点卷尾提交并等待本轮已填写/选择的题 finished。"""
        if self._evaluate(SCROLL_SUBMIT_JS) != "ok":
            return "failed"
        self._sleep(random.uniform(0.4, 0.9))
        fresh = self._fresh_question(qid_sample)
        if fresh is None:
            return "failed"
        latest = self.read_state()
        if not latest.present or latest.modal or latest.start_required or latest.page_id != state.page_id:
            return "failed"
        by_id = {q.qid: q for q in latest.questions}
        if attempted_qids and all(qid in by_id and by_id[qid].finished for qid in attempted_qids):
            return "done"  # 平台已自动交卷，不再消耗一次提交。
        current = by_id.get(qid_sample)
        if current is None or current.submit is None:
            return "failed"
        if not self._click_in_viewport(current.submit, latest.viewport, "卷尾提交按钮"):
            return "failed"
        for _ in range(60):
            self._sleep(0.5)
            if not self._is_running():
                return "failed"
            check = self.read_state()
            if not check.present:
                return "done"
            if check.modal is not None:
                self.handle_modal(check.modal, {"modals": 0}, advance=True)
                continue
            by_id = {q.qid: q for q in check.questions}
            if attempted_qids and all(by_id.get(qid) is None or by_id[qid].finished for qid in attempted_qids):
                return "done"
        return "nomove"

    # ---- 主循环 ----

    def answer_all(
        self,
        *,
        option_label: str = "C",
        judgment_label: str = "错误",
        blank_text: str = ",",
        answer_choice: bool = True,
        answer_judgment: bool = True,
        answer_blank: bool = True,
        max_questions: int = 30,
        advance_on_modal: bool = True,
    ) -> dict:
        """处理当前页面的整套测验：选择全部可答题 → 卷尾统一提交。

        实测（调研 §5）：卷尾 submitQuiz 是整套提交，点一次全部判卷；
        因此本方法先逐题选择、最后只提交一次。
        """
        summary = {"done": 0, "skipped": 0, "failed": 0, "modals": 0}
        provider = FixedAnswerProvider(option_label, judgment_label, blank_text, answer_choice, answer_judgment, answer_blank)

        # 弹窗优先清空（可能残留上一页的章节统计弹窗）
        for _ in range(4):
            state = self.read_state()
            if not state.present or state.modal is None:
                break
            if not self.handle_modal(state.modal, summary, advance=advance_on_modal):
                break

        state = self.read_state()
        if not state.present:
            return summary
        if state.start_required:
            if not any((answer_choice, answer_judgment, answer_blank)):
                return summary
            if not self.ensure_started():
                summary["failed"] = 0 if self.dry_run else 1
                return summary

        # 阶段1：逐题选择（不提交）
        attempted: set[str] = set()
        planned_done = False
        for _ in range(max(1, max_questions)):
            if not self._is_running():
                return summary
            state = self.read_state()
            if not state.present:
                break
            if state.modal is not None:
                if not self.handle_modal(state.modal, summary, advance=advance_on_modal):
                    break
                continue
            def enabled(question: Question) -> bool:
                return provider.enabled(question)

            pending = next((
                q for q in state.questions
                if not q.finished and q.qid not in attempted and enabled(q)
            ), None)
            if pending is None:
                break
            attempted.add(pending.qid)
            outcome = provider.apply(self, state, pending)
            if outcome in ("done", "planned"):
                summary["done"] += 1
                if outcome == "planned":
                    planned_done = True
                    for q in state.questions:
                        if q.finished or q.qid in attempted or not enabled(q):
                            continue
                        provider.apply(self, state, q)
                        summary["done"] += 1
                    break
            elif outcome == "skipped":
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
                break
            self._sleep(random.uniform(1.2, 3.0))

        if planned_done or self.dry_run:
            return summary
        if summary["done"] == 0:
            # 没有任何可执行的选择或输入动作时，不提交空卷。
            return summary

        # 阶段2：卷尾统一提交
        state = self.read_state()
        if not state.present or not state.questions:
            return summary
        submitted_qids = {qid for qid in attempted if qid}
        result = self._submit_and_wait(state, state.questions[0].qid, submitted_qids)
        if result == "failed":
            summary["failed"] += 1
            self._log("提交失败，请人工检查", "warn")
            return summary
        if result == "nomove":
            summary["failed"] += 1
            self._log("提交后未确认到全部完成，请人工检查（可能仍有未答题）", "warn")
        return summary


class FixedAnswerProvider:
    """Legacy GUI policy, separate from reading and page actions."""

    def __init__(self, option="C", judgment="错误", blank=",", choice_enabled=True, judgment_enabled=True, blank_enabled=True):
        self.option, self.judgment, self.blank = option, judgment, blank
        self.choice_enabled, self.judgment_enabled, self.blank_enabled = choice_enabled, judgment_enabled, blank_enabled

    def enabled(self, question):
        return self.judgment_enabled if question.type == "判断题" else self.blank_enabled if question.type == "填空题" else self.choice_enabled

    def apply(self, handler, state, question):
        if question.type == "填空题":
            return handler._fill_blanks(state, question, [self.blank] * len(question.blanks))
        return handler._select_answer(state, question, self.option, self.judgment)


class QuizReader:
    """Only unfinished question semantics cross the Agent boundary."""

    def __init__(self, handler: QuizHandler):
        self.handler = handler

    def read(self) -> QuizState:
        return self.handler.read_state()

    @staticmethod
    def questions(state: QuizState) -> list[dict[str, Any]]:
        result = []
        for question in state.questions:
            if question.finished:
                continue
            kind = {"单选题": "single_choice", "判断题": "true_false", "填空题": "fill_blank"}.get(question.type, "unsupported")
            options = [{"id": str(c["label"]), "text": str(c.get("text") or "")} for c in question.choices] if kind == "single_choice" else []
            if kind == "single_choice" and (not options or len({c["id"] for c in options}) != len(options) or any(not c["id"] for c in options)):
                kind = "unsupported"
            if kind == "fill_blank" and not question.blanks:
                kind = "unsupported"
            schema = {"type": "array", "minItems": 1, "maxItems": 1, "uniqueItems": True, "items": {"enum": [c["id"] for c in options]}} if kind == "single_choice" else (
                {"type": "boolean"} if kind == "true_false" else
                {"type": "array", "minItems": len(question.blanks), "maxItems": len(question.blanks), "items": {"type": "string", "maxLength": 8192}}
                if kind == "fill_blank" else {"not": {}}
            )
            result.append({"id": question.qid, "type": kind, "sourceType": question.type,
                           "prompt": question.title, "options": options, "blankCount": len(question.blanks),
                           "hasMedia": question.has_media, "answerSchema": schema})
        return result


class QuizExecutor:
    """Validated per-question actions, refreshed targets, and exactly one submit attempt."""

    def __init__(self, handler: QuizHandler):
        self.handler = handler

    def execute(self, answers: dict[str, Any], guard, before_submit) -> dict[str, Any]:
        h = self.handler
        if h.dry_run:
            raise AgentError("QUIZ_APPLY_FAILED", "Page execution is disabled.")
        state = guard()
        if state.start_required:
            if not h.ensure_started():
                raise AgentError("QUIZ_APPLY_FAILED", "The timed quiz could not be started.")
            guard()
        for qid, value in answers.items():
            guard()
            if not h._scroll_question_into_view(qid):
                raise AgentError("QUIZ_APPLY_FAILED", "The question could not be located.")
            h._sleep(0.1)
            state = guard()
            question = next(q for q in state.questions if q.qid == qid)
            if question.type == "填空题":
                for index, text in enumerate(value):
                    focused = False
                    for _attempt in range(3):
                        if h._evaluate(SCROLL_BLANK_JS % (json.dumps(qid), index)) != "ok":
                            raise AgentError("QUIZ_APPLY_FAILED", "The blank could not be located.")
                        # 光标滚动和 scrollIntoView 可能跨多个合成帧竞争，每次都重读坐标并验焦点。
                        h._sleep(0.1)
                        state = guard()
                        fresh = next(q for q in state.questions if q.qid == qid)
                        blank = fresh.blanks[index]
                        existing = str(blank.get("value") or "")
                        if existing == text:
                            focused = True
                            break
                        if existing:
                            raise AgentError("QUIZ_APPLY_FAILED", "A blank already contains different text; manual review is required.")
                        if h._click_in_viewport(blank["pos"], state.viewport):
                            focused_state = guard()
                            focused_question = next(q for q in focused_state.questions if q.qid == qid)
                            if focused_question.blanks[index].get("focused"):
                                focused = True
                                break
                        h._sleep(0.2)
                    if not focused:
                        raise AgentError("QUIZ_APPLY_FAILED", "The blank input focus could not be verified.")
                    if existing == text:
                        continue
                    if h._type_text is None or not h._type_text(text):
                        raise AgentError("QUIZ_APPLY_FAILED", "Text input failed.")
                    # 输入后 Chromium 会异步把光标滚入视口；不能与下一空/提交按钮的滚动竞争。
                    h._sleep(0.2)
            else:
                label = ("正确" if value else "错误") if question.type == "判断题" else value[0]
                options = question.judgment if question.type == "判断题" else question.choices
                target = next((item for item in options if item["label"] == label), None)
                if target is None:
                    raise AgentError("QUIZ_APPLY_FAILED", "The answer target is unavailable.")
                if not target.get("selected") and not h._click_in_viewport(target["pos"], state.viewport):
                    raise AgentError("QUIZ_APPLY_FAILED", "The answer target could not be selected.")
        guard()
        if h._evaluate(SCROLL_SUBMIT_JS) != "ok":
            raise AgentError("QUIZ_SUBMIT_FAILED", "The submit button is unavailable.")
        h._sleep(0.1)
        state = guard()
        for question in state.questions:
            if question.qid not in answers:
                continue
            value = answers[question.qid]
            if question.type == "填空题":
                matches = [str(blank.get("value") or "") for blank in question.blanks] == value
            else:
                label = ("正确" if value else "错误") if question.type == "判断题" else value[0]
                options = question.judgment if question.type == "判断题" else question.choices
                matches = [item["label"] for item in options if item.get("selected")] == [label]
            if not matches:
                raise AgentError("QUIZ_APPLY_FAILED", "The applied answers could not be verified; nothing was submitted.")
        target = next((q.submit for q in state.questions if q.qid in answers and q.submit), None)
        if target is None:
            raise AgentError("QUIZ_SUBMIT_FAILED", "The submit button is unavailable.")
        state = before_submit()
        # The caret may asynchronously scroll the last blank back into view even
        # after SCROLL_SUBMIT_JS ran. Require two consecutive, matching submit
        # coordinates before spending the single trusted click attempt.
        target = None
        previous_point = None
        for attempt in range(4):
            if h._evaluate(SCROLL_SUBMIT_JS) != "ok":
                raise AgentError("QUIZ_SUBMIT_FAILED", "The submit target changed before execution.")
            h._sleep(0.1 * (attempt + 1))
            state = guard()
            candidate = next((q.submit for q in state.questions if q.qid in answers and q.submit), None)
            if candidate is None or candidate.get("pointMatches") is False or candidate.get("enabled") is False:
                previous_point = None
                continue
            point = (round(float(candidate["x"]), 1), round(float(candidate["y"]), 1))
            if previous_point is not None and all(abs(a - b) <= 1 for a, b in zip(point, previous_point)):
                target = candidate
                break
            previous_point = point
        if target is None:
            raise AgentError("QUIZ_SUBMIT_FAILED", "The submit target did not become stable before execution.")
        if not h._click_in_viewport(target, state.viewport):
            raise AgentError("QUIZ_SUBMIT_FAILED", "The single submit attempt failed.")
        for _ in range(50):
            if not h._is_running():
                raise AgentError("QUIZ_VERIFY_FAILED", "The course stopped before completion was verified.")
            check = h.read_state()
            if check.page_id != state.page_id or not check.present:
                raise AgentError("QUIZ_VERIFY_FAILED", "The question page disappeared before completion was verified.")
            questions = {q.qid: q for q in check.questions}
            if all(qid in questions and questions[qid].finished for qid in answers):
                return {"completedCount": len(answers), "submitAttempts": 1}
            h._sleep(0.2)
        raise AgentError("QUIZ_VERIFY_FAILED", "Question completion was not verified after the single submit attempt.")


# ---- 独立 CLI 后端：自带一条 CDP 连接，便于单独验证 ----


class StandaloneBackend:
    """quiz_probe 同款的最小 CDP 客户端；仅主文档，不附加 iframe。"""

    def __init__(
        self,
        port: int = PORT,
        dry_run: bool = True,
        log: Callable[..., None] = print,
        tab_keyword: str = TAB_KEYWORD,
    ) -> None:
        targets = json.loads(urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
        pages = [t for t in targets if t.get("type") == "page" and tab_keyword in (t.get("url") or "")]
        if not pages:
            raise RuntimeError("未找到 learnCourse 标签页，请先在浏览器里打开课程页面。")
        self.ws = create_connection(pages[0]["webSocketDebuggerUrl"], timeout=15)
        self._msg_id = 0
        self._dry_run = dry_run
        self._log = log

    def close(self) -> None:
        self.ws.close()

    def evaluate(self, expression: str):
        self._msg_id += 1
        self.ws.send(json.dumps({
            "id": self._msg_id,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        self.ws.settimeout(15)
        while True:
            raw = json.loads(self.ws.recv())
            if raw.get("id") == self._msg_id:
                if raw.get("error"):
                    raise RuntimeError(f"CDP 错误：{raw['error']}")
                return (raw.get("result") or {}).get("result", {}).get("value")

    def click(self, x: float, y: float) -> bool:
        if self._dry_run:
            self._log(f"  [dry-run] 点击 ({x:.0f}, {y:.0f})")
            return True
        events = (
            {"type": "mouseMoved", "x": x, "y": y},
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        for params in events:
            self._msg_id += 1
            self.ws.send(json.dumps({"id": self._msg_id, "method": "Input.dispatchMouseEvent", "params": params}))
            self.ws.settimeout(10)
            while True:
                raw = json.loads(self.ws.recv())
                if raw.get("id") == self._msg_id:
                    break
        return True

    def type_text(self, text: str) -> bool:
        if self._dry_run:
            self._log(f"  [dry-run] 输入文本「{text}」")
            return True
        self._msg_id += 1
        self.ws.send(json.dumps({
            "id": self._msg_id,
            "method": "Input.insertText",
            "params": {"text": text},
        }))
        self.ws.settimeout(10)
        while True:
            raw = json.loads(self.ws.recv())
            if raw.get("id") == self._msg_id:
                return raw.get("error") is None


def main() -> int:
    args = sys.argv[1:]
    dry_run = "--commit" not in args
    option_label = "C"
    judgment_label = "错误"
    port = PORT
    for arg in args:
        if arg.startswith("--option="):
            option_label = arg.split("=", 1)[1].upper()
        elif arg.startswith("--judgment="):
            judgment_label = arg.split("=", 1)[1]
        elif arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])

    backend = StandaloneBackend(port=port, dry_run=dry_run, log=print)
    handler = QuizHandler(
        evaluate=backend.evaluate,
        click=backend.click,
        type_text=backend.type_text,
        log=print,
        dry_run=dry_run,
    )
    state = handler.read_state()
    if not state.present:
        print("当前页面没有检测到测验 (.question-view)")
        return 1
    print(f"检测到 {len(state.questions)} 道题：")
    for q in state.questions:
        flag = "已答" if q.finished else "未答"
        print(f"  [{flag}] {q.type} {q.title[:50]}" + (f" (空×{len(q.blanks)})" if q.blanks else ""))
    if state.modal:
        print(f"检测到弹窗：{state.modal['text'][:60]}")
    print()
    summary = handler.answer_all(option_label=option_label, judgment_label=judgment_label)
    print(
        f"完成：done={summary['done']} skipped={summary['skipped']} "
        f"failed={summary['failed']} modals={summary['modals']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
