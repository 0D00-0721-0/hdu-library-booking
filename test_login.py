"""Offline browser-login checks; no real credentials or upstream requests."""

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import yaml

from libcs import booking, cli, configuration, errors, login
from libcs.web import jobs

COOKIE = {"name": "offline-session", "value": "fake-private-value", "domain": login.LOGIN_HOST,
          "path": "/", "expires": -1, "httpOnly": True, "secure": True}


class LoginTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.yaml"
        self.text = Path(__file__).with_name("config.example.yaml").read_text(encoding="utf-8")
        self.path.write_text(self.text, encoding="utf-8")
        self.config = configuration.load_config(self.path)
        self.enterContext(patch.object(requests.Session, "request", side_effect=AssertionError("Unexpected HTTP")))

    def test_cookie_filter_rejects_unrelated_expired_and_partitioned_values(self):
        candidates = [COOKIE, {**COOKIE, "domain": "login.hdu.edu.cn"},
                      {**COOKIE, "domain": "evil" + login.LOGIN_HOST},
                      {**COOKIE, "domain": login.LOGIN_HOST + ".evil.test"},
                      {**COOKIE, "expires": 1}, {**COOKIE, "partitionKey": "https://other.test"}]
        self.assertEqual(login.library_cookies(candidates), [COOKIE])

    def test_parent_domain_cookie_is_scoped_to_library_host(self):
        self.assertEqual(login.library_cookies([{**COOKIE, "domain": ".zhishulib.com"}]), [COOKIE])
        self.assertEqual(login.library_cookies([{**COOKIE, "domain": "zhishulib.com"}]), [])

    def test_capture_cannot_send_credentials_to_custom_endpoints(self):
        for url in ("https://outside.test/seats", "http://" + login.LOGIN_HOST + "/Seat/Index/searchSeats",
                    login.LOGIN_URL + "Seat/Index/cancelBooking", login.LOGIN_URL + "Seat/Index/searchSeats?x=1"):
            config = copy.deepcopy(self.config)
            config["urls"]["query_seats"] = url
            with self.subTest(url=url), patch.object(login, "playwright_factory") as browser:
                with self.assertRaises(ValueError):
                    login.capture_cookies(config)
                browser.assert_not_called()

    def test_success_atomically_switches_private_config_and_preserves_plan(self):
        original = yaml.safe_load(self.text)
        result = login.save_login(self.path, [COOKIE])
        saved = yaml.safe_load(self.path.read_text())
        target = Path(saved["auth"]["cookie_file"])
        self.assertEqual(saved["booking"], original["booking"])
        self.assertEqual(saved["urls"], original["urls"])
        self.assertEqual(saved["auth"]["cookie"], "")
        self.assertEqual(saved["user_info"], {"uid": "", "name": ""})
        self.assertEqual(json.loads(target.read_text()), {"cookies": [COOKIE]})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn("dry_run: true  #", self.path.read_text())
        self.assertNotIn(COOKIE["value"], str(result))
        self.assertTrue(target.is_absolute())

    def test_inline_auth_and_stale_user_are_replaced(self):
        text = 'auth: {cookie: old, cookie_file: ""}\nuser_info: {uid: "99", name: old}\nbooking: {plan: "1:1558:21:8:1", dry_run: true}\n'
        changed = yaml.safe_load(login.updated_config(text, Path("/tmp/new.json")))
        self.assertEqual(changed["auth"], {"cookie": "", "cookie_file": "/tmp/new.json"})
        self.assertEqual(changed["user_info"], {"uid": "", "name": ""})
        self.assertEqual(changed["booking"], yaml.safe_load(text)["booking"])

    def test_duplicate_auth_and_root_flow_mapping_are_rejected(self):
        for text in ('auth: {}\nauth: {}\n', '{auth: {}, user_info: {}}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                login.updated_config(text, Path("/tmp/new.json"))

    def test_failed_config_write_removes_new_cookie_and_keeps_previous_files(self):
        old = self.path.parent / "old.json"
        old.write_text("old-cookie")
        real_write = login.private_write
        def failing_write(path, text):
            if path == self.path.resolve():
                raise OSError("write failed")
            real_write(path, text)
        with patch.object(login, "private_write", side_effect=failing_write):
            with self.assertRaises(OSError):
                login.save_login(self.path, [COOKIE])
        self.assertEqual(self.path.read_text(), self.text)
        self.assertEqual(old.read_text(), "old-cookie")
        self.assertEqual(list((self.path.parent / "cookies").iterdir()), [])

    def test_cancel_between_cookie_write_and_config_switch_keeps_old_config(self):
        with self.assertRaises(errors.TaskCancelled):
            login.save_login(self.path, [COOKIE], should_cancel=Mock(side_effect=[False, True]))
        self.assertEqual(self.path.read_text(), self.text)
        self.assertEqual(list((self.path.parent / "cookies").iterdir()), [])

    def test_save_rereads_latest_plan_after_browser_wait(self):
        def capture(*args, **kwargs):
            self.path.write_text(self.text.replace('max_trials: 10', 'max_trials: 7'))
            return [COOKIE]
        with patch.object(login, "capture_cookies", side_effect=capture):
            login.login(self.path, logger=lambda _: None)
        self.assertEqual(yaml.safe_load(self.path.read_text())["booking"]["max_trials"], 7)

    def test_probe_clears_stale_uid_enables_tls_and_is_read_only(self):
        config = copy.deepcopy(self.config)
        config["user_info"] = {"uid": "999", "name": "old"}
        config["session"]["verify"] = False
        observed = []
        def keepalive(booker, timeout=None):
            observed.append((booker.uid, booker.session.verify, timeout))
            booker.uid = "123"
        with patch.object(login.client.InstantBooker, "keepalive", keepalive):
            saved = login.verify_cookies(config, [COOKIE])
        self.assertEqual(observed, [("", True, 5)])
        self.assertEqual(saved[0]["value"], COOKIE["value"])
        self.assertEqual(config["user_info"]["uid"], "999")

    def test_anonymous_cookie_cannot_be_saved_as_a_valid_login(self):
        with patch.object(login.client.InstantBooker, "keepalive", side_effect=RuntimeError("not logged in")):
            self.assertIsNone(login.verify_cookies(self.config, [COOKIE]))
        with patch.object(login.client.InstantBooker, "keepalive", return_value=None):
            self.assertIsNone(login.verify_cookies(self.config, [COOKIE]))

    def browser(self):
        context = Mock()
        page = Mock()
        context.pages = [page]
        context.new_page.return_value = page
        context.cookies.return_value = [COOKIE]
        browser = Mock()
        browser.new_context.return_value = context
        browser.is_connected.return_value = True
        self.enterContext(patch.object(login, "playwright_factory", return_value=MockContext()))
        self.enterContext(patch.object(login, "launch_browser", return_value=browser))
        return browser, context

    def test_capture_waits_for_verified_login_then_closes_browser(self):
        browser, context = self.browser()
        with patch.object(login, "verify_cookies", return_value=[COOKIE]) as verify:
            result = login.capture_cookies(self.config, logger=lambda _: None)
        self.assertEqual(result, [COOKIE])
        verify.assert_called_once()
        browser.close.assert_called_once()
        self.assertEqual(context.cookies.call_args.args[0][0], login.LOGIN_URL)

    def test_closed_window_and_timeout_leave_previous_state_untouched(self):
        browser, context = self.browser()
        context.pages = []
        with self.assertRaises(errors.TaskCancelled):
            login.capture_cookies(self.config, logger=lambda _: None)
        browser.close.assert_called_once()
        context.pages = [Mock()]
        with self.assertRaisesRegex(RuntimeError, "超时"):
            login.capture_cookies(self.config, logger=lambda _: None, timeout=0)
        self.assertEqual(self.path.read_text(), self.text)

    def test_cancellation_after_verification_prevents_saving(self):
        self.browser()
        with patch.object(login, "verify_cookies", return_value=[COOKIE]):
            with self.assertRaises(errors.TaskCancelled):
                login.capture_cookies(self.config, logger=lambda _: None,
                                      should_cancel=Mock(side_effect=[False, False, True]))
        self.assertEqual(self.path.read_text(), self.text)

    def test_browser_exception_does_not_expose_tokens(self):
        _, context = self.browser()
        context.cookies.side_effect = Exception("secret-token-in-redirect")
        with self.assertRaises(RuntimeError) as caught:
            login.capture_cookies(self.config, logger=lambda _: None)
        self.assertNotIn("secret-token", str(caught.exception))

    def test_missing_dependency_has_actionable_error(self):
        with patch.dict("sys.modules", {"playwright.sync_api": None}):
            with self.assertRaisesRegex(RuntimeError, "requirements-login.txt"):
                login.playwright_factory()

    def test_missing_browser_has_actionable_error_without_raw_diagnostics(self):
        playwright = Mock()
        playwright.chromium.launch.side_effect = Exception("private-path")
        with self.assertRaisesRegex(RuntimeError, "install chromium") as caught:
            login.launch_browser(playwright)
        self.assertNotIn("private-path", str(caught.exception))
        self.assertEqual(playwright.chromium.launch.call_count, 2)

    def test_cli_login_does_not_dispatch_booking(self):
        with patch("sys.argv", ["instant_book.py", "--login", "--config", str(self.path)]), \
             patch.object(login, "login") as capture, patch.object(booking, "run_booking") as book:
            cli.main()
        capture.assert_called_once_with(str(self.path))
        book.assert_not_called()

    def test_login_job_uses_existing_task_exclusion_and_only_returns_job_id(self):
        with patch.object(jobs, "create_job_record", side_effect=RuntimeError("已有任务")), \
             patch.object(login, "login") as capture:
            with self.assertRaisesRegex(RuntimeError, "已有任务"):
                jobs.start_login_job({"config_path": str(self.path)})
            capture.assert_not_called()

    def test_login_job_completes_without_leaking_cookie_in_snapshot(self):
        self.enterContext(patch.object(jobs, "LOG_DIR", self.path.parent / "logs"))
        self.enterContext(patch.object(jobs, "JOBS", {}))
        def run_now(*, target, **kwargs):
            return SimpleNamespace(start=target)
        with patch.object(threading, "Thread", side_effect=run_now), \
             patch.object(login, "capture_cookies", return_value=[COOKIE]):
            job_id = jobs.start_login_job({"config_path": str(self.path)})
        snapshot = jobs.job_snapshot(job_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertIn("Cookie 已保存", snapshot["message"])
        self.assertNotIn(COOKIE["value"], json.dumps(snapshot))


class MockContext:
    def __enter__(self):
        return Mock()

    def __exit__(self, *args):
        return False


if __name__ == "__main__":
    unittest.main()
