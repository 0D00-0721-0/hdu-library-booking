"""Regression checks for launcher paths and the extracted console assets."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from libcs import cli, errors
from libcs.web import assets, server, settings

ROOT = Path(__file__).resolve().parent


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def launch(self, script, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / script), *args],
            cwd=self.directory, capture_output=True, text=True, timeout=10,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_both_original_launch_commands_work_outside_project(self):
        for script in ("instant_book.py", "web_app.py"):
            with self.subTest(script=script):
                result = self.launch(script, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_offline_config_check_works_outside_project(self):
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        config["auth"] = {"cookie_file": "", "cookie": "offline-test=fake-value"}
        path = self.directory / "config.yaml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        result = self.launch("instant_book.py", "--config", str(path), "--check-config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("尚未验证登录有效性", result.stdout)
        self.assertNotIn("fake-value", result.stdout)

    def test_missing_config_still_exits_with_one(self):
        result = self.launch("instant_book.py", "--config", "missing.yaml", "--check-config")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("config.example.yaml", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_uncertain_and_interrupted_exit_codes_are_preserved(self):
        for error, code in ((errors.ResultUncertain("pending"), 2), (KeyboardInterrupt(), 130)):
            with self.subTest(code=code), patch.object(cli, "main", side_effect=error):
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    cli.run()
                self.assertEqual(caught.exception.code, code)

    def test_imports_do_not_start_http_or_create_runtime_files(self):
        code = """
import json
import sys
from unittest.mock import patch
import requests
sys.path.insert(0, sys.argv[1])
with patch.object(requests.Session, 'request', side_effect=AssertionError('HTTP on import')), \
     patch('http.server.ThreadingHTTPServer', side_effect=AssertionError('Server on import')):
    import instant_book
    import web_app
    from libcs import constants
    from libcs.web import assets, jobs, settings
    print(json.dumps([str(constants.DEFAULT_CONFIG), str(settings.DEFAULT_CONFIG),
                      str(jobs.LOG_DIR), len(assets.STATIC_ASSETS)]))
"""
        result = subprocess.run([sys.executable, "-c", code, str(ROOT)], cwd=self.directory,
                                capture_output=True, text=True, timeout=10,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [str(ROOT / "config.yaml"),
                         str(ROOT / "config.yaml"), str(ROOT / "logs"), 2])
        self.assertEqual(list(self.directory.iterdir()), [])


class ConsoleAssetTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(settings, "WEB_AUTH_PASSWORD", ""))
        self.enterContext(patch.object(settings, "WEB_CSRF_TOKEN", "asset-test-token"))

    def handler(self, path):
        handler = object.__new__(server.WebHandler)
        handler.path = path
        handler.headers = {"Host": "127.0.0.1:8765"}
        handler.server = SimpleNamespace(server_port=8765)
        handler.send_bytes = Mock()
        handler.send_json = Mock()
        handler.send_error = Mock()
        return handler

    def test_separate_script_and_stylesheet_have_correct_content_types(self):
        for filename, content_type in (("console.css", "text/css; charset=utf-8"),
                                       ("console.js", "text/javascript; charset=utf-8")):
            with self.subTest(filename=filename):
                handler = self.handler("/static/" + filename)
                handler.do_GET()
                handler.send_bytes.assert_called_once_with(
                    200, (assets.ASSET_DIR / filename).read_bytes(), content_type)

    def test_html_supplies_page_settings_to_external_script(self):
        handler = self.handler("/")
        handler.do_GET()
        html = handler.send_bytes.call_args.args[1].decode()
        self.assertIn('src="/static/console.js" defer', html)
        self.assertIn('href="/static/console.css"', html)
        self.assertIn('name="csrf-token" content="asset-test-token"', html)
        self.assertIn('name="default-config" content="config.yaml"', html)
        self.assertNotIn("__CSRF_TOKEN__", html)
        self.assertNotIn("__DEFAULT_CONFIG__", html)

    def test_asset_route_cannot_serve_config_or_traverse_directories(self):
        for path in ("/config.yaml", "/static/config.yaml", "/static/../settings.py",
                     "/static/%2e%2e/settings.py", "/static/missing.js"):
            with self.subTest(path=path):
                handler = self.handler(path)
                handler.do_GET()
                handler.send_error.assert_called_once_with(404)
                handler.send_bytes.assert_not_called()

    def test_assets_keep_host_and_password_checks(self):
        handler = self.handler("/static/console.js")
        handler.headers["Host"] = "outside.example:8765"
        handler.do_GET()
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 403)
        handler.send_bytes.assert_not_called()
        with patch.object(settings, "WEB_AUTH_PASSWORD", "test-password"):
            handler = self.handler("/static/console.css")
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            handler.wfile = io.BytesIO()
            handler.do_GET()
            handler.send_response.assert_called_once_with(401)
            handler.send_bytes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
