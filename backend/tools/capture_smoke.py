"""Synthetic Chromium acceptance test for the passive sampler; never uses a real account."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import threading
import time

from course_capture import Recorder, discover, select_target
from quiz_probe import TabConnection

ROOT = Path(__file__).resolve().parents[2]
SECRET = 'CAPTURE_PRIVATE_MARKER_123456789'


def until(read, test, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = read()
            if test(result):
                return result
        except (ConnectionError, OSError):
            pass
        time.sleep(.1)
    raise AssertionError('Synthetic fixture did not reach the required state')


def run(browser):
    class Site(SimpleHTTPRequestHandler):
        submissions = 0

        def log_message(self, *_):
            pass

        def handle(self):
            try:
                super().handle()
            except (ConnectionResetError, BrokenPipeError):
                pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            Site.submissions += 1
            raw = json.dumps({'ok': True, 'status': 1, 'message': SECRET, 'token': SECRET}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Site, directory=str(ROOT / 'backend' / 'quiz_simulator')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {'browser': Path(browser).name, 'checks': []}
    process = driver = recorder = None
    temporary_directory = tempfile.TemporaryDirectory(prefix='dgut-capture-test-', ignore_cleanup_errors=True)
    try:
        with nullcontext(temporary_directory.name) as temporary:
            root = Path(temporary)
            profile = root / 'profile'
            url = f'http://127.0.0.1:{server.server_port}/capture-course.html'
            process = subprocess.Popen([browser, '--headless=new', '--no-first-run', '--no-default-browser-check',
                '--remote-debugging-port=0', '--remote-allow-origins=*', '--site-per-process',
                '--user-data-dir=' + str(profile), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            port_file = profile / 'DevToolsActivePort'
            until(port_file.exists, bool)
            port = int(port_file.read_text().splitlines()[0])
            target = until(lambda: discover(port), lambda items: any('capture-course.html' in t['url'] for t in items))
            target = select_target(target, 'capture-course.html')
            driver = TabConnection(target['webSocketDebuggerUrl'])

            def call(method, params=None):
                result, error = driver.send(method, params)
                assert error is None, error
                return result

            def evaluate(expression):
                value = call('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
                assert 'exceptionDetails' not in value, value
                return value['result'].get('value')

            def click(selector):
                point = evaluate('(()=>{const r=document.querySelector(' + json.dumps(selector) + ').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()')
                for kind in ['mousePressed', 'mouseReleased']:
                    call('Input.dispatchMouseEvent', {'type': kind, **point, 'button': 'left', 'clickCount': 1})

            until(lambda: evaluate('location.href + "|" + document.readyState'), lambda value: value == url + '|complete')
            recorder = Recorder(target, root / 'capture')
            before = recorder.capture('before')
            assert before['coverage']['captured'] == 4, before['coverage']
            assert all(f['context'] == 'default' for f in before['frames'])
            assert len({f['session'] for f in recorder.frame_inventory.values()}) >= 2
            assert any(n.get('openShadowRoot') for f in before['frames'] for n in f['nodes'])
            assert evaluate('interactions') == {'clicks': 0, 'inputs': 0, 'submits': 0}
            recorder.poll()
            assert Site.submissions == 0
            report['checks'].append('four-frames-including-nested-oopif; shadow-dom; passive-zero-actions')

            click('.choice-item')
            click('input.answer-width')
            call('Input.insertText', {'text': SECRET})
            recorder.poll()
            filled = recorder.capture('filled')
            assert any('value' in d['fields'] for changes in filled['diff'].values() for d in changes['changed'])
            click('.btn-submit')
            until(lambda: evaluate("document.querySelector('.question-wrapper').classList.contains('finished')"), bool)
            until(lambda: (recorder.poll(), list(recorder.network.values()))[1],
                  lambda records: any(r['responseBody']['state'] == 'captured' for r in records))
            after = recorder.capture('result')
            assert Site.submissions == 1
            assert any(f['viewModel'].get('currentPage', {}).get('record', {}).get('status') is True for f in after['frames'])
            request = next(r for r in recorder.network.values() if r['method'] == 'POST')
            assert request['requestBody']['schema']['properties']['answers']['type'] == 'array'
            assert request['responseBody']['outcome']['ok'] is True
            assert any(d['changed'] for d in after['diff'].values())
            report['checks'].append('trusted-user-input; value-state-diff; request-response-schema; completion-state')

            call('Page.navigate', {'url': url + '?reload=1'})
            until(lambda: evaluate('location.href + "|" + document.readyState'), lambda value: value == url + '?reload=1|complete')
            reloaded = recorder.capture('next')
            assert reloaded['coverage']['captured'] == 4, reloaded['coverage']
            assert any(d['documentChanged'] for d in reloaded['diff'].values())
            assert evaluate('interactions') == {'clicks': 0, 'inputs': 0, 'submits': 0}
            key = recorder.key
            recorder.close()
            assert evaluate('Object.hasOwn(window,' + json.dumps(key) + ')') is False
            manifest = json.loads((root / 'capture/json/manifest.json').read_text(encoding='utf-8'))
            assert manifest['quality']['allFramesCaptured'], manifest['quality']
            assert not manifest['issues'], manifest['issues']
            assert not list((root / 'capture').glob('*.json*'))
            assert (root / 'capture/json/events.jsonl').is_file()
            assert all((root / 'capture/json' / s['file']).is_file() for s in manifest['snapshots'])
            exported = '\n'.join(p.read_text(encoding='utf-8') for p in (root / 'capture').rglob('*') if p.is_file())
            assert SECRET not in exported and 'Bearer ' not in exported
            assert '"type": "click"' in exported and '"type": "input"' in exported
            assert not (root / 'capture/private').exists()
            report['checks'].append('reload-context-recovery; observer-cleanup; no-secret-on-disk; no-default-screenshot')

            result = subprocess.run([shutil.which('python') or sys.executable, str(ROOT / 'backend' / 'tools' / 'quiz_probe.py'), 'capture-course.html',
                '--port', str(port), '--once', '--output', str(root / 'cli')], capture_output=True, timeout=40)
            assert result.returncode == 0, result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')
            evaluate("(()=>{for(let i=0;i<150;i++)document.body.append(document.createElement('div'))})()")
            limited = Recorder(target, root / 'limited', max_nodes=100)
            try:
                assert limited.capture()['coverage']['complete'] is False
                assert any(i['code'] == 'DOM_TRUNCATED' for i in limited.issues)
            finally:
                limited.close()
            report['checks'].append('public-cli; explicit-truncation-report')
            report['passed'] = True
    finally:
        if recorder:
            recorder.close()
        if driver:
            driver.ws.close()
        if process:
            process.terminate()
            process.wait(timeout=10)
        server.shutdown()
        server.server_close()
        temporary_directory.cleanup()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', required=True, type=Path)
    args = parser.parse_args()
    report = run(str(args.browser.resolve()))
    output = ROOT / '.course-lab' / ('capture-' + args.browser.stem + '.json')
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
