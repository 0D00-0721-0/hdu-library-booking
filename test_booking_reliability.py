import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import requests

import instant_book
import web_app


def minimal_config():
    return {
        "urls": {"book_seat": "https://hdu.huitu.zhishulib.com/Seat/Index/bookSeats"},
        "request": {"timeout": 1, "keepalive_interval": 0},
        "session": {
            "headers": {"Cookie": "stale=header"},
            "params": {"LAB_JSON": "1"},
            "trust_env": False,
            "verify": False,
        },
        "user_info": {"uid": "123", "name": "tester"},
    }


class CookieAndAuthTests(unittest.TestCase):
    def test_inline_cookie_uses_cookie_jar_not_fixed_header(self):
        booker = instant_book.InstantBooker(minimal_config())
        self.assertNotIn("Cookie", booker.session.headers)

        self.assertTrue(booker._load_cookie_header("auth=old; uid=123"))
        booker.session.cookies.set(
            "auth",
            "rotated",
            domain="hdu.huitu.zhishulib.com",
            path="/",
        )
        prepared = booker.session.prepare_request(
            requests.Request("GET", "https://hdu.huitu.zhishulib.com/ping")
        )
        self.assertIn("auth=rotated", prepared.headers.get("Cookie", ""))
        self.assertNotIn("auth=old", prepared.headers.get("Cookie", ""))

    def test_authenticated_detail_checks_uid(self):
        booker = instant_book.InstantBooker(minimal_config())
        self.assertTrue(booker._validate_authenticated_detail({"is_login": True, "uid": 123}))
        with self.assertRaisesRegex(RuntimeError, "登录态已失效"):
            booker._validate_authenticated_detail({"is_login": False, "uid": 123})
        with self.assertRaisesRegex(RuntimeError, "登录用户不匹配"):
            booker._validate_authenticated_detail({"is_login": True, "uid": 999})


class BookingProtocolTests(unittest.TestCase):
    def setUp(self):
        # Exercise the real 3-second policy without slowing tests in wall time.
        self.clock = 100.0
        def sleep(seconds):
            self.clock += seconds
        for patcher in (
            patch.object(instant_book.time, "monotonic", side_effect=lambda: self.clock),
            patch.object(instant_book.time, "sleep", side_effect=sleep),
            patch.object(instant_book, "_BOOKING_GATES", {}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_fallback_seat_parser_deduplicates_and_limits(self):
        self.assertEqual(
            instant_book.parse_fallback_seats("22, 23，21 22", primary_seat="21"),
            ["22", "23"],
        )
        with self.assertRaisesRegex(ValueError, "必须是数字"):
            instant_book.parse_fallback_seats("22,A3", primary_seat="21")
        with self.assertRaisesRegex(ValueError, "最多填写"):
            instant_book.parse_fallback_seats("1,2,3,4,5,6", primary_seat="21")

    def test_seat_unavailable_message_is_classified(self):
        result = {
            "CODE": "ok",
            "DATA": {
                "result": "fail",
                "msg": "选择的座位无法预约，可能座位不可用或已经被其他人锁定或占用，请换一个再试",
            },
        }
        self.assertTrue(instant_book.is_seat_unavailable(result))

    def test_booking_range_validates_duration_and_date(self):
        booker = instant_book.InstantBooker(minimal_config())
        tz = timezone(timedelta(hours=8))
        detail = {
            "range": {
                "minBeginTime": 7,
                "maxEndTime": 22,
                "min_duration": 1,
                "max_duration": 15,
                "advance_date": datetime(2026, 7, 14, tzinfo=tz).timestamp(),
                "max_date": datetime(2026, 7, 16, tzinfo=tz).timestamp(),
            }
        }
        booker.validate_booking_time(
            detail,
            7,
            15,
            begin_time=datetime(2026, 7, 16, 7, tzinfo=tz),
        )
        duration_detail = {"range": {**detail["range"], "maxEndTime": 24}}
        with self.assertRaisesRegex(RuntimeError, "最多 15 小时"):
            booker.validate_booking_time(duration_detail, 7, 16)
        with self.assertRaisesRegex(RuntimeError, "最晚可预约"):
            booker.validate_booking_time(
                detail,
                7,
                15,
                begin_time=datetime(2026, 7, 17, 7, tzinfo=tz),
            )

    def test_success_is_fail_closed(self):
        failures = [
            {},
            {"CODE": "ok"},
            {"CODE": "notLogin", "DATA": {"result": "success", "bookingId": 1}},
            {"CODE": "ok", "DATA": {"result": "pending", "bookingId": 1}},
            {"CODE": "ok", "DATA": {"result": "success"}},
        ]
        for result in failures:
            with self.subTest(result=result):
                self.assertFalse(instant_book.booking_result_succeeded(result))
                self.assertTrue(instant_book.booking_result_failed(result))

        success = {"CODE": "ok", "DATA": {"result": "success", "bookingId": 12345}}
        self.assertTrue(instant_book.booking_result_succeeded(success))
        self.assertFalse(instant_book.booking_result_failed(success))

    def test_nested_error_message_is_used_for_opening_retry(self):
        result = {
            "CODE": "ok",
            "MESSAGE": "请求完成",
            "DATA": {"result": "fail", "msg": "超出可预约座位时间范围"},
        }
        self.assertTrue(instant_book.is_time_out_of_range(result))

    def test_fixed_seat_uses_per_request_token_header(self):
        booker = instant_book.InstantBooker(minimal_config())
        captured = {}

        def fake_request(method, url, data=None, headers=None, timeout=None):
            captured.update(method=method, url=url, data=data, headers=headers)
            return {"CODE": "ok", "DATA": {"result": "success", "bookingId": 1}}

        booker.request = fake_request
        begin = datetime(2026, 7, 15, 7, tzinfo=timezone(timedelta(hours=8)))
        booker.book("62585", begin, 15, is_recommend=0)

        self.assertEqual(captured["data"]["is_recommend"], 0)
        self.assertIn("Api-Token", captured["headers"])
        self.assertNotIn("Api-Token", booker.session.headers)

    def test_continue_seat_requires_temporarily_away_status_and_confirms_active(self):
        class FakeBooker:
            def __init__(self):
                self.calls = 0

            def current_bookings(self):
                self.calls += 1
                status = "2" if self.calls == 1 else "1"
                return [{
                    "id": "123",
                    "room_name": "四楼",
                    "seat_num": "21",
                    "status": status,
                    "status_label": instant_book.BOOKING_STATUS_LABELS[status],
                }]

            def continue_booking(self, booking_id):
                self.booking_id = booking_id
                return {"CODE": "ok", "DATA": {"result": "success"}}

        fake = FakeBooker()
        with patch.object(instant_book, "create_booker", return_value=fake):
            result = instant_book.continue_seat_by_id("unused.yaml", "123", logger=lambda _: None)

        self.assertEqual(fake.booking_id, "123")
        self.assertEqual(result["status_code_before"], "2")
        self.assertEqual(result["status_code_after"], "1")

    def test_continue_seat_rejects_expired_temporary_leave(self):
        class FakeBooker:
            def current_bookings(self):
                return [{
                    "id": "123",
                    "status": "6",
                    "status_label": "暂离未归结束",
                }]

        with patch.object(instant_book, "create_booker", return_value=FakeBooker()):
            with self.assertRaisesRegex(RuntimeError, "续座期限已过"):
                instant_book.continue_seat_by_id("unused.yaml", "123", logger=lambda _: None)

    def test_request_timeout_is_retryable(self):
        booker = instant_book.InstantBooker(minimal_config())
        with patch.object(booker.session, "post", side_effect=requests.Timeout("late")):
            with self.assertRaises(instant_book.RequestFailure) as context:
                booker.request("POST", booker.urls["book_seat"], {})
        self.assertTrue(context.exception.retryable)

    def test_matching_booking_requires_exact_target(self):
        begin = datetime(2026, 7, 15, 7, tzinfo=timezone(timedelta(hours=8)))
        item = {
            "id": "9",
            "seat_num": "21",
            "start_timestamp": int(begin.timestamp()),
            "duration_seconds": 15 * 3600,
            "status": "0",
        }
        self.assertEqual(
            instant_book.find_matching_booking([item], "21", begin, 15),
            item,
        )
        self.assertIsNone(instant_book.find_matching_booking([item], "22", begin, 15))

    def test_run_booking_retries_opening_error_and_confirms_success(self):
        class FakeBooker:
            def __init__(self):
                self.uid = "123"
                self.name = "tester"
                self.keepalive_interval = 0
                self.timeout = 1
                self.last_seat_query_meta = {
                    "is_recommend": 0,
                    "requires_image_code": 0,
                    "server_time": int(time.time()),
                }
                self.book_calls = []

            def load_cookies(self):
                return None

            def resolve_user(self):
                return None

            def query_room_items(self):
                return [{"name": "自习室", "query": "x=1"}]

            def query_room_detail(self, room_item):
                return {"range": {}}

            def validate_booking_time(self, *args, **kwargs):
                return None

            def query_seat_map(self, *args, **kwargs):
                return [{"roomName": "四楼", "seatMap": {"info": {"id": "1558"}}}]

            def find_seat(self, floors, floor_id, seat_num):
                return floors[0], {"id": str(seat_num), "title": str(seat_num), "state": "0"}

            def keepalive(self):
                return "room_detail"

            def book(self, seat_id, begin_time, duration_hours, **kwargs):
                self.book_calls.append((seat_id, begin_time, duration_hours, kwargs))
                if len(self.book_calls) == 1:
                    return {
                        "CODE": "ok",
                        "DATA": {"result": "fail", "msg": "超出可预约座位时间范围"},
                    }
                return {"CODE": "ok", "DATA": {"result": "success", "bookingId": 123}}

            def current_bookings(self):
                seat_id, begin_time, duration_hours, _ = self.book_calls[-1]
                return [
                    {
                        "id": "123",
                        "seat_num": str(seat_id),
                        "start_timestamp": int(begin_time.timestamp()),
                        "duration_seconds": duration_hours * 3600,
                        "status": "0",
                        "label": "confirmed",
                    }
                ]

        fake = FakeBooker()
        config = {"booking": {}, **minimal_config()}
        with (
            patch.object(instant_book, "load_config", return_value=config),
            patch.object(instant_book, "InstantBooker", return_value=fake),
        ):
            result = instant_book.run_booking(
                config_path="unused.yaml",
                plan_text="1:1558:21:7:15",
                fallback_seats="22",
                days=2,
                dry_run_override=False,
                execute_at="",
                max_trials=2,
                retry_delay=0.05,
                logger=lambda message: None,
            )

        self.assertEqual(len(fake.book_calls), 2)
        self.assertEqual([str(call[0]) for call in fake.book_calls], ["21", "21"])
        self.assertEqual(result["confirmed_booking"]["id"], "123")
        self.assertFalse(result["used_fallback"])

    def test_run_booking_reports_unknown_submit_response_as_uncertain(self):
        class UnknownResponseBooker:
            uid = "123"
            name = "tester"
            keepalive_interval = 0
            timeout = 1
            last_seat_query_meta = {
                "is_recommend": 0,
                "requires_image_code": 0,
                "server_time": None,
            }

            def load_cookies(self):
                return None

            def resolve_user(self):
                return None

            def query_room_items(self):
                return [{"name": "自习室", "query": "x=1"}]

            def query_room_detail(self, room_item):
                return {"range": {}}

            def validate_booking_time(self, *args, **kwargs):
                return None

            def query_seat_map(self, *args, **kwargs):
                return [{"roomName": "四楼", "seatMap": {"info": {"id": "1558"}}}]

            def find_seat(self, floors, floor_id, seat_num):
                return floors[0], {"id": "62585", "title": "21", "state": "0"}

            def keepalive(self):
                return "room_detail"

            def book(self, *args, **kwargs):
                return {}

            def current_bookings(self):
                return []

        config = {"booking": {}, **minimal_config()}
        with (
            patch.object(instant_book, "load_config", return_value=config),
            patch.object(instant_book, "InstantBooker", return_value=UnknownResponseBooker()),
        ):
            with self.assertRaisesRegex(instant_book.ResultUncertain, "结果待确认"):
                instant_book.run_booking(
                    config_path="unused.yaml",
                    plan_text="1:1558:21:7:15",
                    days=2,
                    dry_run_override=False,
                    execute_at="",
                    max_trials=1,
                    retry_delay=0.05,
                    logger=lambda message: None,
                )

    def test_run_booking_switches_to_fallback_after_seat_unavailable(self):
        unavailable = {
            "CODE": "ok",
            "DATA": {"result": "fail", "msg": "选择的座位无法预约，座位已被占用"},
        }
        success = {"CODE": "ok", "DATA": {"result": "success", "bookingId": 456}}

        class FallbackBooker:
            uid = "123"
            name = "tester"
            keepalive_interval = 0
            timeout = 1
            last_seat_query_meta = {
                "is_recommend": 0,
                "requires_image_code": 0,
                "server_time": None,
            }

            def __init__(self):
                self.book_calls = []
                self.book_call_times = []

            def load_cookies(self):
                return None

            def resolve_user(self):
                return None

            def query_room_items(self):
                return [{"name": "自习室", "query": "x=1"}]

            def query_room_detail(self, room_item):
                return {"range": {}}

            def validate_booking_time(self, *args, **kwargs):
                return None

            def query_seat_map(self, *args, **kwargs):
                return [{"roomName": "四楼", "seatMap": {"info": {"id": "1558"}}}]

            def find_seat(self, floors, floor_id, seat_num):
                if str(seat_num) == "99":
                    raise RuntimeError("找不到 99 座")
                return floors[0], {"id": str(seat_num), "title": str(seat_num), "state": "0"}

            def keepalive(self):
                return "room_detail"

            def book(self, seat_id, begin_time, duration_hours, **kwargs):
                self.book_calls.append((str(seat_id), begin_time, duration_hours))
                self.book_call_times.append(time.monotonic())
                return unavailable if str(seat_id) == "21" else success

            def current_bookings(self):
                seat_id, begin_time, duration_hours = self.book_calls[-1]
                return [
                    {
                        "id": "456",
                        "seat_num": seat_id,
                        "start_timestamp": int(begin_time.timestamp()),
                        "duration_seconds": duration_hours * 3600,
                        "status": "0",
                        "label": f"{seat_id}座 confirmed",
                    }
                ]

        fake = FallbackBooker()
        config = {"booking": {}, **minimal_config()}
        logs = []
        with (
            patch.object(instant_book, "load_config", return_value=config),
            patch.object(instant_book, "InstantBooker", return_value=fake),
        ):
            result = instant_book.run_booking(
                config_path="unused.yaml",
                plan_text="1:1558:21:7:15",
                fallback_seats="99,22,23",
                days=2,
                dry_run_override=False,
                execute_at="",
                max_trials=1,
                retry_delay=0.05,
                logger=logs.append,
            )

        self.assertEqual([call[0] for call in fake.book_calls], ["21", "22"])
        self.assertEqual(result["booked_seat_num"], "22")
        self.assertTrue(result["used_fallback"])
        self.assertTrue(any("跳过无效备选座位 99" in line for line in logs))
        self.assertTrue(any("3 秒后切换" in line for line in logs))
        self.assertGreaterEqual(fake.book_call_times[1] - fake.book_call_times[0], 3.0)

    def test_run_booking_switches_to_fallback_after_opening_error(self):
        not_open = {
            "CODE": "ParamError",
            "DATA": {"result": "fail", "msg": "超出可预约座位时间范围"},
        }
        success = {"CODE": "ok", "DATA": {"result": "success", "bookingId": 789}}

        class OpeningFallbackBooker:
            uid = "123"
            name = "tester"
            keepalive_interval = 0
            timeout = 1
            last_seat_query_meta = {
                "is_recommend": 0,
                "requires_image_code": 0,
                "server_time": None,
            }

            def __init__(self):
                self.book_calls = []
                self.book_call_times = []

            def load_cookies(self):
                return None

            def resolve_user(self):
                return None

            def query_room_items(self):
                return [{"name": "自习室", "query": "x=1"}]

            def query_room_detail(self, room_item):
                return {"range": {}}

            def validate_booking_time(self, *args, **kwargs):
                return None

            def query_seat_map(self, *args, **kwargs):
                return [{"roomName": "四楼", "seatMap": {"info": {"id": "1558"}}}]

            def find_seat(self, floors, floor_id, seat_num):
                return floors[0], {"id": str(seat_num), "title": str(seat_num), "state": "0"}

            def keepalive(self):
                return "room_detail"

            def book(self, seat_id, begin_time, duration_hours, **kwargs):
                self.book_calls.append((str(seat_id), begin_time, duration_hours))
                self.book_call_times.append(time.monotonic())
                return not_open if str(seat_id) == "21" else success

            def current_bookings(self):
                seat_id, begin_time, duration_hours = self.book_calls[-1]
                return [{
                    "id": "789",
                    "seat_num": seat_id,
                    "start_timestamp": int(begin_time.timestamp()),
                    "duration_seconds": duration_hours * 3600,
                    "status": "0",
                    "label": f"{seat_id}座 confirmed",
                }]

        fake = OpeningFallbackBooker()
        config = {"booking": {}, **minimal_config()}
        logs = []
        with (
            patch.object(instant_book, "load_config", return_value=config),
            patch.object(instant_book, "InstantBooker", return_value=fake),
        ):
            result = instant_book.run_booking(
                config_path="unused.yaml",
                plan_text="1:1558:21:7:15",
                fallback_seats="271",
                days=2,
                dry_run_override=False,
                execute_at="",
                max_trials=1,
                retry_delay=0.05,
                logger=logs.append,
            )

        self.assertEqual([call[0] for call in fake.book_calls], ["21", "271"])
        self.assertEqual(result["booked_seat_num"], "271")
        self.assertTrue(result["used_fallback"])
        self.assertTrue(any("预约入口未开放，3 秒后切换" in line for line in logs))
        self.assertGreaterEqual(fake.book_call_times[1] - fake.book_call_times[0], 3.0)


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tz = timezone(timedelta(hours=8))

    def test_just_missed_execution_uses_grace(self):
        now = datetime(2026, 7, 14, 20, 0, 0, 100_000, tzinfo=self.tz)
        self.assertEqual(instant_book.build_execute_time("20:00:00", now=now), now)

    def test_fractional_execution_time_targets_500ms(self):
        now = datetime(2026, 7, 14, 19, 59, 59, tzinfo=self.tz)
        target = instant_book.build_execute_time("20:00:00.500", now=now)
        self.assertEqual(target.hour, 20)
        self.assertEqual(target.second, 0)
        self.assertEqual(target.microsecond, 500_000)
        self.assertEqual(instant_book.normalize_execute_at("20:00:00.5"), "20:00:00.500")
        self.assertEqual(
            instant_book.format_execute_datetime(target),
            "2026-07-14 20:00:00.500",
        )

    def test_missed_execution_does_not_silently_roll_to_tomorrow(self):
        now = datetime(2026, 7, 14, 20, 0, 6, tzinfo=self.tz)
        with self.assertRaisesRegex(ValueError, "任务已停止"):
            instant_book.build_execute_time("20:00:00", now=now)

    def test_wait_until_has_no_200ms_floor(self):
        target = datetime.now().astimezone() + timedelta(seconds=0.05)
        started = time.monotonic()
        logs = []
        instant_book.wait_until(target, logger=logs.append)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.035)
        self.assertLess(elapsed, 0.15)
        self.assertIn("到点偏差", logs[-1])


class PersistentLogTests(unittest.TestCase):
    def test_web_job_log_is_private_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            old_log_dir = web_app.LOG_DIR
            web_app.LOG_DIR = Path(directory) / "logs"
            try:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.clear()
                job = web_app.create_job_record()
                web_app.append_job_log(job, "test-line")
                text = job["log_path"].read_text(encoding="utf-8")
                self.assertIn("test-line", text)
                self.assertEqual(os.stat(job["log_path"]).st_mode & 0o777, 0o600)
                self.assertEqual(os.stat(web_app.LOG_DIR).st_mode & 0o777, 0o700)
            finally:
                with web_app.JOBS_LOCK:
                    web_app.JOBS.clear()
                web_app.LOG_DIR = old_log_dir


class WebImmediateBookingTests(unittest.TestCase):
    def test_force_immediate_ignores_scheduled_time(self):
        payload = {"execute_at": "20:00:00"}
        self.assertEqual(web_app.booking_execute_at_from_payload(payload), "20:00:00")
        self.assertEqual(
            web_app.booking_execute_at_from_payload(payload, force_immediate=True),
            "",
        )

    def test_run_now_handler_forces_immediate_mode(self):
        payload = {"execute_at": "20:00:00", "seat_num": "21"}
        handler = object.__new__(web_app.WebHandler)
        handler.read_json = lambda: payload
        with patch.object(web_app, "start_booking_job", return_value="job-1") as start:
            result = handler.run_booking_now()
        start.assert_called_once_with(payload, force_immediate=True)
        self.assertEqual(result, {"job_id": "job-1", "mode": "immediate"})

    def test_page_has_dedicated_immediate_action(self):
        self.assertIn('id="instantRunBtn"', web_app.INDEX_HTML)
        self.assertIn('id="fallbackSeats"', web_app.INDEX_HTML)
        self.assertIn('id="continueSeatBtn"', web_app.INDEX_HTML)
        self.assertIn('requestJson("/api/continue-seat"', web_app.INDEX_HTML)
        self.assertIn('requestJson(immediate ? "/api/run-now" : "/api/run"', web_app.INDEX_HTML)

    def test_save_plan_persists_fallback_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "booking:\n"
                "  plan: 1:1558:21:7:15\n"
                "  execute_at: '20:00:00'\n"
                "  max_trials: 10\n"
                "  retry_delay: 0.2\n"
                "  dry_run: false\n",
                encoding="utf-8",
            )
            web_app.write_booking_values(
                path,
                "1:1558:21:7:15",
                "22, 23,22",
                2,
                False,
                "20:00:00",
                10,
                0.2,
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("fallback_seats: '22,23'", text)


if __name__ == "__main__":
    unittest.main()
