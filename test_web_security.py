"""Offline request-boundary checks. No real job or upstream request can run."""

import base64
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from libcs.web import jobs as web_jobs
from libcs.web import server as web_server
from libcs.web import settings as web_settings
from libcs.web.security import MAX_BODY_BYTES


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        for target, value in (("WEB_AUTH_PASSWORD", ""), ("WEB_CSRF_TOKEN", "test-token")):
            patcher = patch.object(web_settings, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        network = patch.object(requests.Session, "request", side_effect=AssertionError("Unexpected upstream request"))
        network.start()
        self.addCleanup(network.stop)
        self.start = patch.object(web_jobs, "start_booking_job", return_value="mock-job")
        self.dispatch = self.start.start()
        self.addCleanup(self.start.stop)

    def handler(self, payload=None, headers=None, route="/api/run-now", raw=None):
        body = json.dumps({"dry_run": True} if payload is None else payload).encode() if raw is None else raw
        handler = object.__new__(web_server.WebHandler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = route
        handler.server = SimpleNamespace(server_port=8765)
        handler.headers = {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765",
                           "Content-Type": "application/json", "Content-Length": str(len(body)),
                           "X-CSRF-Token": "test-token", "Sec-Fetch-Site": "same-origin"}
        handler.headers.update(headers or {})
        handler.rfile = io.BytesIO(body)
        handler.send_json = Mock()
        handler.send_bytes = Mock()
        return handler

    def rejected(self, handler, status):
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.kwargs['status'], status)
        self.dispatch.assert_not_called()
        self.assertTrue(handler.close_connection)

    def test_valid_local_request_dispatches_once(self):
        handler = self.handler()
        handler.do_POST()
        self.dispatch.assert_called_once_with({"dry_run": True}, force_immediate=True)
        self.assertEqual(handler.send_json.call_args.args[0]['job_id'], 'mock-job')

    def test_all_mutations_reject_other_origins_before_action(self):
        for route in ('login', 'save', 'run', 'run-now', 'auto-check-in', 'cancel', 'cancel-booking', 'check-in-test', 'continue-seat'):
            with self.subTest(route=route):
                handler = self.handler(headers={"Origin": "https://outside.example"}, route='/api/' + route)
                self.rejected(handler, 403)

    def test_login_route_uses_job_and_does_not_return_credentials(self):
        with patch.object(web_jobs, "start_login_job", return_value="login-job") as login_job:
            handler = self.handler(payload={}, route="/api/login")
            handler.do_POST()
            login_job.assert_called_once_with({})
            self.assertEqual(handler.send_json.call_args.args[0], {"job_id": "login-job"})

    def test_remote_client_cannot_open_server_login_browser(self):
        with patch.object(web_jobs, "start_login_job") as login_job:
            handler = self.handler(payload={}, route="/api/login")
            handler.client_address = ("192.0.2.1", 12345)
            self.rejected(handler, 403)
            login_job.assert_not_called()

    def test_missing_null_malformed_or_different_port_origin_is_rejected(self):
        for origin in ('', 'null', 'http://127.0.0.1:9999', 'http://127.0.0.1:8765/path',
                       'http://user@127.0.0.1:8765', 'https://127.0.0.1:8765'):
            with self.subTest(origin=origin):
                self.rejected(self.handler(headers={'Origin': origin}), 403)

    def test_dns_rebinding_host_and_cross_site_metadata_are_rejected(self):
        for headers in ({'Host': 'outside.example:8765', 'Origin': 'http://outside.example:8765'},
                        {'Sec-Fetch-Site': 'cross-site'}, {'Host': '127.0.0.1:9999'},
                        {'Host': '127.0.0.1:8765, outside.example'}):
            with self.subTest(headers=headers):
                self.rejected(self.handler(headers=headers), 403)

    def test_missing_or_stale_token_is_rejected(self):
        for token in ('', 'token-from-before-restart'):
            self.rejected(self.handler(headers={'X-CSRF-Token': token}), 403)

    def test_simple_content_type_cannot_reach_booking(self):
        for content_type in ('text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data'):
            self.rejected(self.handler(headers={'Content-Type': content_type}), 415)

    def test_large_request_is_rejected_without_reading_body(self):
        handler = self.handler(headers={'Content-Length': str(MAX_BODY_BYTES + 1)})
        handler.rfile = Mock()
        self.rejected(handler, 413)
        handler.rfile.read.assert_not_called()

    def test_bad_length_and_chunked_body_are_rejected(self):
        for length in ('', '-1', 'NaN', '10,10'):
            self.rejected(self.handler(headers={'Content-Length': length}), 411)
        self.rejected(self.handler(headers={'Transfer-Encoding': 'chunked'}), 400)

    def test_bad_json_and_ambiguous_payloads_are_rejected_without_echo(self):
        for body in (b'[]', b'{', b'null', b'{"dry_run":true,"dry_run":false}',
                     b'{"dry_run":true,"value":NaN}', b'\xff', b'{"private-cookie":'):
            with self.subTest(body=body):
                handler = self.handler(raw=body)
                self.rejected(handler, 400)
                self.assertNotIn('private-cookie', str(handler.send_json.call_args))

    def test_types_and_explicit_dry_run_are_checked(self):
        for payload in ({}, {'dry_run': 'false'}, {'dry_run': 0}, {'dry_run': None},
                        {'dry_run': True, 'days': {}}, {'dry_run': True, 'config_path': []},
                        {'dry_run': True, 'execute_at': 2000}):
            with self.subTest(payload=payload):
                self.rejected(self.handler(payload=payload), 400)

    def test_truncated_body_is_rejected(self):
        self.rejected(self.handler(raw=b'{}', headers={'Content-Length': '10'}), 400)

    def test_authenticated_origin_accepts_original_https_host(self):
        credentials = base64.b64encode(b'hdu:test-password').decode()
        with patch.object(web_settings, 'WEB_AUTH_PASSWORD', 'test-password'):
            handler = self.handler(headers={'Host': 'console.example',
                'Origin': 'https://console.example', 'Authorization': 'Basic ' + credentials,
                'X-Forwarded-Host': 'ignored.example'})
            handler.do_POST()
        self.dispatch.assert_called_once()

    def test_forwarded_headers_cannot_bypass_origin_check(self):
        handler = self.handler(headers={'Origin': 'https://outside.example',
            'X-Forwarded-Host': 'outside.example', 'X-Forwarded-Proto': 'https'})
        self.rejected(handler, 403)

    def test_remote_password_is_still_required_with_valid_csrf(self):
        with patch.object(web_settings, 'WEB_AUTH_PASSWORD', 'test-password'):
            handler = self.handler()
            handler.wfile = io.BytesIO()
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            handler.do_POST()
            handler.send_response.assert_called_once_with(401)
        self.dispatch.assert_not_called()

    def test_page_injects_token_and_remains_uncacheable(self):
        handler = self.handler(route='/')
        handler.do_GET()
        html = handler.send_bytes.call_args.args[1].decode()
        self.assertIn('<meta name="csrf-token" content="test-token">', html)
        self.assertNotIn('__CSRF_TOKEN__', html)
        handler = object.__new__(web_server.WebHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        handler.send_bytes(200, b'page', 'text/html')
        handler.send_header.assert_any_call('Cache-Control', 'no-store')

    def test_non_loopback_bind_requires_password_before_opening_socket(self):
        with patch.object(web_server, 'ThreadingHTTPServer') as server:
            for host in ('0.0.0.0', '192.168.1.2', '::'):
                with self.subTest(host=host), self.assertRaisesRegex(ValueError, 'HDU_WEB_PASSWORD'):
                    web_server.make_server(host, 8765)
            server.assert_not_called()
            web_server.make_server('127.0.0.1', 8765)
            server.assert_called_once()
            with patch.object(web_settings, 'WEB_AUTH_PASSWORD', 'test-password'):
                web_server.make_server('0.0.0.0', 8765)


if __name__ == '__main__':
    unittest.main()
