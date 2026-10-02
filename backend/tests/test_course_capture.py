import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from capture_privacy import Redactor, render_snapshot
from course_capture import CaptureError, changes, select_target, CaptureConnection


def test_selection_never_falls_back_and_ambiguity_requires_exact_id():
    unrelated = {'id': 'other', 'url': 'https://mail.example.invalid', 'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/other'}
    selected = {**unrelated, 'id': 'chosen', 'url': 'https://ua.dgut.edu.cn/learnCourse'}
    with pytest.raises(CaptureError, match='TARGET_NOT_FOUND'):
        select_target([unrelated], 'learnCourse')
    with pytest.raises(CaptureError, match='MULTIPLE_TARGETS'):
        select_target([selected, {**selected, 'id': 'second'}], 'learnCourse')
    assert select_target([unrelated, selected], 'learnCourse', 'chosen') == selected
    with pytest.raises(CaptureError, match='NON_LOCAL'):
        select_target([{**selected, 'webSocketDebuggerUrl': 'ws://outside.invalid/'}], 'learnCourse')


def test_body_values_and_url_secrets_do_not_escape():
    r = Redactor()
    secret = 'SENSITIVE_SENTINEL_123456789'
    body = r.body(json.dumps({'token': secret, 'answers': [{'questionId': secret, 'value': secret}], 'ok': True}))
    url = r.url(f'https://user:{secret}@example.invalid/api/{secret}/submit?token={secret}#{secret}')
    encoded = json.dumps([body, url])
    assert secret not in encoded and 'user:' not in encoded
    assert body['schema']['properties']['token'] == {'type': 'redacted'}
    assert body['outcome'] == {'ok': True}
    assert url['path'].startswith('/api/segment_')
    assert body['schema']['properties']['answers']['variants'][0]['properties']['value']['type'] == 'string'
    assert r.body('<html>' + secret)['reason'] == 'not-json'
    assert r.body('x' * 1_048_577)['reason'] == 'size-limit'


def test_html_and_snapshot_omit_text_bindings_values_and_resources():
    r = Redactor()
    secret = 'PRIVATE_MARKER_987654321'
    raw = {'readyState': 'complete', 'viewport': {}, 'features': {}, 'limits': {}, 'revision': 1,
           'url': 'https://example.invalid/' + secret, 'viewModel': {'currentPage': {'id': secret, 'record': {'status': True}}},
           'nodes': [{'node': 1, 'parent': None, 'tag': 'input', 'value': secret,
                      'attrs': {'id': secret, 'class': 'answer-width selected', 'src': 'https://example.invalid/' + secret,
                                'data-bind': "click: submit('" + secret + "'), text: question.title"}, 'styles': {}},
                     {'node': 2, 'parent': None, 'tag': '#text', 'text': secret}]}
    safe = r.snapshot(raw)
    rendered = render_snapshot(safe)
    assert secret not in json.dumps(safe) + rendered
    assert 'answer-width selected' in rendered
    assert 'question.title' in rendered
    assert '<script' not in rendered and 'src=' not in rendered
    assert safe['viewModel']['currentPage']['record']['status'] is True
    assert r.alias(secret) == r.alias(secret)
    assert r.alias(secret) != Redactor().alias(secret)


def test_diff_includes_checked_focus_values_and_document_replacement():
    before = {'documentEpoch': 1, 'nodes': [{'node': 1, 'checked': False, 'focused': False, 'value': ''}]}
    after = {'documentEpoch': 1, 'nodes': [{'node': 1, 'checked': True, 'focused': True, 'value': '[input]'}]}
    delta = changes(before, after)
    assert set(delta['changed'][0]['fields']) == {'checked', 'focused', 'value'}
    assert changes(before, {**after, 'documentEpoch': 2})['documentChanged']


def test_capture_connection_refuses_mutating_cdp_methods_before_transport():
    connection = object.__new__(CaptureConnection)
    for method in ['Input.dispatchMouseEvent', 'Input.insertText', 'Page.navigate', 'Network.setCookie', 'Runtime.callFunctionOn']:
        with pytest.raises(CaptureError, match='NON_OBSERVATIONAL'):
            connection.call(method)


def test_form_encoded_and_query_schema_keep_structure_without_values():
    from urllib.parse import urlencode
    secret = 'PRIVATE_FORM_SENTINEL_123456789'
    redactor = Redactor()
    result = redactor.body(urlencode({'token': secret, 'record': json.dumps({'answers': [secret]})}),
                           'application/x-www-form-urlencoded; charset=UTF-8')
    assert result['encoding'] == 'form-urlencoded'
    assert result['schema']['properties']['record']['variants'][0]['properties']['answers']['type'] == 'array'
    assert result['schema']['properties']['token']['type'] == 'redacted'
    assert secret not in json.dumps(result)
    query = redactor.url('https://example.invalid/api?' + urlencode({'token': secret, 'page': secret}))
    assert 'page' in query['querySchema']['properties'] and secret not in json.dumps(query)


def test_nested_detach_removes_descendant_sessions_and_contexts():
    import queue
    from types import SimpleNamespace
    from course_capture import Recorder
    recorder = object.__new__(Recorder)
    recorder.connection = SimpleNamespace(events=queue.Queue())
    recorder.sessions = {None: {}, 'parent': {'parentSession': None}, 'child': {'parentSession': 'parent'}}
    recorder.contexts = {('child', 'frame'): 7}
    recorder.worlds = {('child', 'frame'): True}
    recorder.connection.events.put({'method': 'Target.detachedFromTarget', 'params': {'sessionId': 'parent'}})
    recorder.process_events()
    assert recorder.sessions == {None: {}} and not recorder.contexts and not recorder.worlds
