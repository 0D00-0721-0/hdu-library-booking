"""First-run setup checks use fake local cookies and block all HTTP requests."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import requests
import yaml

from libcs import booking, cli, client, configuration, operations
from libcs.web import jobs as web_jobs


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "config.yaml"
        self.cookie_path = self.root / "session.json"
        self.cookie_path.write_text(json.dumps({"cookies": [{
            "name": "offline-test-cookie", "value": "offline-test-value",
            "domain": "hdu.huitu.zhishulib.com", "path": "/",
        }]}), encoding="utf-8")
        self.config = yaml.safe_load(
            Path(__file__).with_name("config.example.yaml").read_text(encoding="utf-8")
        )
        self.config["auth"]["cookie_file"] = str(self.cookie_path)
        network = patch.object(requests.Session, "request", side_effect=AssertionError("Unexpected HTTP"))
        self.request = network.start()
        self.addCleanup(network.stop)

    def save(self, config=None):
        self.path.write_text(
            yaml.safe_dump(self.config if config is None else config, allow_unicode=True),
            encoding="utf-8",
        )
        return self.path

    def test_missing_config_explains_how_to_start_without_exposing_path(self):
        with self.assertRaisesRegex(ValueError, "config.example.yaml") as caught:
            configuration.load_config(self.path)
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_yaml_errors_report_location_without_echoing_credentials(self):
        self.path.write_text('auth:\n  cookie: [private-test-cookie\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "第 3 行") as caught:
            configuration.load_config(self.path)
        self.assertNotIn("private-test-cookie", str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_non_mapping_configuration_and_sections_are_rejected(self):
        for text in ("", "- item\n", "auth: null\n", "session: []\n", "booking: false\n"):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "映射"):
                    configuration.load_config(self.path)

    def test_quoted_boolean_cannot_enable_an_unintended_action(self):
        for section, key in (("booking", "dry_run"), ("session", "verify"), ("session", "trust_env")):
            config = copy.deepcopy(self.config)
            config[section][key] = "false"
            with self.subTest(section=section, key=key):
                with self.assertRaisesRegex(ValueError, "不要加引号"):
                    configuration.load_config(self.save(config))

    def test_cookie_fields_require_strings_without_echoing_the_value(self):
        self.config["auth"]["cookie"] = {"private-test-cookie": "value"}
        with self.assertRaisesRegex(ValueError, "auth.cookie 必须是字符串") as caught:
            configuration.load_config(self.save())
        self.assertNotIn("private-test-cookie", str(caught.exception))

    def test_check_config_accepts_example_with_fake_local_cookies_without_http(self):
        operations.check_config(self.save())
        self.request.assert_not_called()
        self.assertTrue(self.config["booking"]["dry_run"])
        self.assertTrue(self.config["session"]["verify"])
        self.assertEqual(self.config["user_info"], {"uid": "", "name": ""})

    def test_check_config_accepts_inline_cookies_without_a_file(self):
        self.config["auth"] = {"cookie": "offline-test-cookie=offline-test-value", "cookie_file": ""}
        operations.check_config(self.save())
        self.request.assert_not_called()

    def test_check_config_names_missing_api_field(self):
        del self.config["urls"]["query_seats"]
        with self.assertRaisesRegex(ValueError, "urls.query_seats"):
            operations.check_config(self.save())
        self.request.assert_not_called()

    def test_check_config_rejects_invalid_plan_and_day_offset_before_http(self):
        for key, value, message in (
            ("plan", "1:1558:21:23:2", "booking.plan"),
            ("plan", "1:1558:21:8:0", "booking.plan"),
            ("book_days", 3, "booking.book_days"),
        ):
            config = copy.deepcopy(self.config)
            config["booking"][key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(ValueError, message):
                    operations.check_config(self.save(config))
        self.request.assert_not_called()

    def test_check_config_explains_missing_or_malformed_cookie_file(self):
        self.cookie_path.unlink()
        with self.assertRaisesRegex(RuntimeError, "Cookie 文件不存在"):
            operations.check_config(self.save())
        self.cookie_path.write_text('{"cookies": [{"value": "private-test-value"', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "UTF-8 JSON") as caught:
            operations.check_config(self.save())
        self.assertNotIn("private-test-value", str(caught.exception))
        self.request.assert_not_called()

    def test_check_config_command_does_not_dispatch_any_booking(self):
        self.save()
        output = io.StringIO()
        with patch("sys.argv", ["instant_book.py", "--config", str(self.path), "--check-config"]), \
             patch.object(booking, "run_booking", side_effect=AssertionError("Unexpected booking")), \
             redirect_stdout(output):
            cli.main()
        self.assertIn("尚未验证登录有效性", output.getvalue())
        self.assertNotIn("offline-test-value", output.getvalue())
        self.request.assert_not_called()


    def test_invalid_options_fail_both_offline_and_execution_before_http(self):
        for section, key, value in (
            ('booking', 'max_trials', 'invalid'), ('booking', 'max_trials', 0),
            ('booking', 'max_trials', 21), ('booking', 'max_trials', 1.5),
            ('booking', 'max_trials', True), ('booking', 'retry_delay', 'nan'),
            ('booking', 'retry_delay', float('inf')), ('booking', 'hold_before_minutes', 15),
            ('booking', 'hold_before_minutes', -1), ('booking', 'book_days', True),
            ('booking', 'execute_at', 2000), ('request', 'timeout', 0),
            ('request', 'timeout', -1), ('request', 'timeout', 'infinity'),
            ('request', 'keepalive_interval', -1), ('request', 'keepalive_interval', 'NaN'),
        ):
            config = copy.deepcopy(self.config)
            config[section][key] = value
            with self.subTest(section=section, key=key, value=value):
                path = self.save(config)
                for action in (operations.check_config, booking.run_booking):
                    with self.assertRaisesRegex(ValueError, section + r'\.' + key):
                        action(path)
        self.request.assert_not_called()

    def test_hold_requires_schedule_in_both_entry_points(self):
        self.config['booking'].update(hold_before_minutes=1, execute_at='')
        path = self.save()
        for action in (operations.check_config, booking.run_booking):
            with self.assertRaisesRegex(ValueError, 'booking.execute_at'):
                action(path)
        self.request.assert_not_called()

    def test_cli_overrides_are_validated_before_creating_a_client(self):
        path = self.save()
        with patch.object(client, 'InstantBooker') as client_mock:
            for overrides in ({'max_trials': 21}, {'hold_before_minutes': 15},
                              {'days': True}, {'dry_run_override': 'false'}, {'retry_delay': float('nan')}):
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    booking.run_booking(path, **overrides)
            client_mock.assert_not_called()

    def test_valid_numeric_strings_and_fractional_timeout_are_supported(self):
        self.config['request'].update(timeout=0.5, keepalive_interval=0)
        self.config['booking'].update(max_trials='2', book_days='1', retry_delay=0.1)
        path = self.save()
        operations.check_config(path)
        config = configuration.load_config(path)
        self.assertEqual(config['request']['timeout'], 0.5)
        self.assertEqual(config['booking']['max_trials'], 2)
        self.assertEqual(config['booking']['retry_delay'], 3)
        self.request.assert_not_called()

    def test_web_uses_same_validation_before_creating_a_job(self):

        payload = dict(room_type='1', floor_id='1558', seat_num='21', start_hour='8',
                       duration_hours='1', days=2, dry_run=True, execute_at='20:00:00')
        with patch.object(configuration, 'load_config', return_value=self.config), patch.object(web_jobs, 'create_job_record') as create:
            for key, value in (('max_trials', 'invalid'), ('hold_before_minutes', 15),
                               ('days', 3), ('dry_run', 'false'), ('duration_hours', '25')):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    web_jobs.start_booking_job({**payload, key: value})
            create.assert_not_called()
        self.request.assert_not_called()

if __name__ == "__main__":
    unittest.main()
