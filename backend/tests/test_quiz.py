"""yxy_quiz.QuizHandler 的离线单元测试；不连接真实浏览器。"""

import json
import unittest

from dgutbot.course.yxy_quiz import QuizHandler, QuizReader, QuizExecutor
from dgutbot.agent.protocol import AgentError


def make_state(questions, modal=None, viewport=None):
    return json.dumps(
        {
            "present": True,
            "viewport": viewport or {"w": 1528, "h": 779},
            "questions": questions,
            "modal": modal,
        },
        ensure_ascii=False,
    )


def choice(label, text, x=900, y=300):
    return {"label": label, "text": f"{label}. {text}", "pos": {"x": x, "y": y}}


def question(qid, qtype, title, choices=None, judgment=None, finished=False, submit=None):
    return {
        "qid": qid,
        "finished": finished,
        "type": qtype,
        "title": title,
        "choices": choices or [],
        "judgment": judgment or [],
        "blanks": [],
        "submit": submit or {"x": 914, "y": 527},
    }


class FakePage:
    """点击驱动的假页面：读取始终返回当前状态，每次点击推进到下一个状态。"""

    def __init__(self, states):
        self.states = list(states)
        self.index = 0
        self.clicks = []

    def evaluate(self, _expression):
        if "scrollIntoView" in _expression:
            return "ok"
        return self.states[self.index]

    def click(self, x, y):
        self.clicks.append((round(x), round(y)))
        if self.index < len(self.states) - 1:
            self.index += 1
        return True


class ModalTests(unittest.TestCase):
    def test_forward_button_preferred_for_advance(self):
        modal = {
            "text": "本页面还有题目没有完成",
            "buttons": [
                {"text": "留在本页", "pos": {"x": 100, "y": 100}},
                {"text": "确定离开", "pos": {"x": 200, "y": 200}},
            ],
        }
        modal.update(type='incomplete', policy='navigation', page='test', signature='leave', title=modal['text'],
                     target={'x': 200, 'y': 200, 'pointMatches': True})
        page = FakePage([{'dialog': modal}, {'dialog': None}])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click, dry_run=False, jitter=0, sleep=lambda _: None)
        summary = {"modals": 0}
        self.assertTrue(handler.handle_modal(modal, summary, advance=True))
        self.assertEqual(page.clicks, [(200, 200)])

    def test_chapter_stat_modal_forward_is_next_chapter(self):
        modal = {
            "text": "本章成绩 0分",
            "buttons": [
                {"text": "<< 留在本页", "pos": {"x": 100, "y": 100}},
                {"text": "继续下一章 >>", "pos": {"x": 200, "y": 200}},
            ],
        }
        modal.update(type='statistics', policy='navigation', page='test', signature='stat', title=modal['text'],
                     target={'x': 200, 'y': 200, 'pointMatches': True})
        page = FakePage([{'dialog': modal}, {'dialog': None}])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click, dry_run=False, jitter=0, sleep=lambda _: None)
        self.assertTrue(handler.handle_modal(modal, {"modals": 0}, advance=True))
        self.assertEqual(page.clicks, [(200, 200)])

    def test_unknown_buttons_are_rejected(self):
        modal = {"text": "?", "buttons": [{"text": "随便", "pos": {"x": 1, "y": 1}}]}
        modal.update(type='unknown', policy='blocked', page='test', signature='unknown')
        page = FakePage([{'dialog': modal}])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click, dry_run=False)
        self.assertFalse(handler.handle_modal(modal, {"modals": 0}))
        self.assertEqual(page.clicks, [])

    def test_suspend_modal_single_button_is_clicked(self):
        # 按用途恢复学习，不把关闭弹窗当成导航成功。
        modal = {
            "text": "计时学习已暂停 走神太久啦",
            "buttons": [{"text": "继续学习", "pos": {"x": 764, "y": 514}}],
        }
        modal.update(type='suspend', policy='resume', page='test', signature='suspend', title=modal['text'],
                     target={'x': 764, 'y': 514, 'pointMatches': True})
        page = FakePage([{'dialog': modal}, {'dialog': None}])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click, dry_run=False, jitter=0, sleep=lambda _: None)
        self.assertTrue(handler.handle_modal(modal, {"modals": 0}, advance=True))
        self.assertEqual(page.clicks, [(764, 514)])


class AnswerFlowTests(unittest.TestCase):
    def test_reader_exposes_unfinished_question_semantics_without_clicking(self):
        page = FakePage([make_state([
            question("q1", "单选题", "合成问题", choices=[choice("A", "甲"), choice("B", "乙")]),
            question("q2", "判断题", "已完成", finished=True),
        ])])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click, dry_run=False)
        questions = QuizReader.questions(handler.read_state())
        self.assertEqual([q["id"] for q in questions], ["q1"])
        self.assertEqual(questions[0]["type"], "single_choice")
        self.assertEqual(questions[0]["answerSchema"]["items"]["enum"], ["A", "B"])
        self.assertNotIn("pos", json.dumps(questions))
        self.assertEqual(page.clicks, [])

    def test_executor_dry_run_cannot_modify_or_submit(self):
        page = FakePage([make_state([question("q1", "单选题", "合成问题")])])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click, dry_run=True)
        with self.assertRaises(AgentError):
            QuizExecutor(handler).execute({"q1": ["A"]}, handler.read_state, handler.read_state)
        self.assertEqual(page.clicks, [])

    def test_stops_when_no_quiz_present(self):
        page = FakePage(['{"present": false}'])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click)
        self.assertFalse(handler.read_state().present)
        self.assertEqual(QuizReader.questions(handler.read_state()), [])
        self.assertEqual(page.clicks, [])


class TimedQuizTests(unittest.TestCase):
    @staticmethod
    def state(mask=True, page_id="timed-page", **target):
        data = json.loads(make_state([question("q1", "单选题", "模拟题", choices=[choice("C", "丙")])]))
        data.update(pageId=page_id, startRequired=mask,
                    startButton={"x": 500, "y": 400, "enabled": True, "pointMatches": True, **target} if mask else None)
        return json.dumps(data)

    @staticmethod
    def handler(page, **kwargs):
        return QuizHandler(evaluate=page.evaluate, click=page.click, sleep=lambda _: None,
                           log=lambda *_: None, dry_run=False, jitter=0, **kwargs)

    def test_start_only_clicks_verified_start_button(self):
        page = FakePage([self.state(), self.state(False)])
        self.assertTrue(self.handler(page).ensure_started())
        self.assertEqual(page.clicks, [(500, 400)])

    def test_stuck_mask_never_clicks_questions_or_restarts_on_controller_retry(self):
        page = FakePage([self.state()])
        attempts = set()
        for _ in range(2):
            self.assertFalse(self.handler(page, start_attempts=attempts).ensure_started())
        self.assertEqual(page.clicks, [(500, 400)])

    def test_disabled_or_obstructed_start_never_clicks(self):
        for target in ({"enabled": False}, {"pointMatches": False}):
            with self.subTest(target=target):
                page = FakePage([self.state(**target)])
                self.assertFalse(self.handler(page).ensure_started())
                self.assertEqual(page.clicks, [])

    def test_navigation_or_disappearance_is_not_start_success(self):
        for after in (self.state(False, page_id="other-page"), '{"present":false}'):
            with self.subTest(after=after):
                page = FakePage([self.state(), after])
                self.assertFalse(self.handler(page).ensure_started())
                self.assertEqual(page.clicks, [(500, 400)])

    def test_dry_run_does_not_start_timer(self):
        page = FakePage([self.state()])
        handler = self.handler(page)
        handler.dry_run = True
        self.assertFalse(handler.ensure_started())
        self.assertEqual(page.clicks, [])


if __name__ == "__main__":
    unittest.main()
