"""Offline checks for private diagnostics and unchanged booking payloads."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import instant_book as ib
import privacy
import web_app as web
from test_local_optimization import FakeBooker, OfflineTestCase, run_fake_booking


class PrivacyTests(OfflineTestCase):
    def test_certificate_verification_is_enabled_without_an_override(self):
        booker = ib.InstantBooker({"urls": {}, "session": {"headers": {}}})
        self.addCleanup(booker.session.close)
        self.assertTrue(booker.session.verify)

    def test_nested_response_redacts_account_and_credentials_without_mutating_it(self):
        response = {
            "CODE": "ok",
            "DATA": {"bookingId": "booking-42", "uid": "private-account",
                     "items": [{"realName": "private-person", "Cookie": "private-cookie",
                                "Api-Token": "private-token", "result": "success"}]},
        }
        text = privacy.diagnostic_json(response)
        for private in ("private-account", "private-person", "private-cookie", "private-token"):
            self.assertNotIn(private, text)
        self.assertIn("booking-42", text)
        self.assertIn("success", text)
        self.assertEqual(response["DATA"]["uid"], "private-account")

    def test_dry_run_hides_identity_but_keeps_the_real_request_payload(self):
        class DryRunBooker(FakeBooker):
            uid = "private-account"
            name = "private-person"

            def book(self, seat_id, *args, **kwargs):
                return {"payload": {"seatBookers[0]": self.uid, "seats[0]": seat_id}}

        logs = []
        result = run_fake_booking(DryRunBooker(), dry_run_override=True, logger=logs.append)
        self.assertEqual(result["result"]["candidates"][0]["payload"]["seatBookers[0]"], "private-account")
        text = "\n".join(logs)
        self.assertNotIn("private-account", text)
        self.assertNotIn("private-person", text)
        self.assertIn("<redacted>", text)

    def test_booking_success_response_is_redacted_in_logs(self):
        logs = []
        fake = FakeBooker([{"CODE": "ok", "DATA": {
            "result": "success", "bookingId": 123, "userId": "private-account",
            "extra": {"cookies": "private-cookie"},
        }}])
        result = run_fake_booking(fake, logger=logs.append)
        self.assertEqual(result["confirmed_booking"]["id"], "123")
        self.assertNotIn("private-account", "\n".join(logs))
        self.assertNotIn("private-cookie", "\n".join(logs))

    def test_default_config_alias_works_outside_the_project_working_directory(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web, "DEFAULT_CONFIG", Path(directory) / "config.yaml"), \
             patch.object(web, "WEB_AUTH_PASSWORD", "test-password"):
            self.assertEqual(web.web_config_path("config.yaml"), web.DEFAULT_CONFIG)
            with self.assertRaisesRegex(ValueError, "默认配置文件"):
                web.web_config_path(str(Path(directory) / "other.yaml"))
            with patch.object(web, "load_config", return_value={"booking": {"plan": "1:1558:21:8:1"}}):
                self.assertEqual(web.booking_form_from_config("config.yaml")["config_path"], "config.yaml")
            with patch.object(web, "get_current_bookings", return_value=[]):
                self.assertEqual(web.bookings_from_config("config.yaml")["config_path"], "config.yaml")

    def test_html_does_not_embed_the_absolute_project_path(self):
        handler = object.__new__(web.WebHandler)
        handler.path = "/"
        handler.require_auth = lambda: True
        handler.send_bytes = Mock()
        handler.do_GET()
        body = handler.send_bytes.call_args.args[1].decode()
        self.assertNotIn(str(web.DEFAULT_CONFIG), body)
        self.assertIn('fields.configPath.value = "config.yaml";', body)

    def test_job_log_and_status_hide_local_user_paths(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web, "LOG_DIR", Path(directory) / "logs"), \
             patch.object(web, "JOBS", {}):
            job = web.create_job_record()
            message = "missing file: /Users/private-user/cookies.json"
            web.append_job_log(job, message)
            snapshot = web.job_snapshot(job["id"])
            self.assertEqual(snapshot["log_path"], "logs/" + job["log_path"].name)
            self.assertNotIn("private-user", snapshot["logs"][0])
            self.assertNotIn("private-user", job["log_path"].read_text())

    def test_api_error_does_not_expose_the_project_directory(self):
        handler = object.__new__(web.WebHandler)
        handler.send_json = Mock()
        def fail():
            raise FileNotFoundError(str(web.DEFAULT_CONFIG))
        handler.handle_json(fail)
        error = handler.send_json.call_args.args[0]["error"]
        self.assertNotIn(str(web.DEFAULT_CONFIG.parent), error)
        self.assertIn("config.yaml", error)


if __name__ == "__main__":
    unittest.main()
