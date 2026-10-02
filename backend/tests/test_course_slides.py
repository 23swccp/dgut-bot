import unittest
from unittest.mock import Mock, patch

from dgutbot.course.course_slides import frame_point
from dgutbot.course.yxy_course import CourseController, CourseConfig, PagePlan


def slide(current=1, total=3, **kwargs):
    return dict(state='slides', resource='fixture', current=current, total=total,
                viewportWidth=800, viewportHeight=450, target={'x':600,'y':420}, **kwargs)


class SlideTests(unittest.TestCase):
    def error_state(self):
        return {**slide(), 'state': 'slides-error', 'target': None,
                'error': {'code': 'parse-failed', 'message': '课件播放器报告解析失败（平台端）'}}

    def test_player_error_stops_clicks_and_reports_once(self):
        c = self.controller()
        c._emit_event = Mock()
        error = self.error_state()
        for _ in range(4):
            result = c._advance_slide_document({'id': 'frame'}, error)
            c._handle_document_scroll_state({'id': 'frame'}, result)
        c.action_executor.execute_click.assert_not_called()
        self.assertEqual([v.args[0] for v in c._emit_event.call_args_list], ['RESOURCE_ERROR'])
        status = {'state': {'page': 'page-1', 'recordComplete': True},
                  'slideDocuments': [c._slide_progress[('page-1', 'fixture')]]}
        self.assertFalse(PagePlan.from_status(status).ready_for_navigation)
        self.assertEqual(PagePlan.from_status(status).tasks[0].state, 'error')
        self.assertEqual(c._completion_status(status), (False, ''))
        with patch.object(c, '_read_page_status', return_value=status):
            snapshot = c.status_snapshot()
        self.assertEqual(snapshot['resourceError']['code'], 'parse-failed')
        self.assertIn('课件错误', snapshot['currentTask'])

    def test_error_before_click_or_during_verification_is_reported(self):
        for during_click in (False, True):
            c = self.controller()
            c._advance_slide_document({'id': 'frame'}, slide())
            reads = [slide(), self.error_state()] if during_click else [self.error_state()]
            c._cdp_eval = Mock(side_effect=[{'result': {'value': v}} for v in reads])
            result = c._advance_slide_document({'id': 'frame'}, slide())
            self.assertEqual(result['state'], 'slides-error')
            self.assertEqual(c.action_executor.execute_click.call_count, int(during_click))
            self.assertEqual(c._slide_progress[('page-1', 'fixture')]['error']['code'], 'parse-failed')

    def test_error_clear_is_not_completion_and_allows_fresh_detection(self):
        c = self.controller()
        c._emit_event = Mock()
        c._advance_slide_document({'id': 'frame'}, self.error_state())
        result = c._advance_slide_document({'id': 'frame'}, slide())
        self.assertEqual(result['state'], 'slides-reading')
        self.assertFalse(c._slide_progress[('page-1', 'fixture')]['completed'])
        self.assertNotIn('error', c._slide_progress[('page-1', 'fixture')])
        self.assertEqual([v.args[0] for v in c._emit_event.call_args_list], ['RESOURCE_ERROR', 'RESOURCE_ERROR_CLEARED'])

    def test_pending_slides_reject_late_completion_and_generic_recovery(self):
        c = self.controller()
        status = {'state': {'page': 'page-1', 'recordComplete': True},
                  'slideDocuments': [{**self.error_state(), 'completed': False}]}
        c.status_snapshot = Mock(return_value=status)
        c._emit_event = Mock()
        for kind in ('static-ready', 'document-bottom', 'video-ended', 'course-finished'):
            c._handle_course_event({'type': kind, 'source': 'page-1', 'recordComplete': True})
        c._emit_event.assert_not_called()
        c.eval_js.reset_mock()
        c._recover_stall(status)
        c.eval_js.assert_not_called()
        self.assertFalse(c._progress_resolves_recovery(status))

    def test_ready_slides_use_fast_delay_without_speeding_up_other_documents(self):
        self.assertEqual(CourseController._document_poll_delay(3, [{'state':'slides-reading'}]), 0.15)
        for states in ([], [{}], [{'state':'slides-loading'}], [{'state':'slides-wait'}],
                       [{'state':'slides-complete'}], [{'state':'scrolled'}],
                       [{'state':'slides-reading'}, {'state':'scrolled'}]):
            self.assertEqual(CourseController._document_poll_delay(3, states), 3)

    def test_document_loop_actually_uses_fast_slide_delay(self):
        c = self.controller()
        c.status_snapshot = Mock(return_value={'state':{'page':'page-1'}, 'hasDocumentFrame':True})
        c._document_targets = Mock(return_value=[{'id':'frame'}])
        c._scroll_document_target = Mock(return_value={**slide(), 'state':'slides-reading', 'page':'page-1'})
        def stop_after_round(delay):
            c._running = False
            return True
        c._stop_event.wait = Mock(side_effect=stop_after_round)
        c._document_scroll_loop(3)
        c._stop_event.wait.assert_called_once_with(0.15)

    def controller(self):
        c = CourseController(lambda *_: None)
        c._running = True
        c._active_config = CourseConfig(auto_next=False)
        c._read_bootstrap_state = Mock(return_value={'page':'page-1'})
        c._quiz_busy_now = Mock(return_value=False)
        c.eval_js = Mock()
        c._slide_target_point = Mock(return_value=(700, 500))
        c.action_executor.execute_click = Mock(return_value=True)
        c._stop_event.wait = Mock(return_value=False)
        return c

    def test_completed_course_record_does_not_skip_pending_slides(self):
        status = {'state':{'recordComplete':True},'slideDocuments':[{'current':1,'total':53,'completed':False}]}
        self.assertFalse(PagePlan.from_status(status).ready_for_navigation)
        self.assertEqual(PagePlan.from_status(status).active_kind, 'document')
        status['slideDocuments'][0]['completed'] = True
        self.assertTrue(PagePlan.from_status(status).ready_for_navigation)

    def test_iframe_coordinates_include_offset_and_css_scale(self):
        self.assertEqual(frame_point([100,80,500,80,500,305,100,305], slide()), (400,290))
        self.assertIsNone(frame_point([0,0,400,10,400,225,0,225], slide()))
        self.assertIsNone(frame_point(None, slide()))

    def test_first_observation_waits_then_one_verified_step(self):
        c = self.controller()
        item={'id':'frame'}
        self.assertEqual(c._advance_slide_document(item,slide())['state'],'slides-reading')
        c.action_executor.execute_click.assert_not_called()
        c._cdp_eval = Mock(side_effect=[{'result':{'value':slide()}},{'result':{'value':slide(2)}}])
        self.assertEqual(c._advance_slide_document(item,slide())['current'],2)
        c.action_executor.execute_click.assert_called_once_with(700,500)
        c._stop_event.wait.assert_called_once_with(0.05)

    def test_last_slide_needs_stable_read_and_never_clicks_next(self):
        c = self.controller()
        for expected in ['slides-reading','slides-complete']:
            self.assertEqual(c._advance_slide_document({'id':'frame'},slide(3))['state'],expected)
        c.action_executor.execute_click.assert_not_called()

    def test_failed_click_verification_is_bounded_and_not_completed(self):
        c=self.controller()
        c._cdp_eval=Mock(return_value={'result':{'value':slide()}})
        c._advance_slide_document({'id':'frame'},slide())
        for _ in range(5):
            self.assertEqual(c._advance_slide_document({'id':'frame'},slide())['state'],'slides-wait')
        self.assertEqual(c.action_executor.execute_click.call_count,3)
        self.assertFalse(c._slide_progress[('page-1','fixture')]['completed'])

    def test_loading_or_missing_button_never_completes(self):
        c=self.controller()
        state={**slide(), 'state':'slides-loading','target':None}
        for _ in range(4):
            self.assertEqual(c._advance_slide_document({'id':'frame'},state)['state'],'slides-loading')
        c.action_executor.execute_click.assert_not_called()
        events=Mock()
        c._enqueue_course_event=events
        c._handle_document_scroll_state({'id':'frame'},state)
        events.assert_not_called()

    def test_page_changes_before_click_cancel_action(self):
        c=self.controller()
        c._advance_slide_document({'id':'frame'},slide())
        c._cdp_eval=Mock(return_value={'result':{'value':slide()}})
        c._read_bootstrap_state=Mock(side_effect=[{'page':'page-1'},{'page':'page-2'}])
        self.assertEqual(c._advance_slide_document({'id':'frame'},slide())['state'],'slides-wait')
        c.action_executor.execute_click.assert_not_called()

    def test_other_tab_iframe_is_not_attached(self):
        c=self.controller()
        with patch.object(c,'_cdp_call',side_effect=[{'targetInfos':[{'targetId':'other','type':'iframe','url':'https://docs.ulearning.cn/'}]},None]) as call:
            self.assertEqual(c._document_targets(),[])
        self.assertNotIn('Target.attachToTarget',[args.args[0] for args in call.call_args_list])
