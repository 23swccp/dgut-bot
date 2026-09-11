import io
import json
import unittest
from unittest.mock import patch

from dgutbot.app.browser_lifetime import BrowserLifetime


class BrowserLifetimeTests(unittest.TestCase):
    def test_never_attached_service_does_not_exit(self):
        watch = BrowserLifetime(9222, 1420)
        with patch.object(watch, '_read_presence', return_value=False):
            self.assertFalse(watch.closed(0))
            self.assertFalse(watch.closed(1000))

    def test_closed_tab_is_confirmed_in_two_polls_without_heartbeat(self):
        watch = BrowserLifetime(9222, 1420)
        with patch.object(watch, '_read_presence', side_effect=[True, False, False]):
            self.assertFalse(watch.closed(0))
            self.assertFalse(watch.closed(1))
            self.assertTrue(watch.closed(2))

    def test_browser_disconnect_uses_short_grace(self):
        watch = BrowserLifetime(9222, 1420)
        with patch.object(watch, '_read_presence', side_effect=[True, None, None, None, None]):
            self.assertFalse(watch.closed(0))
            self.assertFalse(watch.closed(1))
            self.assertFalse(watch.closed(2))
            self.assertFalse(watch.closed(3))
            self.assertTrue(watch.closed(4))

    def test_refresh_or_other_open_frontend_cancels_missing_state(self):
        watch = BrowserLifetime(9222, 1420)
        with patch.object(watch, '_read_presence', side_effect=[True, False, True, True]):
            for now in range(4):
                self.assertFalse(watch.closed(now))
        self.assertIsNone(watch.missing_since)

    def test_background_frontend_does_not_need_heartbeat(self):
        watch = BrowserLifetime(9222, 1420)
        with patch.object(watch, '_read_presence', return_value=True):
            self.assertFalse(watch.closed(0))
            self.assertFalse(watch.closed(1000))

    def test_presence_matches_only_own_frontend_port_and_page(self):
        watch = BrowserLifetime(9222, 1420)
        for url, kind, expected in (
            ('http://127.0.0.1:1420/', 'page', True),
            ('http://localhost:1420/index.html', 'page', True),
            ('http://127.0.0.1:1420/ai.html', 'page', True),
            ('http://127.0.0.1:1420/(不要删favicon.ico页)', 'page', True),
            ('http://127.0.0.1:1420/%28%E4%B8%8D%E8%A6%81%E5%88%A0favicon.ico%E9%A1%B5%29', 'page', True),
            ('http://127.0.0.1:1421/', 'page', False),
            ('http://127.0.0.1:1420/api/health', 'page', False),
            ('http://127.0.0.1.evil.example:1420/', 'page', False),
            ('http://127.0.0.1:1420/', 'iframe', False),
        ):
            data = json.dumps([{'type': kind, 'url': url}]).encode()
            with self.subTest(url=url, kind=kind), patch.object(watch._opener, 'open', return_value=io.BytesIO(data)):
                self.assertEqual(watch._read_presence(), expected)

    def test_invalid_debug_response_is_unknown_not_confirmed_absence(self):
        watch = BrowserLifetime(9222, 1420)
        for data in (b'not-json', b'{}'):
            with patch.object(watch._opener, 'open', return_value=io.BytesIO(data)):
                self.assertIsNone(watch._read_presence())
