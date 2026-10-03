"""HTTP routes, request guards, static assets and local server startup."""

import argparse
import base64
import hmac
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from libcs import configuration, constants, errors, operations, privacy, records
from libcs.web import assets, forms, jobs, queries, security, settings


class WebHandler(BaseHTTPRequestHandler):
    server_version = "HDULibraryInstant/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def do_GET(self):
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = assets.INDEX_HTML.replace("__DEFAULT_CONFIG__", settings.DEFAULT_CONFIG.name).replace("__CSRF_TOKEN__", settings.WEB_CSRF_TOKEN)
            self.send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path in assets.STATIC_ASSETS:
            content_type, body = assets.STATIC_ASSETS[parsed.path]
            self.send_bytes(200, body, content_type)
            return
        if parsed.path == "/api/config":
            query = parse_qs(parsed.query)
            path = query.get("path", [str(settings.DEFAULT_CONFIG)])[0] or str(settings.DEFAULT_CONFIG)
            self.handle_json(lambda: forms.booking_form_from_config(path))
            return
        if parsed.path == "/api/job":
            query = parse_qs(parsed.query)
            job_id = query.get("id", [""])[0]
            self.handle_json(lambda: jobs.job_snapshot(job_id))
            return
        if parsed.path == "/api/active-job":
            self.handle_json(jobs.active_job_snapshot)
            return
        if parsed.path == "/api/bookings":
            query = parse_qs(parsed.query)
            path = query.get("path", [str(settings.DEFAULT_CONFIG)])[0] or str(settings.DEFAULT_CONFIG)
            self.handle_json(lambda: queries.bookings_from_config(path))
            return
        if parsed.path == "/api/seat-map":
            query = parse_qs(parsed.query)
            value = lambda key, default="": query.get(key, [default])[0]
            self.handle_json(lambda: queries.seat_map_from_config(
                value("path", str(settings.DEFAULT_CONFIG)), value("room_type"), value("floor_id"),
                value("days"), value("start_hour"), value("duration_hours"),
            ))
            return
        if parsed.path == "/api/clock-offset":
            query = parse_qs(parsed.query)
            path = query.get("path", [str(settings.DEFAULT_CONFIG)])[0] or str(settings.DEFAULT_CONFIG)
            self.handle_json(lambda: queries.clock_offset_from_config(path))
            return
        self.send_error(404)

    def do_POST(self):
        if not self.require_auth():
            return
        try:
            security.validate_mutation(self.headers, settings.WEB_CSRF_TOKEN, bool(settings.WEB_AUTH_PASSWORD))
        except security.RequestRejected as exc:
            self.close_connection = True
            self.send_json({"error": str(exc), "code": "request_rejected"}, status=exc.status)
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            self.handle_json(self.login)
            return
        if parsed.path == "/api/save":
            self.handle_json(self.save_plan)
            return
        if parsed.path == "/api/run":
            self.handle_json(self.run_booking)
            return
        if parsed.path == "/api/run-now":
            self.handle_json(self.run_booking_now)
            return
        if parsed.path == "/api/auto-check-in":
            self.handle_json(self.auto_check_in)
            return
        if parsed.path == "/api/cancel":
            self.handle_json(self.cancel_booking)
            return
        if parsed.path == "/api/cancel-booking":
            self.handle_json(self.cancel_seat_booking)
            return
        if parsed.path == "/api/check-in-test":
            self.handle_json(self.check_in_test)
            return
        if parsed.path == "/api/continue-seat":
            self.handle_json(self.continue_seat)
            return
        self.send_error(404)

    def login(self):
        # The browser opens on the server computer; only its local user can start it.
        if not security.is_loopback(self.client_address[0]):
            raise security.RequestRejected("请在运行工具的电脑上获取 Cookie", 403)
        payload = self.read_json()
        return {"job_id": jobs.start_login_job(payload)}

    def save_plan(self):
        payload = self.read_json()
        path = forms.config_path_from_payload(payload)
        with forms.CONFIG_LOCK:
            configuration.load_config(path)
            plan_text = forms.plan_from_payload(payload)
            forms.write_booking_values(
                path,
                plan_text,
                payload.get("fallback_seats"),
                payload.get("days", 1),
                payload["dry_run"],
                payload.get("execute_at"),
                payload.get("max_trials", constants.DEFAULT_MAX_TRIALS),
                payload.get("retry_delay", constants.DEFAULT_RETRY_DELAY),
                payload.get("hold_before_minutes", constants.DEFAULT_HOLD_BEFORE_MINUTES),
            )
        return {"message": "计划已保存"}

    def run_booking(self):
        payload = self.read_json()
        job_id = jobs.start_booking_job(payload)
        mode = "immediate" if not forms.booking_execute_at_from_payload(payload) else "scheduled"
        return {"job_id": job_id, "mode": mode}

    def run_booking_now(self):
        payload = self.read_json()
        job_id = jobs.start_booking_job(payload, force_immediate=True)
        return {"job_id": job_id, "mode": "immediate"}

    def auto_check_in(self):
        payload = self.read_json()
        job_id = jobs.start_auto_check_in_job(payload)
        return {"job_id": job_id}

    def cancel_booking(self):
        payload = self.read_json()
        return jobs.cancel_job(str(payload.get("job_id") or ""))

    def cancel_seat_booking(self):
        payload = self.read_json()
        path = forms.config_path_from_payload(payload)
        result = operations.cancel_booking_by_id(
            config_path=path,
            booking_id=payload.get("booking_id"),
            logger=lambda message: None,
        )
        return {
            "message": "预约已取消",
            "booking_id": result["booking_id"],
            "result": records.safe_check_in_response(result["result"]),
        }

    def check_in_test(self):
        payload = self.read_json()
        path = forms.config_path_from_payload(payload)
        return operations.check_in_test_by_id(
            config_path=path,
            booking_id=payload.get("booking_id"),
            logger=lambda message: None,
        )

    def continue_seat(self):
        payload = self.read_json()
        path = forms.config_path_from_payload(payload)
        result = operations.continue_seat_by_id(
            config_path=path,
            booking_id=payload.get("booking_id"),
            logger=lambda message: None,
        )
        return {"message": "续座成功", **result}

    def read_json(self):
        payload = security.read_json_body(self.headers, self.rfile)
        if urlparse(self.path).path in ("/api/save", "/api/run", "/api/run-now"):
            if not isinstance(payload.get("dry_run"), bool):
                raise security.RequestRejected("请明确选择是否只测试，dry_run 必须是布尔值")
        return payload

    def handle_json(self, callback):
        try:
            data = callback()
        except security.RequestRejected as exc:
            self.close_connection = True
            self.send_json({"error": str(exc), "code": "request_rejected"}, status=exc.status)
        except jobs.JobNotFound as exc:
            self.send_json({"error": privacy.redact_private_text(exc), "code": "job_not_found"}, status=404)
        except errors.ResultUncertain as exc:
            self.send_json({"error": privacy.redact_private_text(exc), "code": "outcome_uncertain"}, status=409)
        except Exception as exc:
            self.send_json({"error": privacy.redact_private_text(exc)}, status=400)
        else:
            self.send_json(data)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        self.wfile.write(body)

    def require_auth(self):
        try:
            security.validate_host(self.headers, bool(settings.WEB_AUTH_PASSWORD), self.server.server_port)
        except security.RequestRejected as exc:
            self.close_connection = True
            self.send_json({"error": str(exc), "code": "request_rejected"}, status=exc.status)
            return False
        if not settings.WEB_AUTH_PASSWORD:
            return True

        authorization = self.headers.get("Authorization", "")
        scheme, _, encoded = authorization.partition(" ")
        supplied_username = ""
        supplied_password = ""
        if scheme.lower() == "basic" and encoded:
            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                supplied_username, supplied_password = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError):
                pass

        username_ok = hmac.compare_digest(
            supplied_username.encode("utf-8"),
            settings.WEB_AUTH_USERNAME.encode("utf-8"),
        )
        password_ok = hmac.compare_digest(
            supplied_password.encode("utf-8"),
            settings.WEB_AUTH_PASSWORD.encode("utf-8"),
        )
        if username_ok and password_ok:
            return True

        body = "需要登录才能访问。".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="HDU Library", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
        return False

    def log_message(self, format_text, *args):
        return


def make_server(host, port):
    if not security.is_loopback(host) and not settings.WEB_AUTH_PASSWORD:
        raise ValueError("非本机监听必须设置 HDU_WEB_PASSWORD；远程访问请使用 HTTPS 通道")
    return ThreadingHTTPServer((host, port), WebHandler)


def parse_args():
    parser = argparse.ArgumentParser(description="HDU 图书馆即时预约网页控制台")
    parser.add_argument("--host", default=settings.HOST, help="监听地址")
    parser.add_argument("--port", type=int, default=settings.PORT, help="起始端口")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    return parser.parse_args()


def main():
    args = parse_args()
    last_error = None
    for port in range(args.port, args.port + 20):
        try:
            server = make_server(args.host, port)
        except OSError as exc:
            last_error = exc
            continue
        url = f"http://{args.host}:{port}"
        print(f"网页控制台已启动：{url}")
        print("按 Ctrl+C 退出")
        if args.open:
            threading.Timer(0.5, webbrowser.open, args=(url,)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已退出")
        finally:
            server.server_close()
        return
    raise RuntimeError(f"没有可用端口：{last_error}")
