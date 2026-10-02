"""Read-only, privacy-filtered course sampling; entry point: backend/tools/quiz_probe.py."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from websocket import create_connection, WebSocketTimeoutException

try:
    from .capture_privacy import Redactor, render_snapshot
except ImportError:
    from capture_privacy import Redactor, render_snapshot

ROOT = Path(__file__).resolve().parents[2]
DOM_FUNCTION = Path(__file__).with_name('capture_dom.js').read_text(encoding='utf-8')
ALLOWED_METHODS = {'Page.enable', 'Runtime.enable', 'Network.enable', 'Network.disable', 'Page.getFrameTree',
    'Page.createIsolatedWorld', 'Page.addScriptToEvaluateOnNewDocument', 'Page.removeScriptToEvaluateOnNewDocument',
    'Page.captureScreenshot', 'Runtime.evaluate', 'Target.setAutoAttach', 'Target.detachFromTarget', 'Network.getResponseBody'}


class CaptureError(Exception):
    """Only fixed error codes are printed/exported; CDP error messages may contain page data."""


class CaptureConnection:
    def __init__(self, url):
        self.ws = create_connection(url, timeout=5, suppress_origin=True, http_proxy_host=None)
        self.events = queue.Queue(maxsize=4096)
        self.dropped = 0
        self.closed = False
        self._counter = 0
        self._condition = threading.Condition()
        self._send_lock = threading.Lock()
        self._pending = {}
        self._reader = threading.Thread(target=self._read, name='capture-cdp-reader', daemon=True)
        self._reader.start()

    def _read(self):
        try:
            while not self.closed:
                try:
                    data = json.loads(self.ws.recv())
                except WebSocketTimeoutException:
                    continue
                if 'id' in data:
                    with self._condition:
                        if data['id'] in self._pending:
                            self._pending[data['id']] = data
                            self._condition.notify_all()
                elif 'method' in data:
                    try:
                        self.events.put_nowait(data)
                    except queue.Full:
                        self.dropped += 1
        except Exception:
            pass
        finally:
            with self._condition:
                self.closed = True
                self._condition.notify_all()

    def call(self, method, params=None, session=None, timeout=5):
        if method not in ALLOWED_METHODS:
            raise CaptureError('NON_OBSERVATIONAL_METHOD_REFUSED')
        with self._send_lock:
            with self._condition:
                if self.closed:
                    raise CaptureError('CDP_DISCONNECTED')
                self._counter += 1
                identifier = self._counter
                self._pending[identifier] = None
            message = {'id': identifier, 'method': method, 'params': params or {}}
            if session is not None:
                message['sessionId'] = session
            try:
                self.ws.send(json.dumps(message))
            except Exception as error:
                with self._condition:
                    self._pending.pop(identifier, None)
                raise CaptureError('CDP_SEND_FAILED') from error
        with self._condition:
            ready = self._condition.wait_for(lambda: self._pending.get(identifier) is not None or self.closed, timeout)
            answer = self._pending.pop(identifier, None)
        if not ready or answer is None:
            raise CaptureError('CDP_DISCONNECTED' if self.closed else 'CDP_TIMEOUT')
        if 'error' in answer:
            raise CaptureError('CDP_COMMAND_REJECTED')
        return answer.get('result', {})

    def close(self):
        self.closed = True
        self.ws.close()
        self._reader.join(timeout=2)


def discover(port):
    if not 1 <= port <= 65535:
        raise CaptureError('INVALID_PORT')
    try:
        with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/json/list', timeout=5) as response:
            data = json.loads(response.read(1_048_577))
        return [t for t in data if t.get('type') == 'page']
    except Exception as error:
        raise CaptureError('DEBUG_BROWSER_UNAVAILABLE') from error


def select_target(targets, keyword, target_id=None):
    matches = [t for t in targets if keyword.lower() in t.get('url', '').lower()]
    if target_id:
        matches = [t for t in matches if t.get('id') == target_id]
    if len(matches) != 1:
        raise CaptureError('TARGET_NOT_FOUND' if not matches else 'MULTIPLE_TARGETS_USE_TARGET_ID')
    target = matches[0]
    parsed = urlsplit(target.get('webSocketDebuggerUrl', ''))
    if parsed.scheme != 'ws' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise CaptureError('NON_LOCAL_DEBUG_TARGET_REFUSED')
    return target


def changes(previous, current):
    if previous is None or previous.get('documentEpoch') != current.get('documentEpoch'):
        return {'documentChanged': previous is not None, 'added': len(current.get('nodes', [])), 'removed': 0, 'changed': []}
    before = {n['node']: n for n in previous.get('nodes', [])}
    after = {n['node']: n for n in current.get('nodes', [])}
    modified = []
    for identifier in before.keys() & after.keys():
        fields = [k for k in after[identifier] if k != 'node' and before[identifier].get(k) != after[identifier][k]]
        if fields:
            modified.append({'node': identifier, 'fields': fields})
    return {'documentChanged': False, 'added': len(after.keys() - before.keys()), 'removed': len(before.keys() - after.keys()),
            'changed': modified[:500], 'changesTruncated': len(modified) > 500}


class Recorder:
    def __init__(self, target, output, *, max_nodes=5000, max_snapshots=60, private_screenshots=False):
        self.redactor = Redactor()
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.json_output = self.output / 'json'
        self.json_output.mkdir()
        self.connection = CaptureConnection(target['webSocketDebuggerUrl'])
        self.scope = urlsplit(target['url'])
        self.key = '__dgut_capture_' + secrets.token_hex(10)
        self.max_nodes, self.max_snapshots = max_nodes, max_snapshots
        self.private_screenshots = private_screenshots
        self.sessions = {None: {'enabled': False}}
        self.contexts = {}
        self.worlds = {}
        self.network = {}
        self.previous = {}
        self.snapshots = []
        self.issues = []
        self.event_count = 0
        self.started = time.monotonic()
        self.last_revision = {}
        self.frame_inventory = {}
        self.network_omitted = 0
        self.last_snapshot_time = 0.0
        self.closed = False
        self.stopped_reason = 'completed'

    def script(self, mode):
        return '(' + DOM_FUNCTION + ')(' + json.dumps(self.key) + ',' + json.dumps(mode) + ',' + str(self.max_nodes) + ')'

    def issue(self, code, **details):
        item = {'code': code, **details}
        if item not in self.issues:
            self.issues.append(item)

    def event(self, category, data):
        if self.event_count >= 20000:
            self.issue('EVENT_FILE_LIMIT')
            return
        self.event_count += 1
        item = {'seq': self.event_count, 'elapsedMs': round((time.monotonic() - self.started) * 1000), 'category': category, **data}
        with (self.json_output / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + '\n')

    def enable(self, session):
        info = self.sessions[session]
        info['enabled'] = True
        try:
            for domain in ('Page', 'Runtime', 'Network'):
                params = {'maxTotalBufferSize': 8_388_608, 'maxResourceBufferSize': 1_048_576, 'maxPostDataSize': 1_048_576} if domain == 'Network' else {}
                self.connection.call(domain + '.enable', params, session)
            self.connection.call('Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': False, 'flatten': True}, session)
            info['scriptId'] = self.connection.call('Page.addScriptToEvaluateOnNewDocument', {'source': self.script('observe')}, session)['identifier']
        except CaptureError as error:
            self.issue(str(error), area='enable', session=self.redactor.alias(session, 'session'))

    def process_events(self):
        # Fixed batch bound avoids starving keyboard commands on noisy pages.
        for _ in range(4096):
            try:
                event = self.connection.events.get_nowait()
            except queue.Empty:
                break
            method, p, session = event['method'], event.get('params', {}), event.get('sessionId')
            if method == 'Page.frameNavigated' and session is None and not p.get('frame', {}).get('parentId'):
                page = urlsplit(p['frame'].get('url', ''))
                if (page.scheme, page.netloc, page.path) != (self.scope.scheme, self.scope.netloc, self.scope.path):
                    self.stopped_reason = 'TARGET_SCOPE_CHANGED'
                    raise CaptureError('TARGET_SCOPE_CHANGED')
            elif method == 'Target.attachedToTarget':
                sid = p['sessionId']
                if p.get('targetInfo', {}).get('type') == 'iframe':
                    self.sessions[sid] = {'enabled': False, 'parentSession': session}
                else:
                    try:
                        self.connection.call('Target.detachFromTarget', {'sessionId': sid}, session)
                    except CaptureError:
                        pass
            elif method == 'Target.detachedFromTarget':
                removed = {p.get('sessionId')}
                while True:
                    children = {sid for sid, info in self.sessions.items() if sid is not None and info.get('parentSession') in removed}
                    if children <= removed:
                        break
                    removed.update(children)
                for sid in removed:
                    self.sessions.pop(sid, None)
                for key in list(self.contexts):
                    if key[0] in removed:
                        self.contexts.pop(key, None)
                        self.worlds.pop(key, None)
            elif method == 'Runtime.executionContextCreated':
                context = p['context']
                aux = context.get('auxData', {})
                if aux.get('isDefault') and aux.get('frameId'):
                    self.contexts[(session, aux['frameId'])] = context['id']
            elif method in {'Runtime.executionContextsCleared', 'Runtime.executionContextDestroyed'}:
                for key, value in list(self.contexts.items()):
                    if key[0] == session and (method.endswith('Cleared') or value == p.get('executionContextId')):
                        self.contexts.pop(key, None)
                        self.worlds.pop(key, None)
            elif method.startswith('Network.'):
                self.network_event(method, p, session)

    def network_event(self, method, p, session):
        key = (session, p.get('requestId'))
        if method == 'Network.requestWillBeSent':
            if p.get('type') not in {'XHR', 'Fetch', 'Document'}:
                return
            if len(self.network) >= 500:
                self.network_omitted += 1
                return
            request = p['request']
            content_type = next((str(value) for name, value in request.get('headers', {}).items() if name.lower() == 'content-type'), '')
            # Redirect hops share a CDP ID; preserve the prior sanitized hop.
            previous = self.network.get(key)
            if previous:
                self.event('network-redirect', {'request': previous['id'], 'status': p.get('redirectResponse', {}).get('status')})
            record = {'id': self.redactor.alias(str(key) + str(p.get('timestamp')), 'request'), 'resourceType': p['type'],
                      'method': request['method'], 'url': self.redactor.url(request['url']), 'frame': self.redactor.alias(p.get('frameId', ''), 'frame'),
                      'requestBody': self.redactor.body(request.get('postData'), content_type) if request.get('postData') is not None
                                     else {'state': 'unavailable' if request.get('hasPostData') else 'empty'}, 'responseBody': {'state': 'pending'}}
            self.network[key] = record
            self.event('network-request', {'request': record['id'], 'method': record['method'], 'url': record['url']})
        elif key in self.network:
            record = self.network[key]
            if method == 'Network.responseReceived':
                response = p['response']
                record.update(status=response['status'], mimeType=response.get('mimeType', '').split(';')[0][:80], fromCache=bool(response.get('fromDiskCache')))
            elif method == 'Network.loadingFailed':
                record.update(failed=True, responseBody={'state': 'unavailable', 'reason': 'loading-failed'})
                self.event('network-failed', {'request': record['id'], 'cancelled': bool(p.get('canceled'))})
            elif method == 'Network.loadingFinished':
                length = p.get('encodedDataLength', 0)
                record['encodedBytes'] = length
                if length > 1_048_576 or 'json' not in record.get('mimeType', ''):
                    record['responseBody'] = {'state': 'omitted', 'reason': 'size-limit' if length > 1_048_576 else 'not-json-mime'}
                else:
                    try:
                        body = self.connection.call('Network.getResponseBody', {'requestId': p['requestId']}, session)
                        raw = base64.b64decode(body['body']).decode('utf-8') if body.get('base64Encoded') else body['body']
                        record['responseBody'] = self.redactor.body(raw)
                    except Exception:
                        record['responseBody'] = {'state': 'unavailable', 'reason': 'body-not-retained'}
                self.event('network-complete', {'request': record['id'], 'status': record.get('status'), 'bodyState': record['responseBody']['state']})

    def refresh(self):
        for _ in range(5):
            self.process_events()
            pending = [sid for sid, info in self.sessions.items() if not info['enabled']]
            if not pending:
                break
            for sid in pending:
                self.enable(sid)
            time.sleep(0.05)
        self.process_events()
        inventory = {}

        def walk(tree, session, parent=None, root=False):
            frame = tree['frame']
            existing = inventory.get(frame['id'])
            value = {'id': frame['id'], 'session': session, 'parent': parent or frame.get('parentId') or (existing or {}).get('parent'), 'url': frame.get('url', '')}
            if not existing or root:
                inventory[frame['id']] = value
            for child in tree.get('childFrames', []):
                walk(child, session, frame['id'])

        for sid in list(self.sessions):
            if sid not in self.sessions:
                continue
            try:
                tree = self.connection.call('Page.getFrameTree', session=sid)['frameTree']
                if sid is None:
                    page = urlsplit(tree['frame'].get('url', ''))
                    if (page.scheme, page.netloc, page.path) != (self.scope.scheme, self.scope.netloc, self.scope.path):
                        self.stopped_reason = 'TARGET_SCOPE_CHANGED'
                        raise CaptureError('TARGET_SCOPE_CHANGED')
                walk(tree, sid, root=True)
            except CaptureError as error:
                if str(error) == 'TARGET_SCOPE_CHANGED':
                    raise
                self.process_events()
                if sid is not None and sid not in self.sessions:
                    continue  # A normal reload may detach an old OOPIF mid-enumeration.
                self.issue(str(error), area='frame-tree', session=self.redactor.alias(sid, 'session'))
        self.process_events()
        self.frame_inventory = inventory
        return inventory

    def evaluate(self, frame, mode):
        key = (frame['session'], frame['id'])
        context = self.contexts.get(key)
        isolated = False
        if context is None:
            context = self.connection.call('Page.createIsolatedWorld', {'frameId': frame['id'], 'worldName': self.key}, frame['session'])['executionContextId']
            self.contexts[key] = context
            self.worlds[key] = True
        isolated = self.worlds.get(key, False)
        result = self.connection.call('Runtime.evaluate', {'expression': self.script(mode), 'contextId': context, 'returnByValue': True}, frame['session'])
        if 'exceptionDetails' in result or 'value' not in result.get('result', {}):
            raise CaptureError('FRAME_EVALUATION_FAILED')
        return result['result']['value'], isolated

    def poll(self):
        before = {key: value['url'] for key, value in self.frame_inventory.items()}
        inventory = self.refresh()
        dirty = before != {key: value['url'] for key, value in inventory.items()}
        for frame in list(inventory.values())[:64]:
            alias = self.redactor.alias(frame['id'], 'frame')
            try:
                raw, _ = self.evaluate(frame, 'drain')
                if raw['dropped']:
                    self.issue('PAGE_EVENTS_DROPPED', frame=alias, count=raw['dropped'])
                if raw['revision'] != self.last_revision.get(alias, -1):
                    dirty = True
                self.last_revision[alias] = raw['revision']
                for event in raw['events']:
                    safe = {k: v for k, v in event.items() if k in {'type', 'node', 'nodes', 'at', 'trusted', 'x', 'y', 'scrollX', 'scrollY', 'count'}}
                    if 'attributes' in event:
                        safe['attributes'] = [self.redactor.symbol(n) for n in event['attributes']]
                    self.event('page', {'frame': alias, **safe})
            except CaptureError:
                dirty = True  # Snapshot will record the precise coverage failure.
        return dirty

    def capture(self, label='manual'):
        if len(self.snapshots) >= self.max_snapshots:
            self.issue('SNAPSHOT_LIMIT')
            return None
        inventory = self.refresh()
        number = len(self.snapshots) + 1
        captured, failures, diffs = [], [], {}
        for frame in list(inventory.values())[:64]:
            alias = self.redactor.alias(frame['id'], 'frame')
            try:
                raw, isolated = self.evaluate(frame, 'snapshot')
                safe = self.redactor.snapshot(raw)
                safe.update(frame=alias, parent=self.redactor.alias(frame['parent'], 'frame') if frame['parent'] else None,
                            context='isolated-dom-only' if isolated else 'default', documentEpoch=raw.get('documentEpoch'))
                if isolated:
                    self.issue('VIEW_MODEL_NOT_AVAILABLE_IN_ISOLATED_CONTEXT', frame=alias)
                if any(safe['limits'].values()):
                    self.issue('DOM_TRUNCATED', frame=alias)
                diffs[alias] = changes(self.previous.get(alias), safe)
                self.previous[alias] = safe
                captured.append(safe)
                (self.output / f'{number:03d}-{alias}.html').write_text(render_snapshot(safe), encoding='utf-8')
            except CaptureError as error:
                failures.append({'frame': alias, 'code': str(error)})
        expected = len(inventory)
        complete = expected > 0 and len(captured) == expected and not failures and not any(any(f['limits'].values()) for f in captured)
        stages = {'initial', 'auto', 'manual', 'final', 'before', 'selected', 'filled', 'submitted', 'result', 'next', 'error'}
        snapshot = {'schemaVersion': 1, 'number': number, 'label': label if label in stages else self.redactor.text(label),
                    'elapsedMs': round((time.monotonic() - self.started) * 1000), 'frames': captured, 'diff': diffs,
                    'coverage': {'expected': expected, 'captured': len(captured), 'failed': failures, 'frameLimit': expected > 64, 'complete': complete}}
        file = f'{number:03d}-snapshot.json'
        (self.json_output / file).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        if self.private_screenshots:
            private = self.output / 'private'
            private.mkdir(exist_ok=True)
            try:
                image = self.connection.call('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False})
                (private / f'{number:03d}.png').write_bytes(base64.b64decode(image['data']))
            except CaptureError:
                self.issue('PRIVATE_SCREENSHOT_FAILED', snapshot=number)
        self.snapshots.append({'number': number, 'file': file, 'coverage': snapshot['coverage']})
        self.last_snapshot_time = time.monotonic()
        self.event('snapshot', {'number': number, 'file': file, 'coverageComplete': complete})
        self.flush()
        return snapshot

    def flush(self):
        network = list(self.network.values())
        quality = {'snapshotCount': len(self.snapshots), 'allFramesCaptured': bool(self.snapshots) and all(s['coverage']['complete'] for s in self.snapshots),
                   'cdpEventsDropped': self.connection.dropped, 'networkOmitted': self.network_omitted,
                   'networkBodiesUnavailable': sum(r['responseBody']['state'] in {'pending', 'unavailable'} for r in network)}
        manifest = {'schemaVersion': 1, 'createdAt': datetime.now(timezone.utc).isoformat(), 'scope': self.redactor.url(self.scope.geturl()),
                    'privacy': {'pageText': 'pseudonymized-except-ui-labels', 'inputValues': 'pseudonymized', 'headers': 'not-exported',
                                'networkBodies': 'structure-only', 'privateScreenshots': self.private_screenshots},
                    'snapshots': self.snapshots, 'network': network, 'quality': quality, 'issues': self.issues, 'stopReason': self.stopped_reason,
                    'limitations': ['Static HTML is a structural fixture, not a replay of platform business logic.',
                                    'Closed shadow roots, pixels inside images/canvas and truncated data are not reconstructed.',
                                    'Only requests observed after sampling started are included; no authenticated requests are replayed.']}
        (self.json_output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            try:
                try:
                    self.process_events()
                except CaptureError:
                    pass  # Scope change must not prevent observer cleanup.
                for frame in self.frame_inventory.values():
                    try:
                        self.evaluate(frame, 'cleanup')
                    except CaptureError:
                        pass
                for sid, info in reversed(list(self.sessions.items())):
                    try:
                        if info.get('scriptId'):
                            self.connection.call('Page.removeScriptToEvaluateOnNewDocument', {'identifier': info['scriptId']}, sid)
                        self.connection.call('Target.setAutoAttach', {'autoAttach': False, 'waitForDebuggerOnStart': False, 'flatten': True}, sid)
                    except CaptureError:
                        pass
            finally:
                self.flush()
        finally:
            self.connection.close()


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='只读课件采样：操作前后快照、iframe、事件与脱敏接口结构。')
    parser.add_argument('keyword', nargs='?', default='ua.dgut.edu.cn/learnCourse', help='目标 URL 关键字，必须唯一匹配')
    parser.add_argument('--port', type=int, default=9222)
    parser.add_argument('--target-id', help='多个匹配页时精确指定调试标签 ID')
    parser.add_argument('--list', action='store_true', help='只列出匹配页的 ID 和脱敏 URL，不列出其他标签')
    parser.add_argument('--once', action='store_true', help='采集一个静态快照后退出')
    parser.add_argument('--duration', type=float, help='自动观察秒数；不指定则回车打点、q 结束')
    parser.add_argument('--interval', type=float, default=2, help='自动快照最小间隔（秒）')
    parser.add_argument('--checkpoint', type=float, default=15, help='即使没有 DOM 事件也定期保存状态（秒，默认 15）')
    parser.add_argument('--max-nodes', type=int, default=5000)
    parser.add_argument('--max-snapshots', type=int, default=60)
    parser.add_argument('--output', type=Path, default=ROOT / '.course-captures')
    parser.add_argument('--private-screenshots', action='store_true', help='额外保存未脱敏截图到 private/，可能含真实课程/账户文字，仅本地使用')
    args = parser.parse_args(argv)
    if not 100 <= args.max_nodes <= 20000 or not 1 <= args.max_snapshots <= 300 or not .5 <= args.interval <= 3600 or not .5 <= args.checkpoint <= 3600 or args.duration is not None and not 0 < args.duration <= 3600:
        parser.error('节点 100–20000；快照 1–300；间隔至少 0.5 秒；时长 0–3600 秒。')
    recorder = None
    try:
        targets = discover(args.port)
        if args.list:
            redactor = Redactor()
            print(json.dumps([{'targetId': t['id'], 'url': redactor.url(t['url'])} for t in targets if args.keyword.lower() in t.get('url', '').lower()], ensure_ascii=False, indent=2))
            return 0
        target = select_target(targets, args.keyword, args.target_id)
        output = args.output.resolve() / (time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
        recorder = Recorder(target, output, max_nodes=args.max_nodes, max_snapshots=args.max_snapshots, private_screenshots=args.private_screenshots)
        first = recorder.capture('initial')
        print(f"已绑定目标。初始框架：{first['coverage']['captured']}/{first['coverage']['expected']}；样本：{output}", flush=True)
        commands = queue.Queue()
        if not args.once and args.duration is None:
            if not sys.stdin.isatty():
                raise CaptureError('USE_ONCE_OR_DURATION_WITH_NONINTERACTIVE_INPUT')
            print('请在浏览器正常操作；回车保存快照，可输入阶段名称；q 结束。脚本不作答、不提交。', flush=True)
            def keyboard():
                try:
                    while True:
                        line = input()
                        commands.put(line)
                        if line.strip().lower() == 'q':
                            return
                except EOFError:
                    commands.put('q')
            threading.Thread(target=keyboard, daemon=True).start()
        deadline = time.monotonic() + args.duration if args.duration else None
        dirty = False
        while not args.once and (deadline is None or time.monotonic() < deadline):
            dirty = recorder.poll() or dirty
            try:
                command = commands.get_nowait()
            except queue.Empty:
                command = None
            if command is not None and command.strip().lower() == 'q':
                break
            elapsed = time.monotonic() - recorder.last_snapshot_time
            if command is not None or dirty and elapsed >= args.interval or elapsed >= args.checkpoint:
                result = recorder.capture(command or 'auto')
                if result:
                    print(f"快照 {result['number']}：框架 {result['coverage']['captured']}/{result['coverage']['expected']}", flush=True)
                dirty = False
            time.sleep(0.2)
        if not args.once:
            recorder.poll()
            recorder.capture('final')
        recorder.close()
        print(f'采样完成：{recorder.json_output / "manifest.json"}')
        return 0 if all(s['coverage']['complete'] for s in recorder.snapshots) and not recorder.issues and not recorder.connection.dropped else 2
    except KeyboardInterrupt:
        if recorder:
            recorder.stopped_reason = 'interrupted'
        return 130
    except Exception as error:
        code = str(error) if isinstance(error, CaptureError) else type(error).__name__
        print('采样停止：' + code, file=sys.stderr)
        if recorder:
            recorder.stopped_reason = code
        return 1
    finally:
        if recorder:
            recorder.close()


if __name__ == '__main__':
    raise SystemExit(main())
