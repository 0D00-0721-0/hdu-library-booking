"""Optional real-browser integration; all library traffic is intercepted locally."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import yaml

from libcs import login, operations


@unittest.skipUnless(os.environ.get("LIBCS_BROWSER_TEST") == "1", "optional real-browser check")
class BrowserLoginIntegration(unittest.TestCase):
    def test_real_browser_cookie_capture_and_private_config_round_trip(self):
        real_launch = login.launch_browser
        browser_requests = []
        api_requests = []

        def launch(playwright):
            browser = (playwright.chromium.launch(headless=True)
                       if os.environ.get("LIBCS_BROWSER_HEADLESS") == "1"
                       else real_launch(playwright))
            class Browser:
                def new_context(self):
                    context = browser.new_context()
                    def route_request(route):
                        browser_requests.append(route.request.url)
                        if route.request.url == login.LOGIN_URL:
                            route.fulfill(status=200, content_type="text/html", headers={
                                "Set-Cookie": "offline-session=fake-browser-value; Path=/; Secure; HttpOnly",
                            }, body="<title>Offline login fixture</title><p>Simulated login only</p>")
                        else:
                            route.abort()
                    context.route("**/*", route_request)
                    return context
                def is_connected(self):
                    return browser.is_connected()
                def close(self):
                    browser.close()
            return Browser()

        def request(session, method, url, **kwargs):
            api_requests.append((method, url))
            self.assertEqual(method, "GET")
            self.assertEqual(session.cookies.get("offline-session"), "fake-browser-value")
            if url == login.LOGIN_URL + "Space/Category/list":
                data = {"content": {"children": [{}, {"defaultItems": [{
                    "name": "offline room", "link": {"url": "/Seat/Index/searchSeats?category=1"},
                }]}]}}
            elif url == login.LOGIN_URL + "Seat/Index/searchSeats?category=1":
                data = {"data": {"is_login": True, "uid": "123"}}
            else:
                self.fail("Unexpected endpoint")
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(data).encode()
            return response

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(Path(__file__).with_name("config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            messages = []
            with patch.object(login, "launch_browser", side_effect=launch), \
                 patch.object(requests.Session, "request", request):
                result = login.login(path, logger=messages.append)
                operations.check_config(path)
            self.assertTrue(result["ok"])
            saved = yaml.safe_load(path.read_text())
            cookie_path = Path(saved["auth"]["cookie_file"])
            cookies = json.loads(cookie_path.read_text())["cookies"]
            self.assertEqual(cookies[0]["value"], "fake-browser-value")
            self.assertTrue(cookies[0]["secure"])
            self.assertNotIn("fake-browser-value", "".join(messages))
            self.assertIn(login.LOGIN_URL, browser_requests)
            self.assertEqual(len(api_requests), 2)


if __name__ == "__main__":
    unittest.main()
