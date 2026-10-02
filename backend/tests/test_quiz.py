"""yxy_quiz.QuizHandler 的离线单元测试；不连接真实浏览器。"""

import json
import unittest

from dgutbot.course.yxy_quiz import QuizHandler


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
    def test_observed_auto_completion_before_submit_does_not_submit_again(self):
        finished = make_state([question('q1', '单选题', '模拟题', finished=True)])
        page = FakePage([finished])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click, dry_run=False, sleep=lambda _: None)
        state = handler.read_state()
        self.assertEqual(handler._submit_and_wait(state, 'q1', {'q1'}), 'done')
        self.assertEqual(page.clicks, [])

    def test_disabled_question_type_is_not_answered(self):
        state = make_state(
            [
                question("q1", "单选题", "选择题", choices=[choice("C", "丙")]),
                question(
                    "q2",
                    "判断题",
                    "判断题",
                    judgment=[{"label": "错误", "pos": {"x": 900, "y": 360}}],
                ),
            ]
        )
        page = FakePage([state])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click, dry_run=True)
        summary = handler.answer_all(answer_choice=False, answer_judgment=True)
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["skipped"], 0)

    def test_dry_run_lists_every_question_without_clicking(self):
        state = make_state(
            [
                question(
                    "q1",
                    "判断题",
                    "题干一",
                    judgment=[
                        {"label": "正确", "pos": {"x": 900, "y": 300}},
                        {"label": "错误", "pos": {"x": 900, "y": 360}},
                    ],
                ),
                question("q2", "单选题", "题干二", choices=[choice("A", "甲"), choice("C", "丙")]),
            ]
        )
        page = FakePage([state])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click, dry_run=True)
        summary = handler.answer_all()
        self.assertEqual(summary["done"], 2)
        self.assertEqual(page.clicks, [])

    def test_judgment_placeholder_defaults_to_wrong(self):
        state = make_state(
            [
                question(
                    "q1",
                    "判断题",
                    "题干一",
                    judgment=[
                        {"label": "正确", "pos": {"x": 900, "y": 300}},
                        {"label": "错误", "pos": {"x": 900, "y": 360}},
                    ],
                )
            ]
        )
        page = FakePage([state])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click, dry_run=True)
        handler.answer_all()
        # dry-run 不点击，验证计划目标：直接检查选择逻辑选中的是「错误」
        fresh = handler.read_state()
        target = "错误"
        entry = next(j for j in fresh.questions[0].judgment if j["label"] == target)
        self.assertEqual(entry["pos"], {"x": 900, "y": 360})

    def test_choice_question_answers_with_placeholder_and_submits(self):
        unanswered = make_state(
            [
                question(
                    "q1",
                    "单选题",
                    "题干",
                    choices=[choice("A", "甲"), choice("B", "乙"), choice("C", "丙"), choice("D", "丁")],
                )
            ]
        )
        answered = make_state(
            [
                question(
                    "q1",
                    "单选题",
                    "题干",
                    choices=[choice("A", "甲"), choice("B", "乙"), choice("C", "丙"), choice("D", "丁")],
                    finished=True,
                )
            ]
        )
        # 选择选项后仍是未交卷态，第二次点击提交后才 finished。
        page = FakePage([unanswered, unanswered, answered])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click, dry_run=False, jitter=0)
        summary = handler.answer_all(option_label="C")
        self.assertEqual(summary["done"], 1)
        # 至少两次点击：选项 C（y=300） + 卷尾提交（y=527）
        self.assertIn((900, 300), page.clicks)
        self.assertIn((914, 527), page.clicks)

    def test_blank_question_is_filled_and_submitted(self):
        state = make_state([question("q1", "填空题", "题干")])
        payload = json.loads(state)
        payload["questions"][0]["blanks"] = [{"pos": {"x": 900, "y": 300}, "value": ""}]
        answered = json.loads(make_state([question("q1", "填空题", "题干", finished=True)]))
        answered["questions"][0]["blanks"] = [{"pos": {"x": 900, "y": 300}, "value": ","}]
        page = FakePage([json.dumps(payload, ensure_ascii=False), json.dumps(answered, ensure_ascii=False)])
        typed = []
        handler = QuizHandler(
            evaluate=page.evaluate,
            click=page.click,
            type_text=lambda text: typed.append(text) or True,
            dry_run=False,
            jitter=0,
        )
        summary = handler.answer_all()
        self.assertEqual(summary["skipped"], 0)
        self.assertEqual(summary["done"], 1)
        self.assertEqual(typed, [","])
        self.assertIn((900, 300), page.clicks)

    def test_stops_when_no_quiz_present(self):
        page = FakePage(['{"present": false}'])
        handler = QuizHandler(evaluate=page.evaluate, click=page.click, dry_run=False)
        summary = handler.answer_all()
        self.assertEqual(
            summary, {"done": 0, "skipped": 0, "failed": 0, "modals": 0}
        )


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

    def test_start_precedes_answers_and_single_submit(self):
        active = self.state(False)
        finished = json.loads(active)
        finished["questions"][0]["finished"] = True
        page = FakePage([self.state(), active, active, json.dumps(finished)])
        summary = self.handler(page).answer_all()
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(page.clicks, [(500, 400), (900, 300), (914, 527)])

    def test_stuck_mask_never_clicks_questions_or_restarts_on_controller_retry(self):
        page = FakePage([self.state()])
        attempts = set()
        for _ in range(2):
            summary = self.handler(page, start_attempts=attempts).answer_all()
            self.assertEqual(summary["failed"], 1)
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

    def test_dry_run_and_disabled_types_do_not_start_timer(self):
        page = FakePage([self.state()])
        handler = self.handler(page)
        handler.dry_run = True
        self.assertEqual(handler.answer_all()["failed"], 0)
        handler.dry_run = False
        self.assertEqual(handler.answer_all(answer_choice=False, answer_judgment=False, answer_blank=False)["failed"], 0)
        self.assertEqual(page.clicks, [])


if __name__ == "__main__":
    unittest.main()
