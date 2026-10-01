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

import instant_book as ib


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
            ib.load_config(self.path)
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_yaml_errors_report_location_without_echoing_credentials(self):
        self.path.write_text('auth:\n  cookie: [private-test-cookie\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "第 3 行") as caught:
            ib.load_config(self.path)
        self.assertNotIn("private-test-cookie", str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_non_mapping_configuration_and_sections_are_rejected(self):
        for text in ("", "- item\n", "auth: null\n", "session: []\n", "booking: false\n"):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "映射"):
                    ib.load_config(self.path)

    def test_quoted_boolean_cannot_enable_an_unintended_action(self):
        for section, key in (("booking", "dry_run"), ("session", "verify"), ("session", "trust_env")):
            config = copy.deepcopy(self.config)
            config[section][key] = "false"
            with self.subTest(section=section, key=key):
                with self.assertRaisesRegex(ValueError, "不要加引号"):
                    ib.load_config(self.save(config))

    def test_cookie_fields_require_strings_without_echoing_the_value(self):
        self.config["auth"]["cookie"] = {"private-test-cookie": "value"}
        with self.assertRaisesRegex(ValueError, "auth.cookie 必须是字符串") as caught:
            ib.load_config(self.save())
        self.assertNotIn("private-test-cookie", str(caught.exception))

    def test_check_config_accepts_example_with_fake_local_cookies_without_http(self):
        ib.check_config(self.save())
        self.request.assert_not_called()
        self.assertTrue(self.config["booking"]["dry_run"])
        self.assertTrue(self.config["session"]["verify"])
        self.assertEqual(self.config["user_info"], {"uid": "", "name": ""})

    def test_check_config_accepts_inline_cookies_without_a_file(self):
        self.config["auth"] = {"cookie": "offline-test-cookie=offline-test-value", "cookie_file": ""}
        ib.check_config(self.save())
        self.request.assert_not_called()

    def test_check_config_names_missing_api_field(self):
        del self.config["urls"]["query_seats"]
        with self.assertRaisesRegex(ValueError, "urls.query_seats"):
            ib.check_config(self.save())
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
                    ib.check_config(self.save(config))
        self.request.assert_not_called()

    def test_check_config_explains_missing_or_malformed_cookie_file(self):
        self.cookie_path.unlink()
        with self.assertRaisesRegex(RuntimeError, "Cookie 文件不存在"):
            ib.check_config(self.save())
        self.cookie_path.write_text('{"cookies": [{"value": "private-test-value"', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "UTF-8 JSON") as caught:
            ib.check_config(self.save())
        self.assertNotIn("private-test-value", str(caught.exception))
        self.request.assert_not_called()

    def test_check_config_command_does_not_dispatch_any_booking(self):
        self.save()
        output = io.StringIO()
        with patch("sys.argv", ["instant_book.py", "--config", str(self.path), "--check-config"]), \
             patch.object(ib, "run_booking", side_effect=AssertionError("Unexpected booking")), \
             redirect_stdout(output):
            ib.main()
        self.assertIn("尚未验证登录有效性", output.getvalue())
        self.assertNotIn("offline-test-value", output.getvalue())
        self.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
