"""真实 Chromium + 本机 HTTP 服务 + CLI 子进程的隔离课件回归实验室。"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

CASES = ("roundtrip", "timed", "long_narrow", "delayed", "slow_render", "rerender", "invalid",
         "stale_page", "stale_structure", "unsupported", "expired", "occluded", "prefilled",
         "multi_blank", "graded_wrong", "server_error", "response_lost", "late_response", "cancelled", "document")


def check(condition, detail):
    if not condition:
        raise AssertionError(detail)


def fixture(seed, case):
    """站点题库/答案不依赖生产选择器或读取器；只把公开题干交给浏览器。"""
    rng = random.Random(seed)
    pages, expected = [], {}
    for index in range(2 if case == "roundtrip" else 1):
        page_id = f"page-{seed}-{index}"
        a, b = rng.randrange(10, 80), rng.randrange(10, 80)
        options = [a + b, a + b + 1, a + b - 1, a + b + 10]
        rng.shuffle(options)
        opts = [{"id": label, "text": str(n)} for label, n in zip("ABCD", options)]
        truth = bool(rng.randrange(2))
        word = f"中文答案-{seed}"
        blank_values = [word, 'A&B <测试> \\"']
        if case == "multi_blank":
            blank_values.append(f"第三空-{seed}")
        questions = [
            {"id": page_id + "-choice", "type": "single_choice", "sourceType": "单选题",
             "prompt": f"计算 {a} + {b} = ?", "options": opts},
            {"id": page_id + "-bool", "type": "true_false", "sourceType": "判断题",
             "prompt": f"判断：{a} < {a + 1 if truth else a - 1}", "options": []},
            {"id": page_id + "-blank", "type": "fill_blank", "sourceType": "填空题",
             "prompt": "按顺序填入" + "、".join(f"「{value}」" for value in blank_values),
             "options": [], "blankCount": len(blank_values)},
        ]
        expected[page_id] = {questions[0]["id"]: [opts[options.index(a + b)]["id"]],
                             questions[1]["id"]: truth, questions[2]["id"]: blank_values}
        if case == "unsupported":
            questions[0]["type"] = "multiple_choice"
            questions[0]["sourceType"] = "多选题"
        pages.append({"id": page_id, "questions": questions})
    return {"pages": pages, "document": case == "document", "timed": case == "timed", "long": case == "long_narrow", "slow": case == "slow_render",
            "prefilled": case == "prefilled"}, expected


def solve(request):
    """确定性参考答题端，仅使用 CLI 公开 JSON；测试传输和执行，不评估大模型知识。"""
    answers = []
    for q in request["questions"]:
        if q["type"] in {"single_choice", "unsupported"}:
            a, b = map(int, re.search(r"(\d+) \+ (\d+)", q["prompt"]).groups())
            value = [next(o["id"] for o in q["options"] if int(o["text"]) == a + b)]
        elif q["type"] == "true_false":
            a, b = map(int, re.search(r"(\d+) < (\d+)", q["prompt"]).groups())
            value = a < b
        else:
            value = re.findall("「(.*?)」", q["prompt"])
        answers.append({"questionId": q["id"], "value": value})
    return {"requestId": request["requestId"], "revision": request["revision"], "answers": answers}


def wait_until(read, predicate, timeout=25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = read()
        if predicate(last):
            return last
        time.sleep(0.15)
    raise AssertionError(f"等待超时，最后状态：{last}")


class Lab:
    def __init__(self, args, root):
        # 必须早于生产模块导入；所有配置、运行凭据、浏览器 profile 均在临时目录。
        os.environ["YXY_DATA_DIR"] = str(root)
        from dgutbot.app import backend_commands as commands, web_server
        from dgutbot.agent.agent_runtime import new_runtime, publish_runtime
        from dgutbot.course import yxy_course
        from quiz_simulator import QuietHandler, wait_for_debug_port
        from quiz_probe import TabConnection

        self.args, self.root, self.commands = args, root, commands
        self.runs, self.trace = {}, []
        self.browser = None
        self.connection = None
        self.servers = []
        self.server_threads = []
        self.original_keyword = yxy_course.COURSE_TAB_URL_KEYWORD
        self.course_module = yxy_course
        lab = self

        class Site(QuietHandler):
            def reply(self, status, data):
                raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                uri = urlsplit(self.path)
                if uri.path != "/fixture":
                    return super().do_GET()
                record = lab.runs.get(parse_qs(uri.query).get("run", [""])[0])
                if record is None:
                    return self.reply(404, {"error": "Unknown fixture run"})
                self.reply(200, record["fixture"])

            def do_POST(self):
                uri = urlsplit(self.path)
                if uri.path != "/submit":
                    return self.reply(404, {})
                record = lab.runs.get(parse_qs(uri.query).get("run", [""])[0])
                if record is None:
                    return self.reply(404, {"error": "Unknown fixture run"})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                accepted = body["answers"] == record["expected"].get(body["pageId"])
                correct = accepted and record["case"] != "graded_wrong"
                record["submissions"].append({**body, "accepted": accepted, "correct": correct})
                if record["case"] == "delayed":
                    time.sleep(1.5)
                if record["case"] == "late_response":
                    record["release"].wait(40)
                if record["case"] == "response_lost":
                    self.close_connection = True
                    return  # 服务端已保存，客户端没收到回执；不能重试提交。
                self.reply(503 if record["case"] == "server_error" else 200,
                           {"accepted": accepted, "correct": correct})

        try:
            site = self.serve(partial(Site, directory=str(ROOT / "quiz_simulator")))
            self.site_url = f"http://127.0.0.1:{site.server_address[1]}"
            api = self.serve(web_server.LocalApiHandler)
            info = new_runtime(api.server_address[1])
            commands.configure_agent_registry(info.instance_id)
            web_server.configure_agent_api(info.auth_token, info.instance_id)
            publish_runtime(info, root)
            browser_path = args.browser or commands.backend.find_browser()[0]
            if not browser_path or not Path(browser_path).is_file():
                raise RuntimeError("未找到 Chromium；请用 --browser 指定 Chrome/Edge 可执行文件。")
            reservation = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
            port = reservation.server_address[1]
            reservation.server_close()
            command = [str(browser_path), f"--remote-debugging-port={port}", "--remote-allow-origins=*",
                       f"--user-data-dir={root / 'browser-profile'}", "--no-first-run", "--no-default-browser-check",
                       "--disable-background-networking", "--disable-component-update", "--disable-background-mode",
                       "--window-size=1280,900"]
            if not args.show:
                command.append("--headless=new")
            command.append(self.site_url + "/agent-course.html")
            self.browser = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            wait_for_debug_port(port)
            # 仅替换测试站点定位和随机调试端口，启动/状态机/事件/答题执行均使用生产实现。
            yxy_course.COURSE_TAB_URL_KEYWORD = self.site_url + "/agent-course.html"
            controller = commands.backend.course_controller
            self.original_find = controller.find_course_tab
            controller.find_course_tab = partial(self.original_find, port)
            target = wait_until(controller.find_course_tab, bool)
            self.connection = TabConnection(target)
            self.cdp("Runtime.enable")
            self.browser_version = self.cdp("Browser.getVersion")
        except BaseException:
            self.close()
            raise

    def serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append(server)
        self.server_threads.append(thread)
        return server

    def cdp(self, method, params=None):
        result, error = self.connection.send(method, params or {}, timeout=10)
        if error:
            raise RuntimeError(f"{method}: {error}")
        return result

    def evaluate(self, expression):
        result = self.cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if result.get("exceptionDetails"):
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def cli(self, tool, payload=None, error=None):
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT))), "PYTHONIOENCODING": "utf-8"}
        process = subprocess.run([shutil.which("python") or sys.executable, "-m", "dgutbot.agent.agent_cli", "call", tool],
                                 input=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
                                 capture_output=True, timeout=40, env=env, cwd=ROOT)
        value = json.loads(process.stdout.decode("utf-8"))
        self.trace.append({"tool": tool, "input": payload or {}, "output": value})
        check(not process.stderr, process.stderr.decode("utf-8", errors="replace"))
        if error:
            check(not value["ok"] and (value["error"] or {}).get("code") == error, value)
            check(process.returncode != 0, "错误响应退出码应非零")
            return value
        check(process.returncode == 0 and value["ok"], value)
        return value["result"]

    def task(self, task_id):
        return self.cli("task.get", {"taskId": task_id})

    def run(self, case, seed):
        self.trace = []
        self.event_start = self.commands.EVENT_BUFFER.get_events()["latestSeq"]
        public, expected = fixture(seed, case)
        run_id = uuid4().hex
        record = {"case": case, "fixture": public, "expected": expected, "submissions": [], "release": threading.Event()}
        self.runs[run_id] = record
        self.cdp("Emulation.setDeviceMetricsOverride", {"width": 620 if case == "long_narrow" else 1280,
                 "height": 720 if case == "long_narrow" else 900, "deviceScaleFactor": 1, "mobile": False})
        self.cdp("Page.navigate", {"url": self.site_url + "/agent-course.html?run=" + run_id})
        wait_until(lambda: self.evaluate("window.labReady === true && new URLSearchParams(location.search).get('run') === " + json.dumps(run_id)), bool)
        task = self.cli("course.start", {"quizMode": "agent", "rate": 1,
                        "quizRequestTimeoutMs": 3000 if case == "expired" else 60000, "idempotencyKey": uuid4().hex})
        task_id = task["taskId"]
        if case == "document":
            final_task = wait_until(lambda: self.task(task_id), lambda t: t["state"] in {"completed", "failed", "cancelled"}, timeout=35)
            check(final_task["state"] == "completed", final_task)
            check(self.evaluate("scrollY + innerHeight >= document.documentElement.scrollHeight - 2"), "文档尚未滚动到末尾")
            check(not record["submissions"], "文档场景不应提交测验")
            return {"case": case, "seed": seed, "passed": True, "browser": self.evaluate("window.labSnapshot()"), "submissions": []}
        for index, page in enumerate(public["pages"]):
            task = wait_until(lambda: self.task(task_id), lambda t: t["state"] in {"waiting_for_input", "failed", "completed", "cancelled"})
            check(task["state"] == "waiting_for_input", task)
            request = self.cli("quiz.get_request", {"requestId": task["waiting"]["requestId"]})
            check(request["pageId"] == page["id"], request)
            check(len(request["questions"]) == 3, request)
            check(all("answerSchema" in q for q in request["questions"]), request)
            if case == "multi_blank":
                blank = next(q for q in request["questions"] if q["type"] == "fill_blank")
                check(blank["blankCount"] == 3, request)
            snapshot = self.evaluate("window.labSnapshot()")
            if case == "unsupported":
                check(request["questions"][0]["type"] == "unsupported", request)
                answers = {"requestId": request["requestId"], "revision": request["revision"],
                           "answers": [{"questionId": q["id"], "value": []} for q in request["questions"]]}
            else:
                answers = solve(request)
            submit = {**answers, "idempotencyKey": uuid4().hex}
            if case == "invalid":
                invalid = {**answers, "answers": answers["answers"][:-1]}
                self.cli("quiz.validate_answers", invalid, error="QUIZ_ANSWER_INVALID")
                check(self.evaluate("window.labSnapshot()") == snapshot, "格式错误时发生了网页操作")
                self.cli("quiz.validate_answers", {**answers, "revision": answers["revision"] + 1}, error="QUIZ_REVISION_MISMATCH")
            if case == "stale_page":
                self.evaluate("window.labChangePage()")
            if case == "stale_structure":
                self.evaluate("document.querySelector('.question-title-html').textContent += ' 已改题'")
            if case == "rerender":
                self.evaluate("window.labRerender()")
            if case == "occluded":
                self.evaluate("window.labCover()")
            if case == "cancelled":
                self.cli("task.cancel", {"taskId": task_id, "idempotencyKey": uuid4().hex})
            if case == "expired":
                wait_until(lambda: self.cli("quiz.get_result", {"requestId": request["requestId"]}),
                           lambda r: r["state"] == "expired", timeout=10)
            rejection = {"stale_page": "QUIZ_PAGE_CHANGED", "stale_structure": "QUIZ_PAGE_CHANGED", "unsupported": "QUIZ_UNSUPPORTED_TYPE",
                         "expired": "QUIZ_REQUEST_EXPIRED", "cancelled": "QUIZ_REQUEST_EXPIRED"}.get(case)
            if rejection:
                self.cli("quiz.submit_answers", submit, error=rejection)
                check(self.evaluate("window.labSnapshot()")["submits"] == 0, "拒绝后发生了提交")
                check(self.evaluate("window.labSnapshot()")["inputs"] == 0, "拒绝后发生了输入")
                check(self.evaluate("window.labSnapshot()")["clicks"] == 0, "拒绝后发生了点击")
                break
            self.cli("quiz.validate_answers", answers)
            if case not in {"occluded"}:
                check(self.evaluate("window.labSnapshot()") == snapshot, "validate 操作改变了页面")
            first = self.cli("quiz.submit_answers", submit)
            duplicate = self.cli("quiz.submit_answers", submit)
            check(first == duplicate, "重复请求没有返回相同结果")
            result = wait_until(lambda: self.cli("quiz.get_result", {"requestId": request["requestId"]}),
                                lambda r: r["state"] in {"completed", "failed", "expired", "cancelled"})
            failure = {"occluded": "QUIZ_APPLY_FAILED", "prefilled": "QUIZ_APPLY_FAILED",
                       "server_error": "QUIZ_VERIFY_FAILED", "response_lost": "QUIZ_VERIFY_FAILED",
                       "late_response": "QUIZ_VERIFY_FAILED"}.get(case)
            if failure:
                check(result["state"] == "failed" and result["error"]["code"] == failure, result)
                expected_submits = int(case in {"server_error", "response_lost", "late_response"})
                check(len(record["submissions"]) == expected_submits, record["submissions"])
                if case == "late_response":
                    record["release"].set()
                    wait_until(lambda: self.evaluate("window.labSnapshot()"), lambda s: s["accepted"] == 1, timeout=10)
                self.cli("quiz.submit_answers", submit)  # 完成/失败后重放同一个幂等键仍不重发。
                check(self.evaluate("window.labSnapshot()")["submits"] == expected_submits, "失败后重复提交")
                break
            check(result["state"] == "completed" and result["result"] == {"completedCount": 3, "submitAttempts": 1}, result)
            check(len(record["submissions"]) == index + 1 and all(s["accepted"] for s in record["submissions"]), record["submissions"])
        if case in {"roundtrip", "timed", "long_narrow", "delayed", "slow_render", "rerender", "invalid", "multi_blank", "graded_wrong"}:
            final_task = wait_until(lambda: self.task(task_id), lambda t: t["state"] in {"completed", "failed", "cancelled"})
            check(final_task["state"] == "completed", final_task)
        browser_result = self.evaluate("window.labSnapshot()")
        check(browser_result["untrusted"] == 0, browser_result)
        check(browser_result["starts"] == int(case == "timed"), browser_result)
        check(browser_result["next"] == (1 if case == "roundtrip" else 0), browser_result)
        if case == "multi_blank":
            check(browser_result["lockedInputs"] == 3 and browser_result["answerResults"] == 3, browser_result)
        if case == "graded_wrong":
            check(browser_result["lockedInputs"] == 2 and browser_result["wrongResults"] == 2, browser_result)
            check(record["submissions"] and record["submissions"][0]["accepted"] and not record["submissions"][0]["correct"], record["submissions"])
        return {"case": case, "seed": seed, "passed": True, "browser": browser_result, "submissions": record["submissions"]}

    def close(self):
        for record in self.runs.values():
            record["release"].set()
        try:
            self.commands.AGENT_SERVICE.stop_course()
        finally:
            self.course_module.COURSE_TAB_URL_KEYWORD = self.original_keyword
            if hasattr(self, "original_find"):
                self.commands.backend.course_controller.find_course_tab = self.original_find
            if self.connection:
                self.connection.ws.close()
            if self.browser:
                self.browser.terminate()
                try:
                    self.browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.browser.kill()
                    self.browser.wait(timeout=5)
            for server in reversed(self.servers):
                server.shutdown()
                server.server_close()
            for thread in self.server_threads:
                thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="Chrome 或 Edge 可执行文件路径；默认自动检测")
    parser.add_argument("--show", action="store_true", help="显示隔离测试浏览器")
    parser.add_argument("--case", choices=CASES, action="append", help="只运行指定场景，可重复")
    parser.add_argument("--repeat", type=int, default=1, help="重复轮数，每轮变更题目和选项顺序")
    parser.add_argument("--seed", type=int, default=20260904)
    parser.add_argument("--output", type=Path, default=ROOT / ".course-lab")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat 必须大于 0")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    output = args.output.resolve() / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6])
    output.mkdir(parents=True)
    report = {"cases": [], "scope": "synthetic local Chromium + production service + CLI; no real platform/AI accuracy claim"}
    with tempfile.TemporaryDirectory(prefix="dgut-course-lab-", ignore_cleanup_errors=True) as folder:
        lab = Lab(args, Path(folder))
        report["browser"] = lab.browser_version
        try:
            for repetition in range(args.repeat):
                for case in args.case or CASES:
                    seed = args.seed + repetition
                    started = time.monotonic()
                    try:
                        row = lab.run(case, seed)
                    except Exception as error:
                        row = {"case": case, "seed": seed, "passed": False, "error": str(error)}
                        try:
                            capture = lab.cdp("Page.captureScreenshot", {"format": "png"})
                            (output / f"{case}-{seed}.png").write_bytes(base64.b64decode(capture["data"]))
                            row["browser"] = lab.evaluate("window.labSnapshot && window.labSnapshot()")
                            row["timeline"] = lab.evaluate("window.labTimeline")
                        except Exception as diagnostic_error:
                            row["diagnosticError"] = str(diagnostic_error)
                    finally:
                        try:
                            lab.commands.AGENT_SERVICE.stop_course()
                            wait_until(lambda: lab.commands.backend.course_controller._quiz_busy, lambda busy: not busy, timeout=10)
                        except Exception as cleanup_error:
                            row["passed"] = False
                            row["cleanupError"] = str(cleanup_error)
                    row["seconds"] = round(time.monotonic() - started, 2)
                    report["cases"].append(row)
                    # 全为本地合成数据；不保存 runtime 凭据、浏览器 profile 或任何真实课程。
                    (output / f"{case}-{seed}.json").write_text(json.dumps({"result": row, "cli": lab.trace,
                        "events": lab.commands.EVENT_BUFFER.wait_events(lab.event_start, 0, 100, ["course", "quiz", "session", "navigation", "recovery"])},
                        ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"{'通过' if row['passed'] else '失败'} | {case} | seed={seed} | {row['seconds']}s {row.get('error', '')}", flush=True)
        finally:
            lab.close()
            (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = sum(not row["passed"] for row in report["cases"])
    print(f"结果：{len(report['cases']) - failures}/{len(report['cases'])} 通过；报告：{output}")
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
