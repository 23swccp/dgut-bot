"""弹窗执行策略离线测试；真实 DOM 分支另由 quiz_simulator 覆盖。"""

import unittest
from unittest.mock import Mock

from dgutbot.course.course_dialogs import handle_dialog
from dgutbot.course.yxy_course import PagePlan, CourseController, CourseConfig
from dgutbot.course.yxy_quiz import QuizHandler


def dialog(kind='suspend', policy='resume', page='test-page'):
    return dict(type=kind, policy=policy, page=page, signature=f'{page}:{kind}',
                title='测试提示', buttons=['继续学习'],
                target=dict(x=50, y=50, pointMatches=True, disabled=False))


class DialogTests(unittest.TestCase):
    def execute(self, read, ledger=None, **kwargs):
        click = Mock(return_value=True)
        result = handle_dialog(read, click, lambda _: None, lambda: True,
                               ledger if ledger is not None else {}, **kwargs)
        return result, click

    def test_read_failure_is_not_success(self):
        result, click = self.execute(Mock(side_effect=[{'dialog': dialog()}] + [None] * 10))
        self.assertEqual(result[0], 'unverified')
        click.assert_called_once()

    def test_same_dialog_remaining_is_not_success(self):
        result, click = self.execute(Mock(return_value={'dialog': dialog()}))
        self.assertEqual(result[0], 'unverified')
        click.assert_called_once()

    def test_changed_dialog_before_action_does_not_use_stale_coordinates(self):
        result, click = self.execute(Mock(return_value={'dialog': dialog('autoSubmit', 'ack')}), expected='old')
        self.assertEqual(result[0], 'changed')
        click.assert_not_called()

    def test_next_dialog_is_left_to_next_iteration(self):
        result, click = self.execute(Mock(side_effect=[{'dialog': dialog()}, {'dialog': dialog('autoSubmit', 'ack')}]))
        self.assertEqual(result[0], 'dismissed')
        click.assert_called_once()

    def test_reappearing_dialog_does_not_reset_retry_budget(self):
        ledger = {}
        for attempt in range(5):
            result, click = self.execute(Mock(side_effect=[{'dialog': dialog()}, {'dialog': None}]), ledger)
            self.assertEqual(result[0], 'dismissed' if attempt < 3 else 'exhausted')
            self.assertEqual(click.call_count, int(attempt < 3))
        self.assertEqual(ledger, {('test-page', 'suspend'): 3})
        result, _ = self.execute(Mock(side_effect=[{'dialog': dialog(page='next-page')}, {'dialog': None}]), ledger)
        self.assertEqual(result[0], 'dismissed')

    def test_no_background_completion_under_unknown_dialog(self):
        plan = PagePlan.from_status({'state': {'recordComplete': True}, 'dialogState': dialog('new', 'blocked')})
        self.assertFalse(plan.ready_for_navigation)
        self.assertEqual(plan.active_kind, 'dialog')

    def test_navigation_is_not_a_generic_dismiss_action(self):
        result, click = self.execute(Mock(return_value={'dialog': dialog('incomplete', 'navigation')}))
        self.assertEqual(result[0], 'blocked')
        click.assert_not_called()

    def test_unknown_and_occluded_never_click(self):
        for item in [dialog('unknown', 'blocked'), {**dialog(), 'target': None}]:
            result, click = self.execute(Mock(return_value={'dialog': item}))
            self.assertIn(result[0], ['blocked', 'unavailable'])
            click.assert_not_called()

    def test_invalid_coordinates_never_click(self):
        for pos in ({}, {'x': -1, 'y': 2}, {'x': float('nan'), 'y': 2}):
            item = {**dialog(), 'target': {**pos, 'pointMatches': True}}
            result, click = self.execute(Mock(return_value={'dialog': item}))
            self.assertEqual(result[0], 'unavailable')
            click.assert_not_called()

    def test_quiz_dry_run_never_dismisses_dialog(self):
        click = Mock()
        handler = QuizHandler(evaluate=Mock(), click=click, dry_run=True)
        self.assertFalse(handler.handle_modal(dialog(), {'modals': 0}))
        click.assert_not_called()

    def test_quiz_obeys_dismiss_switch(self):
        read, click = Mock(), Mock()
        handler = QuizHandler(evaluate=read, click=click, dry_run=False, auto_dismiss_dialog=False)
        self.assertFalse(handler.handle_modal(dialog(), {'modals': 0}))
        read.assert_not_called()
        click.assert_not_called()

    def test_controller_does_not_interleave_clicks_with_quiz(self):
        controller = CourseController(lambda *_: None)
        controller._running = True
        controller._quiz_busy = True
        controller._active_config = CourseConfig()
        controller.eval_js = Mock()
        self.assertFalse(controller._handle_classified_dialog())
        controller.eval_js.assert_not_called()

    def test_unknown_warning_is_deduplicated_and_does_not_need_manual_confirmation(self):
        events = []
        controller = CourseController(lambda *_: None, emit_event=lambda *args, **kwargs: events.append(args))
        controller._running = True
        controller._active_config = CourseConfig()
        controller.eval_js = Mock(return_value={'dialog': dialog('new', 'blocked')})
        for _ in range(4):
            self.assertFalse(controller._handle_classified_dialog())
        self.assertEqual(len(events), 1)
        self.assertTrue(controller._running)
