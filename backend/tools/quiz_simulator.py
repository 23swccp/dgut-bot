"""本地优学院测验模拟器：用真实 Chromium/CDP 验证 QuizHandler 的题型开关。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT = PROJECT_ROOT
SOURCE_ROOT = PROJECT_ROOT / "backend" / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from dgutbot.domain.yxy_backend import AppConfig, SignBackend
from dgutbot.course.yxy_quiz import QUIZ_STATE_JS, QuizHandler, StandaloneBackend
from dgutbot.course.yxy_course import INJECT_JS, CourseConfig, CourseController, PagePlan
from dgutbot.course.course_dialogs import DIALOG_STATE_JS
from quiz_probe import TabConnection


SIMULATOR_ROOT = PROJECT_ROOT / "backend" / "quiz_simulator"


def run_slide_fixture(backend, debug_port, web_port, cross_origin):
    from dgutbot.course.course_slides import SLIDE_STATE_JS
    from dgutbot.course.yxy_course import PagePlan
    host = 'localhost' if cross_origin else '127.0.0.1'
    source = f'http://{host}:{web_port}/slides.html'
    backend.evaluate('window.showSlideFixture(' + json.dumps(source) + ')')
    tabs = json.loads(urlopen(f'http://127.0.0.1:{debug_port}/json/list').read())
    tab = next(t for t in tabs if '/learnCourse.html' in t.get('url',''))
    conn = TabConnection(tab['webSocketDebuggerUrl'])
    controller = CourseController(lambda *_: None)
    def call(method, params=None, timeout=10.0, session_id=None):
        result, error = conn.send(method, params, session_id=session_id, timeout=timeout)
        return None if error else result
    controller._cdp_call = call
    controller.action_executor._cdp_call = call
    controller._running = True
    controller._active_config = CourseConfig(auto_next=False, anti_idle_scroll=False)
    try:
        item = None
        for _ in range(40):
            targets = call('Target.getTargets').get('targetInfos', [])
            found = next((t for t in targets if t.get('type')=='iframe' and t.get('url')==source),None)
            if found:
                attached = call('Target.attachToTarget',{'targetId':found['targetId'],'flatten':True})
                item = {'id':found['targetId'],'sessionId':attached['sessionId'],'url':source}
                context_id = None
            else:
                frames = controller._walk_frames(call('Page.getFrameTree').get('frameTree',{}))
                frame = next((f for f in frames if f.get('url')==source),None)
                if frame:
                    item=frame
                    context_id=controller._frame_context_id(frame['id'])
            if item:
                raw=controller._cdp_eval(SLIDE_STATE_JS,context_id=context_id,session_id=item.get('sessionId'))
                if (raw or {}).get('result',{}).get('value',{}):
                    break
            time.sleep(0.05)
        assert item is not None, 'PPT fixture frame unavailable'
        backend.evaluate('window.__YXY_CONFIG__=' + controller._active_config.to_js() + ';' + INJECT_JS)
        # 平台业务错误即使仍有首页/页码，也必须优先于“可翻页/已完成”。
        for message, code in [('Sorry！解析过程出错。', 'parse-failed'), ('加载失败', 'load-failed'),
                              ('试读结束', 'access-limited'), ('未知播放器提示', 'player-message')]:
            controller._cdp_eval("document.querySelector('#msg').hidden=false;document.querySelector('#msg').textContent=" + json.dumps(message),
                                 context_id=context_id, session_id=item.get('sessionId'))
            error = controller._scroll_document_target(item) if item.get('sessionId') else controller._scroll_document_frame(item)
            assert error['state'] == 'slides-error' and error['error']['code'] == code, error
            status = backend.evaluate('window.__yxy_controller.get_status()')
            assert not PagePlan.from_status(status).ready_for_navigation, status
            assert backend.evaluate('window.__yxy_controller.get_navigation_target()') is None
        controller._cdp_eval("document.querySelector('#msg').hidden=true", context_id=context_id, session_id=item.get('sessionId'))
        controller._cdp_eval("document.querySelector('.waitmsg').hidden=false", context_id=context_id, session_id=item.get('sessionId'))
        loading = controller._scroll_document_target(item) if item.get('sessionId') else controller._scroll_document_frame(item)
        assert loading['state']=='slides-loading', loading
        controller._cdp_eval("document.querySelector('.waitmsg').hidden=true", context_id=context_id, session_id=item.get('sessionId'))
        for expected in [1,2,3,3]:
            result = controller._scroll_document_target(item) if item.get('sessionId') else controller._scroll_document_frame(item)
            assert result['current']==expected, result
            controller._handle_document_scroll_state(item,result)
            status=backend.evaluate('window.__yxy_controller.get_status()')
            assert PagePlan.from_status(status).ready_for_navigation == (result['state']=='slides-complete'), status
            if expected == 1:
                backend.evaluate("(()=>{const e=document.createElement('div');e.id='ppt-test-cover';e.style.cssText='position:fixed;inset:0;background:white;z-index:999999';document.body.append(e)})()")
                blocked=controller._scroll_document_target(item) if item.get('sessionId') else controller._scroll_document_frame(item)
                assert blocked['state']=='slides-wait', blocked
                backend.evaluate("document.querySelector('#ppt-test-cover').remove()")
        stats = (controller._cdp_eval('window.stats',session_id=item['sessionId'])['result']['value'] if item.get('sessionId')
                 else backend.evaluate("document.querySelector('#slideFixture iframe').contentWindow.stats"))
        assert stats=={'next':2,'animation':0,'trusted':True}, stats
        print(f"通过 | PPT {'跨域' if cross_origin else '同进程 iframe'} | 1 → 2 → 3，末张不重复翻页")
        return True
    except Exception as error:
        print(f"失败 | PPT {'跨域' if cross_origin else '同进程 iframe'} | {error}")
        return False
    finally:
        backend.evaluate('window.__yxy_controller && window.__yxy_controller.cleanup()')
        controller._running=False
        conn.ws.close()


class QuietHandler(SimpleHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass  # Chromium 切换站点/关闭隔离测试页时会取消旧连接。

    def log_message(self, _format: str, *_args) -> None:
        pass


def wait_for_debug_port(port: int, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
                return
        except OSError:
            time.sleep(0.15)
    raise RuntimeError(f"浏览器调试端口 {port} 未能启动")


def load_current_config() -> AppConfig:
    try:
        values = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        values = {}
    return AppConfig.from_mapping(values, ROOT)


def wait_for_simulator_page(backend: StandaloneBackend, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if backend.evaluate("typeof window.getSimulatorResult === 'function'") is True:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("模拟测验页面未能完成加载")


def result_matches(result: dict, *, choice: bool, judgment: bool, blank: bool) -> bool:
    expected_finished = {
        "question-sim-choice": choice,
        "question-sim-judgment": judgment,
        "question-sim-blank": blank,
    }
    return (
        result.get("finished") == expected_finished
        and result.get("choice") == ("C" if choice else "")
        and result.get("judgment") == ("错误" if judgment else "")
        and result.get("blank") == ("," if blank else "")
        and result.get("submitCount") == (1 if any((choice, judgment, blank)) else 0)
        and result.get("trustedClicks", 0) >= sum((choice, judgment, blank)) + (1 if any((choice, judgment, blank)) else 0)
    )


def run_case(
    backend: StandaloneBackend,
    name: str,
    *,
    enabled: bool,
    choice: bool,
    judgment: bool,
    blank: bool,
    timed: bool = False,
) -> tuple[bool, dict, dict]:
    backend.evaluate(f"window.resetQuiz({str(timed).lower()})")
    summary = {"done": 0, "skipped": 0, "failed": 0, "modals": 0}
    if enabled:
        handler = QuizHandler(
            evaluate=backend.evaluate,
            click=backend.click,
            type_text=backend.type_text,
            sleep=lambda _seconds: None,
            log=lambda *_args: None,
            dry_run=False,
            jitter=0,
        )
        summary = handler.answer_all(
            answer_choice=choice,
            answer_judgment=judgment,
            answer_blank=blank,
        )
    result = json.loads(backend.evaluate("JSON.stringify(window.getSimulatorResult())"))
    expected = (choice, judgment, blank) if enabled else (False, False, False)
    passed = summary["failed"] == 0 and result_matches(
        result,
        choice=expected[0],
        judgment=expected[1],
        blank=expected[2],
    )
    should_start = timed and any(expected)
    passed = passed and result.get("startCount") == int(should_start)
    passed = passed and result.get("startPending") == (timed and not should_start)
    detail = json.dumps({"summary": summary, "page": result}, ensure_ascii=False)
    backend.evaluate(
        f"window.renderRunnerResult({json.dumps(name, ensure_ascii=False)}, {str(passed).lower()}, {json.dumps(detail, ensure_ascii=False)})"
    )
    return passed, summary, result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="启动本地模拟测验并用真实浏览器事件验证自动答题")
    parser.add_argument("--show", action="store_true", help="显示测试浏览器窗口")
    parser.add_argument("--hold", type=float, default=15.0, help="--show 时测试结束后保留窗口的秒数")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(SIMULATOR_ROOT)))
    server_thread = threading.Thread(target=server.serve_forever, name="quiz-simulator-http", daemon=True)
    server_thread.start()
    web_port = int(server.server_address[1])

    finder = SignBackend(lambda *_args: None, root=ROOT)
    browser_path, browser_name = finder.find_browser()
    if not browser_path:
        server.shutdown()
        print("未找到 Chromium 浏览器；请先在设置中选择 Edge、Chrome 或其他 Chromium 浏览器。")
        return 2

    backend = None
    process = None
    failures = 0
    with tempfile.TemporaryDirectory(prefix="yxy-quiz-simulator-", ignore_cleanup_errors=True) as profile:
        debug_server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        debug_port = int(debug_server.server_address[1])
        debug_server.server_close()
        url = f"http://127.0.0.1:{web_port}/learnCourse.html"
        command = [
            browser_path,
            f"--remote-debugging-port={debug_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--disable-component-update",
            "--window-size=1280,900",
        ]
        if not args.show:
            command.append("--headless=new")
        command.append(url)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if not args.show else 0,
            )
            wait_for_debug_port(debug_port)
            backend = StandaloneBackend(
                port=debug_port,
                dry_run=False,
                log=lambda *_args: None,
                tab_keyword="/learnCourse.html",
            )
            wait_for_simulator_page(backend)

            cases = [
                ("总开关关闭", False, True, True, True, False),
                ("三类题全部开启", True, True, True, True, False),
                ("仅选择题", True, True, False, False, False),
                ("仅判断题", True, False, True, False, False),
                ("仅填空题", True, False, False, True, False),
                ("限时测验自动开始并完成", True, True, True, True, True),
                ("限时测验总开关关闭", False, True, True, True, True),
                ("限时测验题型全关闭", True, False, False, False, True),
            ]
            config = load_current_config()
            cases.append((
                "当前 config.json 设置",
                bool(config.course_quiz_auto_answer),
                bool(config.course_quiz_choice_enabled),
                bool(config.course_quiz_judgment_enabled),
                bool(config.course_quiz_blank_enabled),
                False,
            ))
            print(f"模拟浏览器：{browser_name}")
            for name, enabled, choice, judgment, blank, timed in cases:
                passed, summary, result = run_case(
                    backend,
                    name,
                    enabled=enabled,
                    choice=choice,
                    judgment=judgment,
                    blank=blank,
                    timed=timed,
                )
                failures += 0 if passed else 1
                print(
                    f"{'通过' if passed else '失败'} | {name} | "
                    f"作答={summary['done']} 失败={summary['failed']} | "
                    f"选择={result['choice'] or '-'} 判断={result['judgment'] or '-'} "
                    f"填空={result['blank'] or '-'} 启动={result['startCount']} 可信点击={result['trustedClicks']}"
                )
            for auto_answer, auto_next in ((False, True), (False, False), (True, True)):
                backend.evaluate('window.setupTimedSkipFixture()')
                config = CourseConfig(quiz_auto_answer=auto_answer, auto_next=auto_next, document_scroll_enabled=False, anti_idle_scroll=False)
                backend.evaluate('window.__YXY_CONFIG__=' + config.to_js() + ';' + INJECT_JS)
                status = backend.evaluate('window.__yxy_controller.get_status()')
                target = backend.evaluate('window.__yxy_controller.get_navigation_target()')
                passed = status['quizStartPending'] and status['quizSkipBeforeStart'] == (not auto_answer)
                passed = passed and PagePlan.from_status(status).ready_for_navigation == (not auto_answer)
                if not auto_answer and auto_next:
                    passed = passed and bool(target) and backend.click(target['x'], target['y'])
                    after = backend.evaluate('window.__yxy_controller.get_page_state()')
                    passed = passed and after['page'] == 'next-page'
                else:
                    passed = passed and target is None
                result = backend.evaluate('window.getSimulatorResult()')
                passed = passed and result['startCount'] == 0 and result['submitCount'] == 0
                failures += not passed
                if not passed:
                    print(f"  入口诊断：status={status} plan={PagePlan.from_status(status)} target={target} result={result}")
                print(f"{'通过' if passed else '失败'} | 限时入口 自动答题={auto_answer} 自动翻页={auto_next} | 未启动计时")
                backend.evaluate('window.__yxy_controller.cleanup();window.cleanupTimedSkipFixture()')
            for kind in ('docNoWifi', 'videoNoWifi'):
                backend.evaluate(f"window.showNetworkPrompt({json.dumps(kind)})")
                course_config = CourseConfig(auto_next=False, document_scroll_enabled=False, anti_idle_scroll=False)
                backend.evaluate("window.__YXY_CONFIG__=" + course_config.to_js() + ";" + INJECT_JS)
                controller = CourseController(lambda *_args: None)
                controller._running = True
                controller._active_config = course_config
                controller.eval_js = backend.evaluate
                controller.action_executor.execute_click = backend.click
                passed = controller._handle_network_dialog()
                result = json.loads(backend.evaluate("JSON.stringify(window.getNetworkResult())"))
                passed = passed and result['continued'] == 1 and result['cancelled'] == 0 and not result['pending']
                failures += 0 if passed else 1
                print(f"{'通过' if passed else '失败'} | 非 Wi-Fi {kind} | 继续={result['continued']} 取消={result['cancelled']}")
                backend.evaluate("window.__yxy_controller.cleanup()")
                controller._running = False
            dialog_cases = [
                ('suspend', '计时学习已暂停', ['继续学习'], '继续学习', {}),
                ('multiLearning', '请勿同时学习多个页面', ['返回课程章节', '继续学习'], '继续学习', {}),
                ('videoGuide', '视频观看时长达到要求才能完成', ['知道了'], '知道了', {}),
                ('splitScreen', '请在完成作答后点击退出分屏', ['知道了'], '知道了', {}),
                ('autoSubmit', '限时答题时间已耗尽，已自动提交练习', ['我知道了'], '我知道了', {}),
                ('incompleteTimeLimit', '限时答题还未完成', ['我知道了'], '我知道了', {}),
                ('docFailed', '文档未显示可能是以下原因', ['我知道了'], '我知道了', {}),
                ('flashFailed', '加载异常', ['我知道了'], '我知道了', {}),
                ('videoFailed', '加载异常', ['我知道了'], '我知道了', {}),
                ('createRecordFailed', '保存学习记录失败', ['确定', '重试'], '重试', {}),
                ('goBackCreateRecordFailed', '保存学习记录失败', ['留在本页', '确定离开'], '留在本页', {}),
                ('userGuide', '首次使用说明：跳过所有提示', ['跳过所有提示'], '跳过所有提示', {}),
                ('stopLearning', '本页面已停止学习', ['返回课程章节'], None, {}),
                ('createRecordFailedTooMany', '保存学习记录失败', ['确定'], None, {}),
                ('timeLimit', '限时练习', ['开始答题'], None, {}),
                ('incompleteOralItem', '本页面还有单词未读完', ['继续学习', '离开本页'], None, {}),
                ('incomplete', '本页面还有题目没有完成', ['留在本页', '确定离开'], None, {}),
                ('statistics', '本章成绩 完成本章学习', ['留在本页', '继续下一章 >>'], None, {}),
                ('newUnknownType', '新提示', ['确定', '继续'], None, {}),
                ('suspend', '身份验证，请确认在场', ['继续学习'], None, {}),
                ('suspend', '计时学习已暂停', ['继续学习'], None, {'disabled': True}),
                ('suspend', '计时学习已暂停', ['继续学习'], None, {'occluded': True}),
                ('suspend', '计时学习已暂停', ['继续学习', '继续学习'], None, {}),
                ('suspend', '文案含义已变化', ['继续学习'], None, {}),
            ]
            for kind, body, labels, expected, options in dialog_cases:
                backend.evaluate('window.showDialogCase(' + ','.join(json.dumps(x, ensure_ascii=False) for x in (kind, body, labels, options)) + ')')
                config = CourseConfig(auto_next=False, document_scroll_enabled=False, anti_idle_scroll=False)
                backend.evaluate('window.__YXY_CONFIG__=' + config.to_js() + ';' + INJECT_JS)
                controller = CourseController(lambda *_args: None)
                controller._running = True
                controller._active_config = config
                controller.eval_js = backend.evaluate
                controller.action_executor.execute_click = backend.click
                before = backend.evaluate(DIALOG_STATE_JS)
                quiz_modal = json.loads(backend.evaluate(QUIZ_STATE_JS))['modal']
                handled = controller._handle_classified_dialog()
                result = backend.evaluate('window.dialogCaseResult')
                passed = handled == bool(expected) and result['clicked'] == ([expected] if expected else []) and result['trusted']
                passed = passed and quiz_modal['signature'] == before['signature'] and quiz_modal['policy'] == before['policy']
                if kind == 'autoSubmit':
                    backend.evaluate("document.querySelectorAll('.question-wrapper').forEach(el => el.classList.add('finished'))")
                    counts = backend.evaluate('window.getSimulatorResult()')
                    backend.evaluate('window.showDialogCase(' + ','.join(json.dumps(x, ensure_ascii=False) for x in (kind, body, labels)) + ')')
                    handler = QuizHandler(evaluate=backend.evaluate, click=backend.click, sleep=lambda _: None, log=lambda *_: None, dry_run=False, jitter=0)
                    handler.answer_all()
                    after_counts = backend.evaluate('window.getSimulatorResult()')
                    passed = passed and after_counts['submitCount'] == counts['submitCount']
                    passed = passed and backend.evaluate('window.dialogCaseResult.clicked') == ['我知道了']
                if before['policy'] == 'navigation':
                    target = before.get('target') or {}
                    passed = passed and target.get('label') == labels[-1]
                elif not expected:
                    status = backend.evaluate('window.__yxy_controller.get_status()')
                    passed = passed and not status['courseFinished'] and status['dialogState'] is not None
                failures += not passed
                print(f"{'通过' if passed else '失败'} | 弹窗 {kind} {options} | 点击={result['clicked']}")
                backend.evaluate('window.__yxy_controller.cleanup()')
                controller._running = False
            for cross_origin in (False, True):
                failures += not run_slide_fixture(backend, debug_port, web_port, cross_origin)
            if args.show and args.hold > 0:
                print(f"浏览器窗口保留 {args.hold:g} 秒，页面显示最后一组（当前设置）的结果。")
                time.sleep(args.hold)
        finally:
            if backend is not None:
                backend.close()
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                time.sleep(0.8)
            server.shutdown()
            server.server_close()

    if failures:
        print(f"模拟测试失败：{failures} 组未通过。")
        return 1
    print("模拟测试完成：全部场景通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
