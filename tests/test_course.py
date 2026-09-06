"""课程页辅助的离线单元测试；不连接真实浏览器。"""

import json
import unittest
from unittest.mock import Mock, patch

from dgutbot.course.yxy_course import (
    COURSE_TAB_URL_KEYWORD,
    INJECT_JS,
    FIRST_PAGE_TARGET_JS,
    ActionExecutor,
    ActionResult,
    CourseConfig,
    CourseController,
    PagePlan,
    CourseState,
    CourseStateMachine,
    TemplateMatch,
    template_match_to_css,
)


class CourseConfigTests(unittest.TestCase):
    def test_config_serializes_supported_controls_only(self):
        config = CourseConfig(playback_rate=4.0, document_scroll_speed=2.0)
        values = json.loads(config.to_js())
        self.assertEqual(values["playback_rate"], 4.0)
        self.assertEqual(values["document_scroll_speed"], 2.0)
        self.assertEqual(values["quiz_blank_text"], ",")
        self.assertNotIn("auto_answer", values)
        self.assertNotIn("mouse_interval_min", values)
        self.assertNotIn("mouse_interval_max", values)


class CourseStateMachineTests(unittest.TestCase):
    def test_legal_state_transitions(self):
        machine = CourseStateMachine()
        machine.transition(CourseState.ATTACHING)
        machine.transition(CourseState.LOADING)
        machine.observe_video_ready()
        machine.observe_video_playing()
        self.assertTrue(machine.mark_content_finished("video"))
        self.assertEqual(machine.state, CourseState.WAITING_PAGE_CONFIRM)
        self.assertTrue(machine.mark_next_ready())
        self.assertTrue(machine.begin_navigation())
        machine.navigation_succeeded("chapter-2")
        self.assertEqual(machine.state, CourseState.LOADING)
        self.assertEqual(machine.chapter_key, "chapter-2")

    def test_invalid_transition_is_rejected(self):
        machine = CourseStateMachine()
        with self.assertRaises(ValueError):
            machine.transition(CourseState.VIDEO_PLAYING)

    def test_same_completion_only_navigates_once(self):
        machine = CourseStateMachine()
        machine.transition(CourseState.ATTACHING)
        machine.transition(CourseState.LOADING)
        machine.observe_video_ready()
        machine.observe_video_playing()
        self.assertTrue(machine.mark_content_finished("video"))
        self.assertFalse(machine.mark_content_finished("video"))
        self.assertTrue(machine.mark_next_ready())
        self.assertTrue(machine.begin_navigation())
        self.assertFalse(machine.begin_navigation())

    def test_video_and_document_cannot_both_finish_one_generation(self):
        machine = CourseStateMachine()
        machine.transition(CourseState.ATTACHING)
        machine.transition(CourseState.LOADING)
        machine.observe_video_ready()
        machine.observe_video_playing()
        self.assertFalse(machine.mark_content_finished("document"))
        self.assertTrue(machine.mark_content_finished("video"))

    def test_late_completion_from_old_media_source_is_ignored(self):
        machine = CourseStateMachine()
        machine.transition(CourseState.ATTACHING)
        machine.transition(CourseState.LOADING)
        machine.observe_video_ready("video-2")
        machine.observe_video_playing("video-2")
        self.assertFalse(machine.mark_content_finished("video", "video-1"))
        self.assertTrue(machine.mark_content_finished("video", "video-2"))

    def test_already_ended_video_can_finish_from_ready_state(self):
        machine = CourseStateMachine()
        machine.transition(CourseState.ATTACHING)
        machine.transition(CourseState.LOADING)
        machine.observe_video_ready("video-1")
        self.assertTrue(machine.mark_content_finished("video", "video-1"))
        self.assertEqual(machine.state, CourseState.WAITING_PAGE_CONFIRM)

    def test_chapter_change_resets_old_completion(self):
        machine = CourseStateMachine()
        machine.transition(CourseState.ATTACHING)
        machine.transition(CourseState.LOADING)
        machine.observe_document_reading()
        self.assertTrue(machine.mark_content_finished("document"))
        machine.mark_next_ready()
        machine.begin_navigation()
        machine.navigation_succeeded("chapter-2")
        machine.observe_document_reading()
        self.assertTrue(machine.mark_content_finished("document"))

    def test_static_page_can_finish_and_navigate(self):
        machine = CourseStateMachine()
        machine.transition(CourseState.ATTACHING)
        machine.transition(CourseState.LOADING)
        self.assertTrue(machine.observe_static_ready("page-1"))
        self.assertTrue(machine.mark_content_finished("static", "page-1"))
        self.assertEqual(machine.state, CourseState.WAITING_PAGE_CONFIRM)

    def test_completed_record_can_skip_an_already_playing_page(self):
        machine = CourseStateMachine()
        machine.transition(CourseState.ATTACHING)
        machine.transition(CourseState.LOADING)
        machine.observe_video_ready("page-1")
        machine.observe_video_playing("page-1")
        self.assertTrue(machine.mark_record_complete("page-1"))
        self.assertEqual(machine.state, CourseState.WAITING_PAGE_CONFIRM)

    def test_video_can_handoff_to_document_on_the_same_page(self):
        machine = CourseStateMachine()
        machine.transition(CourseState.ATTACHING)
        machine.transition(CourseState.LOADING)
        machine.observe_video_ready("video-1")
        machine.observe_video_playing("video-1")
        self.assertTrue(machine.prepare_document_after_video())
        self.assertTrue(machine.observe_document_reading("document-1"))
        self.assertTrue(machine.mark_content_finished("document", "document-1"))


class PagePlanTests(unittest.TestCase):
    def test_empty_loading_quiz_blocks_navigation(self):
        plan = PagePlan.from_status({"state": {"recordComplete": False}, "quizLoading": True, "quizUnfinished": 0})
        self.assertFalse(plan.ready_for_navigation)
        self.assertEqual(plan.active_kind, "quiz")

    def test_completed_record_is_ready_without_replaying_video(self):
        plan = PagePlan.from_status({"state": {"recordComplete": True}, "videos": [{"ended": False}]})
        self.assertTrue(plan.ready_for_navigation)
        self.assertEqual(plan.active_kind, "navigation")
        self.assertEqual(plan.tasks[0].kind, "record")

    def test_video_document_quiz_are_composed_and_quiz_has_interaction_priority(self):
        plan = PagePlan.from_status({
            "state": {"recordComplete": False},
            "videos": [{"ended": False}],
            "hasDocument": True,
            "quizUnfinished": 3,
            "quizTypes": {"单选题": 1, "判断题": 1, "填空题": 1},
        })
        self.assertEqual([task.kind for task in plan.tasks], ["video", "document", "quiz"])
        self.assertEqual(plan.active_kind, "quiz")
        self.assertFalse(plan.ready_for_navigation)
        self.assertEqual(plan.tasks[-1].types, {"单选题": 1, "判断题": 1, "填空题": 1})

    def test_video_finished_but_document_pending_cannot_navigate(self):
        plan = PagePlan.from_status({
            "state": {"recordComplete": False},
            "videos": [{"ended": True}],
            "hasDocumentFrame": True,
            "quizUnfinished": 0,
        })
        self.assertEqual(plan.active_kind, "document")
        self.assertFalse(plan.ready_for_navigation)

    def test_timed_quiz_mask_blocks_navigation_even_with_stale_complete_record(self):
        plan = PagePlan.from_status({
            "state": {"recordComplete": True},
            "quizStartPending": True,
            "quizUnfinished": 0,
        })
        self.assertEqual(plan.tasks[0].kind, "quiz")
        self.assertEqual(plan.active_kind, "quiz")
        self.assertFalse(plan.ready_for_navigation)

    def test_network_prompt_is_not_page_completion(self):
        plan = PagePlan.from_status({"state": {"recordComplete": True}, "networkDialogPending": True})
        self.assertEqual(plan.active_kind, "network")
        self.assertFalse(plan.ready_for_navigation)


class InjectScriptTests(unittest.TestCase):
    def test_disabled_timed_quiz_skip_is_not_completion(self):
        status = {'state': {'recordComplete': False}, 'quizStartPending': True,
                  'quizSkipBeforeStart': True, 'quizUnfinished': 18}
        plan = PagePlan.from_status(status)
        self.assertTrue(plan.ready_for_navigation)
        self.assertEqual(plan.tasks[0].state, 'skipped')
        controller = CourseController(lambda *_: None)
        self.assertEqual(controller._completion_status(status), (False, ''))
        status['quizStartPending'] = False
        self.assertFalse(PagePlan.from_status(status).ready_for_navigation)

    def test_skipping_timed_quiz_still_waits_for_other_page_tasks(self):
        status = {'quizStartPending': True, 'quizSkipBeforeStart': True, 'quizUnfinished': 18,
                  'videos': [{'ended': False}]}
        plan = PagePlan.from_status(status)
        self.assertFalse(plan.ready_for_navigation)
        self.assertEqual(plan.active_kind, 'video')

    def test_disabled_answering_never_launches_quiz_worker(self):
        controller = CourseController(lambda *_: None)
        controller._active_config = CourseConfig(quiz_auto_answer=False)
        with patch('yxy_course.threading.Thread') as worker:
            controller._on_quiz_appeared({'unfinished': 18})
        worker.assert_not_called()
        self.assertFalse(controller._quiz_busy)

    def test_busy_quiz_worker_does_not_launch_a_second_page_handler(self):
        controller = CourseController(lambda *_: None)
        controller._active_config = CourseConfig(quiz_auto_answer=True, quiz_mode="ai")
        controller._quiz_busy = True
        with patch('yxy_course.threading.Thread') as worker:
            controller._on_quiz_appeared({'unfinished': 3})
        worker.assert_not_called()
        self.assertTrue(controller._quiz_busy)

    def test_queued_quiz_worker_rechecks_answering_setting(self):
        controller = CourseController(lambda *_: None)
        controller._active_config = CourseConfig(quiz_auto_answer=False)
        controller._quiz_busy = True
        with patch('yxy_course.QuizHandler') as handler:
            controller._run_quiz_handler()
        handler.assert_not_called()
        self.assertFalse(controller._quiz_busy)

    def test_ai_fallback_uses_fixed_answers_once_and_stops_on_action_failure(self):
        controller = CourseController(lambda *_: None)
        controller._active_config = CourseConfig(quiz_mode="ai")
        controller._session_id = "session"
        controller._running = True
        controller._quiz_busy = True
        controller._ai_answer_provider = Mock()
        controller._ai_answer_provider.answer.return_value = {"state": "fallback", "reason": "invalid JSON"}
        controller.eval_js = Mock(return_value=True)
        controller.stop = Mock()
        handler = Mock()
        handler.answer_all.return_value = {"done": 1, "skipped": 0, "failed": 1, "modals": 0}
        with patch('yxy_course.QuizHandler', return_value=handler):
            controller._run_quiz_handler()
        handler.answer_all.assert_called_once()
        controller.stop.assert_called_once()
        self.assertFalse(controller._quiz_busy)

    def test_script_observes_video_and_completion_state(self):
        self.assertIn("loadedmetadata", INJECT_JS)
        self.assertIn("ratechange", INJECT_JS)
        self.assertIn("MutationObserver", INJECT_JS)
        self.assertIn("[yxy:event]", INJECT_JS)
        self.assertIn("继续", INJECT_JS)
        self.assertIn("恭喜你完成本章", INJECT_JS)
        self.assertIn("if (currentPageRecordComplete())", INJECT_JS)
        self.assertIn("completedVideos.add(videoItemKey(video, videos))", INJECT_JS)

    def test_script_has_no_fake_activity_or_broad_clicks(self):
        for forbidden in (
            "fakeMouseActivity",
            "mouse_interval_min",
            "mouse_interval_max",
            "Math.random",
            "new MouseEvent('mousemove'",
            ".click()",
        ):
            self.assertNotIn(forbidden, INJECT_JS)

    def test_script_has_no_answer_fetch_or_submission(self):
        for forbidden in ("questionAnswer", "correctAnswerList", "fetchAndAnswer", "btn-submit"):
            self.assertNotIn(forbidden, INJECT_JS)

    def test_script_owns_and_cleans_all_resources(self):
        self.assertIn("observer.disconnect()", INJECT_JS)
        self.assertIn("clearInterval", INJECT_JS)
        self.assertIn("clearTimeout", INJECT_JS)
        self.assertIn("removeEventListener", INJECT_JS)

    def test_start_target_uses_the_first_visible_page_and_never_invokes_view_model_navigation(self):
        self.assertIn("course.chapters", FIRST_PAGE_TARGET_JS)
        self.assertIn("document.getElementById('page' + pageId)", FIRST_PAGE_TARGET_JS)
        self.assertNotIn("currentPage(", FIRST_PAGE_TARGET_JS)


class ActionExecutorTests(unittest.TestCase):
    def test_cdp_click_uses_viewport_coordinates_and_order(self):
        calls = []

        def cdp_call(method, params=None, timeout=10.0, session_id=None):
            calls.append((method, params))
            return {}

        executor = ActionExecutor(cdp_call, lambda _expression, _timeout=10.0: None)
        self.assertTrue(executor.click_viewport_point(123.5, 456.25))
        self.assertEqual(
            [params["type"] for method, params in calls if method == "Input.dispatchMouseEvent"],
            ["mouseMoved", "mousePressed", "mouseReleased"],
        )
        self.assertTrue(all(params["x"] == 123.5 and params["y"] == 456.25 for _, params in calls))

    def test_attach_enables_background_focus_emulation_without_foregrounding_tab(self):
        controller = CourseController(lambda _text, _kind: None)
        controller.ws_url = "ws://course"
        sent = []
        fake_ws = Mock()
        with (
            patch("yxy_course.create_connection", return_value=fake_ws),
            patch.object(controller, "_recv_loop"),
            patch.object(controller, "_event_loop"),
            patch.object(controller, "_send", side_effect=lambda method, params=None, **_kwargs: sent.append((method, params)) or 1),
        ):
            self.assertTrue(controller.attach())
        self.assertIn(("Emulation.setFocusEmulationEnabled", {"enabled": True}), sent)
        self.assertNotIn(("Page.bringToFront", None), sent)
        controller.stop()

    def test_navigation_retries_are_bounded_when_postcondition_fails(self):
        calls = []
        page_state = {"url": "https://example/1", "chapter": "one", "source": "video-1"}
        target = {"x": 10, "y": 20, "width": 30, "height": 40, "pointMatches": True, "disabled": False}

        def evaluate(expression, _timeout=10.0):
            return target if "get_navigation_target" in expression else page_state

        executor = ActionExecutor(
            lambda method, params=None, timeout=10.0, session_id=None: calls.append((method, params)) or {},
            evaluate,
            sleep=lambda _seconds: None,
        )
        result = executor.execute_navigation(max_retries=2, verify_timeout=0)
        self.assertFalse(result.ok)
        presses = [params for method, params in calls if method == "Input.dispatchMouseEvent" and params["type"] == "mousePressed"]
        self.assertEqual(len(presses), 2)
        self.assertEqual(result.attempts, 2)

    def test_navigation_requires_real_page_change(self):
        before = {"page": "1", "pageIndex": 1, "pageName": "引论", "source": "video-a"}
        self.assertFalse(ActionExecutor._page_changed(before, {**before, "url": "https://example?tick=2"}))
        self.assertFalse(ActionExecutor._page_changed(before, {**before, "pageName": "引论（已完成）"}))
        self.assertTrue(ActionExecutor._page_changed(before, {**before, "page": "2", "pageIndex": 2, "pageName": "课程简介"}))

    def test_navigation_click_does_not_activate_the_course_tab(self):
        calls = []
        before = {"page": "1", "pageIndex": 1, "pageName": "引论", "source": "video-a"}
        target = {"x": 10, "y": 20, "width": 30, "height": 40, "pointMatches": True, "disabled": False}

        def evaluate(expression, _timeout=10.0):
            return target if "get_navigation_target" in expression else before

        executor = ActionExecutor(
            lambda method, params=None, timeout=10.0, session_id=None: calls.append(method) or {},
            evaluate,
            sleep=lambda _seconds: None,
        )
        executor.execute_navigation(max_retries=1, verify_timeout=0)
        self.assertIn("Input.dispatchMouseEvent", calls)
        self.assertNotIn("Page.bringToFront", calls)

    def test_stopped_executor_does_not_click(self):
        cdp_call = Mock()
        executor = ActionExecutor(cdp_call, Mock(), is_running=lambda: False)
        result = executor.execute_navigation(max_retries=2, verify_timeout=0)
        self.assertFalse(result.ok)
        cdp_call.assert_not_called()


class TemplateCoordinateTests(unittest.TestCase):
    def test_template_coordinates_scale_to_css_viewport(self):
        match = TemplateMatch(x=500, y=250, width=100, height=50, confidence=0.95)
        self.assertEqual(
            template_match_to_css(match, screenshot_size=(2000, 1000), viewport_size=(1000, 500)),
            (275.0, 137.5),
        )

    def test_low_confidence_template_is_rejected(self):
        match = TemplateMatch(x=500, y=250, width=100, height=50, confidence=0.49)
        self.assertIsNone(
            template_match_to_css(match, screenshot_size=(2000, 1000), viewport_size=(1000, 500), threshold=0.8)
        )


class CdpDocumentTests(unittest.TestCase):
    def test_finds_and_attaches_oopif_document(self):
        controller = CourseController(lambda _text, _kind: None)
        with patch.object(
            controller,
            "_cdp_call",
            side_effect=[
                {"targetInfos": [{"targetId": "doc-1", "type": "iframe", "url": "https://docs.ulearning.cn/view/1"}]},
                {"backendNodeId": 101},
                {"sessionId": "session-1"},
            ],
        ):
            targets = controller._document_targets()
        self.assertEqual(
            targets,
            [{"id": "doc-1", "url": "https://docs.ulearning.cn/view/1", "sessionId": "session-1", "kind": "oopif"}],
        )

    def test_document_completes_only_after_real_scroll(self):
        events = []
        controller = CourseController(lambda text, _kind: events.append(text))
        item = {"id": "doc-1", "url": "https://docs.ulearning.cn/view/1"}
        with patch.object(controller, "_enqueue_course_event") as enqueue:
            controller._handle_document_scroll_state(item, {"state": "complete"})
            enqueue.assert_not_called()
            controller._handle_document_scroll_state(item, {"state": "scrolled"})
            controller._handle_document_scroll_state(item, {"state": "complete"})
            controller._handle_document_scroll_state(item, {"state": "complete"})
        completion_events = [
            call.args[0] for call in enqueue.call_args_list if call.args[0]["type"] == "document-bottom"
        ]
        self.assertEqual(len(completion_events), 1)
        self.assertTrue(any("文档已滚动至末尾" in text for text in events))


class CourseControllerLifecycleTests(unittest.TestCase):
    @staticmethod
    def observable_controller():
        events = []
        controller = CourseController(
            lambda _text, _kind: None,
            emit_event=lambda code, level, category, message, **kwargs: events.append({"code": code, "level": level, "category": category, "message": message, **kwargs}),
        )
        controller._session_id = "course-test"
        controller._running = True
        controller.state_machine.transition(CourseState.ATTACHING)
        controller.state_machine.transition(CourseState.LOADING)
        controller._last_status = {"state": {"page": "1", "pageName": "引论", "pageIndex": 1, "pageTotal": 40}}
        return controller, events

    def test_unstarted_quiz_skip_uses_navigation_without_completion_event(self):
        controller, events = self.observable_controller()
        status = {**controller._last_status, 'readOk': True, 'quizSkipBeforeStart': True,
                  'quizStartPending': True, 'quizUnfinished': 18}
        with patch.object(controller, 'status_snapshot', return_value=status), \
                patch.object(controller, '_attempt_navigation_if_ready') as navigate:
            controller._handle_course_event({'type': 'static-ready', 'source': 'page-1', 'quizSkipped': True})
            controller._handle_course_event({'type': 'static-ready', 'source': 'page-1', 'quizSkipped': True})
        navigate.assert_called_once()
        self.assertEqual([event['code'] for event in events], ['QUIZ_SKIPPED'])
        self.assertEqual(controller._completion_status(status), (False, ''))

    def test_quiz_skip_event_rechecks_mask_and_fresh_status(self):
        for update in ({'quizStartPending': False}, {'readOk': False}):
            controller, events = self.observable_controller()
            status = {**controller._last_status, 'readOk': True, 'quizSkipBeforeStart': True,
                      'quizStartPending': True, 'quizUnfinished': 18, **update}
            with patch.object(controller, 'status_snapshot', return_value=status), \
                    patch.object(controller, '_attempt_navigation_if_ready') as navigate:
                controller._handle_course_event({'type': 'static-ready', 'source': 'page-1', 'quizSkipped': True})
            navigate.assert_not_called()
            self.assertEqual(events, [])

    def test_network_prompt_is_dismissed_while_loading_with_auto_next_off(self):
        controller, events = self.observable_controller()
        controller._active_config = CourseConfig(auto_next=False)
        dialog = {"type": "docNoWifi", "policy": "network", "page": "1", "signature": "network",
                  "title": "非 Wi-Fi 提示", "buttons": ["继续", "取消"],
                  "target": {"x": 100, "y": 200, "pointMatches": True}}
        with (
            patch.object(controller, "eval_js", side_effect=[{"dialog": dialog}, {"dialog": None}]),
            patch.object(controller.action_executor, "execute_click", return_value=True) as click,
            patch.object(controller._stop_event, "wait", return_value=False),
        ):
            self.assertTrue(controller._handle_network_dialog())
        click.assert_called_once_with(100.0, 200.0)
        self.assertEqual(controller.state_machine.state, CourseState.LOADING)
        self.assertEqual([event["code"] for event in events], ["DIALOG_DISMISSED"])

    def test_network_prompt_obeys_dismiss_setting_and_does_not_click_other_dialogs(self):
        controller, _ = self.observable_controller()
        controller._active_config = CourseConfig(auto_dismiss_dialog=False)
        with patch.object(controller, "eval_js") as evaluate:
            self.assertFalse(controller._handle_network_dialog())
            evaluate.assert_not_called()
        controller._active_config.auto_dismiss_dialog = True
        with patch.object(controller, "eval_js", return_value={"kind": "forward-dialog"}), patch.object(controller.action_executor, "execute_click") as click:
            self.assertFalse(controller._handle_network_dialog())
            click.assert_not_called()

    def test_network_prompt_failed_verification_has_bounded_retries(self):
        controller, events = self.observable_controller()
        controller._active_config = CourseConfig()
        dialog = {"type": "docNoWifi", "policy": "network", "page": "1", "signature": "network",
                  "title": "非 Wi-Fi 提示", "buttons": ["继续", "取消"],
                  "target": {"x": 100, "y": 200, "pointMatches": True}}
        with (
            patch.object(controller, "eval_js", return_value={"dialog": dialog}),
            patch.object(controller.action_executor, "execute_click", return_value=True) as click,
            patch.object(controller._stop_event, "wait", return_value=False),
        ):
            for _ in range(4):
                self.assertFalse(controller._handle_network_dialog())
        self.assertEqual(click.call_count, 3)
        self.assertEqual([event['code'] for event in events], ['DIALOG_UNRESOLVED', 'DIALOG_UNRESOLVED'])

    def test_page_completed_is_emitted_once_for_all_videos(self):
        controller, events = self.observable_controller()
        controller.state_machine.observe_video_ready("page-1")
        controller.state_machine.observe_video_playing("page-1")
        with patch.object(controller, "_attempt_navigation_if_ready"):
            controller._handle_course_event({"type": "video-ended", "source": "page-1", "total": 2})
            controller._handle_course_event({"type": "video-ended", "source": "page-1", "total": 2})
        self.assertEqual([event["code"] for event in events].count("PAGE_COMPLETED"), 1)

    def test_startup_returns_to_first_page_and_verifies_page_id(self):
        controller, _events = self.observable_controller()
        first_target = {"page": "first", "pageName": "引论", "x": 100, "y": 200}
        with (
            patch.object(controller, "eval_js", return_value=first_target),
            patch.object(controller, "_cdp_call", return_value={}) as cdp,
            patch.object(controller.action_executor, "execute_click", return_value=True) as click,
            patch.object(controller, "_read_bootstrap_state", return_value={"page": "first", "pageName": "引论"}),
        ):
            state = controller._return_to_course_start({"page": "last", "pageName": "最后一节"})
        self.assertEqual(state["page"], "first")
        cdp.assert_not_called()
        click.assert_called_once_with(100.0, 200.0)

    def test_startup_refuses_to_continue_when_first_page_is_not_verified(self):
        controller, _events = self.observable_controller()
        target = {"page": "first", "pageName": "引论", "x": 100, "y": 200}
        with (
            patch.object(controller, "eval_js", return_value=target),
            patch.object(controller, "_cdp_call", return_value={}),
            patch.object(controller.action_executor, "execute_click", return_value=False),
        ):
            self.assertIsNone(controller._return_to_course_start({"page": "last"}))

    def test_stall_and_recovery_events_are_deduplicated(self):
        controller, events = self.observable_controller()
        state = controller._last_status["state"]
        controller._begin_recovery("video", 1, state)
        controller._begin_recovery("video", 1, state)
        controller._finish_recovery(state)
        controller._finish_recovery(state)
        codes = [event["code"] for event in events]
        self.assertEqual(codes.count("STALL_DETECTED"), 1)
        self.assertEqual(codes.count("RECOVERY_STARTED"), 1)
        self.assertEqual(codes.count("RECOVERY_SUCCEEDED"), 1)

    def test_navigation_recovery_requires_page_change(self):
        controller, _events = self.observable_controller()
        state = controller._last_status["state"]
        controller._begin_recovery("navigation", 1, state)
        self.assertFalse(controller._progress_resolves_recovery({"state": {**state, "recordComplete": True}}))
        self.assertTrue(controller._progress_resolves_recovery({"state": {**state, "page": "2", "pageIndex": 2}}))

    def test_ended_video_recovery_retries_navigation_even_if_already_completed(self):
        controller, _events = self.observable_controller()
        controller.state_machine.observe_video_ready("page-1")
        controller.state_machine.observe_video_playing("page-1")
        controller.state_machine.mark_content_finished("video", "page-1")
        status = {**controller._last_status, "videos": [{"ended": True}]}
        with patch.object(controller, "_attempt_navigation_if_ready") as navigate, patch.object(controller, "eval_js"):
            controller._recover_stall(status)
        navigate.assert_called_once()

    def test_late_page_events_cannot_bypass_navigation_backoff(self):
        controller, _events = self.observable_controller()
        controller.state_machine.observe_static_ready("page-1")
        controller.state_machine.mark_content_finished("static", "page-1")
        controller._begin_recovery("navigation", 1, controller._last_status["state"])
        with patch.object(controller.action_executor, "navigation_target") as target:
            controller._attempt_navigation_if_ready()
        target.assert_not_called()

    def test_late_page_ready_cannot_revert_observed_page(self):
        controller, events = self.observable_controller()
        controller._observed_page_id = "page-2"
        controller._handle_course_event({"type": "page-ready", "state": {"page": "page-1", "pageName": "旧页"}})
        self.assertEqual(controller._observed_page_id, "page-2")
        self.assertNotIn("PAGE_ENTERED", [event["code"] for event in events])

    def test_status_polling_does_not_emit_logs_and_tracks_cdp_reconnect(self):
        controller, events = self.observable_controller()
        controller.ws = object()
        with patch.object(controller, "_read_page_status", return_value=controller._last_status):
            self.assertTrue(controller.status_snapshot()["connected"])
            controller._connection_lost.set()
            self.assertFalse(controller.status_snapshot()["connected"])
            controller._connection_lost.clear()
            self.assertTrue(controller.status_snapshot()["connected"])
        self.assertEqual(events, [])

    def test_navigation_preflight_uses_real_completion_state(self):
        controller, _events = self.observable_controller()
        incomplete = {"state": {"page": "1", "recordComplete": False}, "videos": [{"ended": False}]}
        self.assertFalse(controller._navigation_precondition_met(incomplete))
        self.assertTrue(controller._navigation_precondition_met({**incomplete, "state": {"page": "1", "recordComplete": True}}))
        self.assertTrue(controller._navigation_precondition_met({**incomplete, "videos": [{"ended": True}]}))

    def test_status_snapshot_exposes_confirmed_page_completion(self):
        controller, _events = self.observable_controller()
        raw = {"state": {"page": "1", "pageName": "引论", "recordComplete": True}, "videos": [{"ended": True, "rate": 6}]}
        controller.ws = object()
        with patch.object(controller, "_read_page_status", return_value=raw):
            status = controller.status_snapshot()
        self.assertTrue(status["pageCompleted"])
        self.assertEqual(status["completionSource"], "record")
        self.assertTrue(status["page"]["completed"])

    def test_course_completion_stops_watchdog_and_recovery(self):
        controller, events = self.observable_controller()
        controller.ws = Mock()
        controller.state_machine.complete()
        controller._session_started_at = 0
        with patch.object(controller, "_cdp_eval", return_value={}), patch("yxy_course.time.monotonic", return_value=100):
            controller._emit_course_completed(controller._last_status["state"])
        self.assertFalse(controller._running)
        self.assertTrue(controller._stop_event.is_set())
        self.assertIsNone(controller.ws)
        controller._begin_recovery("video", 1, controller._last_status["state"])
        self.assertEqual([event["code"] for event in events], ["COURSE_COMPLETED"])
        self.assertIn("用时 01分40秒", events[0]["message"])
        self.assertEqual(events[0]["data"]["elapsedSeconds"], 100)

    def test_stale_course_finished_event_cannot_finish_loading_quiz(self):
        controller, events = self.observable_controller()
        status = {"courseFinished": False, "quizLoading": True, "state": {"page": "1"}}
        with patch.object(controller, "status_snapshot", return_value=status):
            controller._handle_course_event({"type": "course-finished", "state": {"page": "1"}})
        self.assertTrue(controller._running)
        self.assertNotEqual(controller.state_machine.state, CourseState.COMPLETED)
        self.assertNotIn("COURSE_COMPLETED", [event["code"] for event in events])

    def test_last_page_video_does_not_bypass_pending_document(self):
        controller, events = self.observable_controller()
        controller.state_machine.mark_content_finished("video")
        status = {"courseFinished": True, "state": {"page": "1", "recordComplete": False},
                  "videos": [{"ended": True}], "hasDocument": True}
        with patch.object(controller, "status_snapshot", return_value=status):
            controller._attempt_navigation_if_ready()
        self.assertTrue(controller._running)
        self.assertNotIn("COURSE_COMPLETED", [event["code"] for event in events])

    def test_elapsed_format_is_stable_for_short_and_long_sessions(self):
        self.assertEqual(CourseController._format_elapsed(246), "04分06秒")
        self.assertEqual(CourseController._format_elapsed(3723), "01时02分03秒")

    def test_idle_snapshot_does_not_claim_a_page_is_completed(self):
        controller, _events = self.observable_controller()
        controller._last_status = None
        status = controller.status_snapshot()
        self.assertFalse(status["pageCompleted"])
        self.assertEqual(status["currentTask"], "等待")
        self.assertEqual(status["pagePlan"], [])

    def test_failed_click_never_emits_page_changed(self):
        controller, events = self.observable_controller()
        controller.state_machine.observe_static_ready("page-1")
        controller.state_machine.mark_content_finished("static", "page-1")
        controller.state_machine.mark_next_ready()
        controller.state_machine.begin_navigation()
        with patch.object(controller.action_executor, "execute_navigation", return_value=ActionResult(False, 2, "postcondition-timeout", controller._last_status["state"])):
            controller._perform_navigation()
        self.assertNotIn("PAGE_CHANGED", [event["code"] for event in events])

    def test_verified_page_change_emits_success(self):
        controller, events = self.observable_controller()
        controller.state_machine.observe_static_ready("page-1")
        controller.state_machine.mark_content_finished("static", "page-1")
        controller.state_machine.mark_next_ready()
        controller.state_machine.begin_navigation()
        changed = {"page": "2", "pageName": "课程简介", "pageIndex": 2, "pageTotal": 40, "source": "video-2"}
        with patch.object(controller.action_executor, "execute_navigation", return_value=ActionResult(True, 1, "page-state-changed", changed)):
            controller._perform_navigation()
        page_events = [event for event in events if event["code"] == "PAGE_CHANGED"]
        self.assertEqual(len(page_events), 1)
        self.assertIn("课程简介（2/40）", page_events[0]["message"])

    def test_repeated_start_does_not_create_two_controllers(self):
        controller = CourseController(lambda _text, _kind: None)
        with (
            patch.object(controller, "find_course_tab", return_value="ws://course"),
            patch.object(controller, "attach", side_effect=lambda: setattr(controller, "_running", True) or True),
            patch.object(controller, "_return_to_course_start", return_value={"page": "first", "pageName": "引论"}),
            patch.object(controller, "inject_main_script", return_value=True) as inject,
        ):
            self.assertTrue(controller.start(CourseConfig(document_scroll_enabled=False)))
            self.assertFalse(controller.start(CourseConfig(document_scroll_enabled=False)))
        inject.assert_called_once()

    def test_stop_prevents_late_events_from_triggering_actions(self):
        controller = CourseController(lambda _text, _kind: None)
        controller._running = True
        controller.state_machine.transition(CourseState.ATTACHING)
        controller.state_machine.transition(CourseState.LOADING)
        controller.stop()
        with patch.object(controller, "_perform_navigation") as navigate:
            controller._enqueue_course_event({"type": "video-ended", "source": "video-1"})
        navigate.assert_not_called()
        self.assertEqual(controller.state_machine.state, CourseState.STOPPED)

    def test_console_events_from_previous_injection_are_ignored(self):
        controller = CourseController(lambda _text, _kind: None)
        controller._running = True
        controller._session_token = "current"
        controller._on_console({"args": [{"type": "string", "value": '[yxy:event] {"type":"video-ended","session":"old"}'}]})
        self.assertTrue(controller._event_queue.empty())
        controller._on_console({"args": [{"type": "string", "value": '[yxy:event] {"type":"video-ended","session":"current"}'}]})
        self.assertEqual(controller._event_queue.get_nowait()["session"], "current")
        controller._running = False


class CourseTabTests(unittest.TestCase):
    @patch("yxy_course.requests.get")
    def test_finds_course_tab(self, mock_get):
        mock_get.return_value.json.return_value = [
            {"type": "page", "url": f"https://{COURSE_TAB_URL_KEYWORD}/learnCourse.html", "webSocketDebuggerUrl": "ws://course"},
            {"type": "page", "url": "https://lms.dgut.edu.cn/", "webSocketDebuggerUrl": "ws://lms"},
        ]
        self.assertEqual(CourseController(lambda _text, _kind: None).find_course_tab(), "ws://course")


if __name__ == "__main__":
    unittest.main()
