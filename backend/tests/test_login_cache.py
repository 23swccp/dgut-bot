import json
from unittest.mock import patch

import pytest

from dgutbot.domain.yxy_backend import Course, SignBackend


def make_backend(root):
    return SignBackend(lambda *_: None, root=root)


def save(backend, token='test-token', user_id=123):
    backend._apply_token(token, user_id, display_name='测试用户')
    backend._save_login_cache()


def test_json_contains_credentials_and_timestamp_without_renewing_same_login(tmp_path):
    backend = make_backend(tmp_path)
    with patch('dgutbot.domain.yxy_backend.time.time', return_value=100):
        save(backend)
    saved = json.loads((tmp_path / 'login-cache.json').read_text(encoding='utf-8'))
    assert saved['token'] == 'test-token'
    assert saved['saved_at'] == 100
    with patch('dgutbot.domain.yxy_backend.time.time', return_value=200):
        save(backend)
    assert json.loads((tmp_path / 'login-cache.json').read_text(encoding='utf-8'))['saved_at'] == 100


def test_json_cache_is_not_used_after_one_day(tmp_path):
    backend = make_backend(tmp_path)
    with patch('dgutbot.domain.yxy_backend.time.time', return_value=100):
        save(backend)
    with patch('dgutbot.domain.yxy_backend.time.time', return_value=100 + 86400 - 1):
        assert backend._load_login_cache()['token'] == 'test-token'
    with patch('dgutbot.domain.yxy_backend.time.time', return_value=100 + 86400):
        assert backend._load_login_cache() == {}


def test_next_start_restores_cached_login_without_entering_credentials(tmp_path):
    save(make_backend(tmp_path))
    backend = make_backend(tmp_path)
    with patch.object(backend, '_get_ws_url', return_value='ws://test'), \
         patch.object(backend, '_cookies', return_value=[]), \
         patch.object(backend, '_restore_browser_login') as restore, \
         patch.object(backend, '_fetch_courses', return_value=[Course(1, '课程')]):
        assert backend.load_session_and_courses(1, True)
    restore.assert_called_once_with('ws://test', 'test-token', 123)
    assert backend.login_status()['authenticated']
    assert backend.user_id == 123


@pytest.mark.parametrize('error, retained', [(RuntimeError('HTTP 401'), False),
                                           (RuntimeError('network unavailable'), True)])
def test_server_rejection_clears_cache_but_network_failure_keeps_it(tmp_path, error, retained):
    save(make_backend(tmp_path))
    backend = make_backend(tmp_path)
    with patch.object(backend, '_get_ws_url', return_value='ws://test'), \
         patch.object(backend, '_cookies', return_value=[]), \
         patch.object(backend, '_restore_browser_login'), \
         patch.object(backend, '_fetch_courses', side_effect=error):
        assert not backend.load_session_and_courses(1, True)
    assert bool(backend._load_login_cache()) == retained


def test_new_browser_account_replaces_cache(tmp_path):
    save(make_backend(tmp_path), token='old-token', user_id=1)
    backend = make_backend(tmp_path)
    cookies = [{'domain': '.dgut.edu.cn', 'name': 'AUTHORIZATION', 'value': 'new-token'},
               {'domain': '.dgut.edu.cn', 'name': 'userid', 'value': '2'}]
    with patch.object(backend, '_get_ws_url', return_value='ws://test'), \
         patch.object(backend, '_cookies', return_value=cookies), \
         patch.object(backend, '_fetch_courses', return_value=[Course(2, '新课程')]):
        assert backend.load_session_and_courses(1, True)
    assert backend._load_login_cache()['token'] == 'new-token'
    assert backend.user_id == 2
