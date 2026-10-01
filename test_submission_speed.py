"""Offline timing checks for the 3-second submission rule and first send."""

import base64
import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

import instant_book as ib
import web_app as web
from test_local_optimization import FakeBooker


class VirtualClock:
    strptime = staticmethod(datetime.strptime)
    fromtimestamp = staticmethod(datetime.fromtimestamp)

    def __init__(self):
        self.elapsed = 0.0
        self.origin = datetime(2026, 9, 20, 19, 59, 59, tzinfo=timezone(timedelta(hours=8)))

    def now(self):
        return self.origin + timedelta(seconds=self.elapsed)

    def time(self):
        return self.origin.timestamp() + self.elapsed

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class SpeedTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock()
        for patcher in (
            patch.object(ib, "datetime", self.clock),
            patch.object(ib.time, "time", self.clock.time),
            patch.object(ib.time, "monotonic", self.clock.monotonic),
            patch.object(ib.time, "sleep", self.clock.sleep),
            patch.object(ib, "_BOOKING_GATES", {}),
            patch("requests.sessions.Session.request", side_effect=AssertionError("No upstream HTTP allowed")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.begin = self.clock.origin + timedelta(days=2)

    def booker(self, uid="123"):
        booker = ib.InstantBooker({
            "urls": {"book_seat": "https://hdu.huitu.zhishulib.com/Seat/Index/bookSeats"},
            "session": {"headers": {}, "verify": True},
            "user_info": {"uid": uid},
        })
        self.addCleanup(booker.session.close)
        return booker


class CooldownTests(SpeedTestCase):
    def test_temporary_hold_uses_official_endpoint_without_booking_token(self):
        booker = self.booker()
        booker.request = Mock(return_value={"CODE": "ok", "DATA": {"result": "success"}})
        booker.lock_seat("63025", self.begin, 11)
        method, url, payload = booker.request.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://hdu.huitu.zhishulib.com/Seat/Index/lockSeats")
        self.assertEqual(payload["seats[0]"], "63025")
        self.assertEqual(payload["beginTime"], int(self.begin.timestamp()))
        self.assertEqual(payload["duration"], 11 * 3600)
        self.assertNotIn("seatBookers[0]", payload)

    def test_config_and_web_cannot_lower_three_second_minimum(self):
        for value in (0, -1, 0.05, "0.2", float("nan"), float("inf"), None, "invalid"):
            with self.subTest(value=value):
                self.assertEqual(ib.normalize_retry_delay(value), 3.0)
                self.assertEqual(web.normalize_retry_delay(value), 3.0)
        self.assertEqual(ib.normalize_retry_delay(5), 5)

    def test_new_client_and_fallback_share_account_cooldown_after_response(self):
        first, second = self.booker(), self.booker()
        calls = []
        def response(*args, **kwargs):
            start = self.clock.elapsed
            self.clock.sleep(0.4)
            calls.append((start, self.clock.elapsed))
            return {"CODE": "ok", "DATA": {"result": "fail"}}
        first.request = second.request = response
        first.book("21", self.begin, 1)
        second.book("22", self.begin, 1)
        self.assertAlmostEqual(calls[0][0], 0)
        self.assertGreaterEqual(calls[1][0] - calls[0][1], 3)
        self.assertAlmostEqual(calls[1][0], 3.4)

    def test_exception_also_starts_a_cooldown(self):
        booker = self.booker()
        sent = []
        def request(*args, **kwargs):
            sent.append(self.clock.elapsed)
            self.clock.sleep(0.2)
            if len(sent) == 1:
                raise ib.RequestFailure("connect timeout", True, outcome_unknown=False)
            return {"CODE": "ok"}
        booker.request = request
        with self.assertRaises(ib.RequestFailure):
            booker.book("21", self.begin, 1)
        booker.book("22", self.begin, 1)
        self.assertGreaterEqual(sent[1] - (sent[0] + 0.2), 3)

    def test_queries_do_not_wait_for_or_extend_booking_cooldown(self):
        booker = self.booker()
        booker.request = Mock(return_value=[])
        booker.book("21", self.begin, 1)
        booker.current_bookings()
        self.assertEqual(self.clock.elapsed, 0)
        booker.book("22", self.begin, 1)
        self.assertAlmostEqual(self.clock.elapsed, 3)

    def test_dry_run_does_not_consume_submission_slot(self):
        booker = self.booker()
        booker.request = Mock(return_value={})
        result = booker.book("21", self.begin, 1, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(ib._BOOKING_GATES, {})
        booker.book("21", self.begin, 1)
        self.assertEqual(self.clock.elapsed, 0)

    def test_cancel_during_cooldown_sends_nothing_and_releases_lock(self):
        booker = self.booker()
        booker.request = Mock(return_value={})
        booker.book("21", self.begin, 1)
        with self.assertRaises(ib.TaskCancelled):
            booker.book("22", self.begin, 1, should_cancel=lambda: self.clock.elapsed >= 0.2)
        self.assertEqual(booker.request.call_count, 1)
        booker.book("23", self.begin, 1)
        self.assertEqual(booker.request.call_count, 2)
        self.assertAlmostEqual(self.clock.elapsed, 3)

    def test_other_account_does_not_share_cooldown(self):
        first, second = self.booker("123"), self.booker("456")
        first.request = second.request = Mock(return_value={})
        first.book("21", self.begin, 1)
        second.book("22", self.begin, 1)
        self.assertEqual(self.clock.elapsed, 0)


class RequestPreparationTests(SpeedTestCase):
    def test_reused_template_refreshes_timestamp_and_signature_after_cooldown(self):
        booker = self.booker()
        captured = []
        booker.request = lambda method, url, data=None, headers=None: captured.append((dict(data), dict(headers))) or {}
        template = ib.prepare_booking_request(booker.uid, "21", self.begin, 1)
        booker.book("21", self.begin, 1, prepared=template)
        booker.book("21", self.begin, 1, prepared=template)
        self.assertEqual(captured[1][0]["api_time"] - captured[0][0]["api_time"], 3)
        self.assertEqual(template["payload"]["api_time"], 0)
        for data, headers in captured:
            canonical = (
                "post&/Seat/Index/bookSeats?LAB_JSON=1"
                f"&api_time{data['api_time']}&beginTime{data['beginTime']}"
                f"&duration{data['duration']}&is_recommend{data['is_recommend']}"
                f"&seatBookers[0]{data['seatBookers[0]']}&seats[0]{data['seats[0]']}"
            )
            expected = base64.b64encode(hashlib.md5(canonical.encode()).hexdigest().encode()).decode()
            self.assertEqual(headers["Api-Token"], expected)

    def test_cookies_are_read_from_live_session_not_frozen_in_template(self):
        booker = self.booker()
        cookies = []
        booker._load_cookie_header("auth=old")
        def post(url, data=None, headers=None, **kwargs):
            prepared = booker.session.prepare_request(requests.Request("POST", url, data=data, headers=headers))
            cookies.append(prepared.headers["Cookie"])
            booker._load_cookie_header("auth=new")
            return Mock(status_code=200, json=Mock(return_value={"CODE": "ok"}))
        template = ib.prepare_booking_request(booker.uid, "21", self.begin, 1)
        with patch.object(booker.session, "post", side_effect=post):
            booker.book("21", self.begin, 1, prepared=template)
            booker.book("21", self.begin, 1, prepared=template)
        self.assertIn("auth=old", cookies[0])
        self.assertIn("auth=new", cookies[1])
        self.assertNotIn("auth=old", cookies[1])

    def test_warmup_refreshes_prepared_seat_and_mode(self):
        fake = FakeBooker()
        fake.last_seat_query_meta = {}
        def seat(floors, floor_id, seat_num):
            return floors[0], {"id": "777" if floors[0].get("warm") else "62585", "title": seat_num, "state": "0"}
        fake.find_seat = seat
        def warm(*args, **kwargs):
            fake.last_seat_query_meta["is_recommend"] = 1
            return [{"roomName": "四楼", "warm": True}]
        fake._query_seat_map_once = warm
        book = Mock(wraps=fake.book)
        fake.book = book
        with patch.object(ib, "load_config", return_value={"booking": {}}), \
             patch.object(ib, "InstantBooker", return_value=fake):
            ib.run_booking(plan_text="1:1558:21:8:1", days=2, execute_at="20:00:06",
                           max_trials=1, logger=lambda _: None)
        template = book.call_args.kwargs["prepared"]
        self.assertEqual(template["payload"]["seats[0]"], "777")
        self.assertEqual(template["payload"]["is_recommend"], 1)


class FirstSendTests(SpeedTestCase):
    def test_slow_ready_and_submission_logs_do_not_delay_first_send(self):
        fake = FakeBooker()
        send_times = []
        log_times = []
        template_times = []
        original_book = fake.book
        original_prepare = ib.prepare_booking_request
        def book(*args, **kwargs):
            send_times.append(self.clock.elapsed)
            self.clock.sleep(0.02)
            return original_book(*args, **kwargs)
        fake.book = book
        def prepare(*args, **kwargs):
            template_times.append(self.clock.elapsed)
            return original_prepare(*args, **kwargs)
        def slow_logger(message):
            if "到点偏差" in message or "[try=" in message:
                log_times.append(self.clock.elapsed)
                self.clock.sleep(0.25)
        with patch.object(ib, "load_config", return_value={"booking": {}}), \
             patch.object(ib, "InstantBooker", return_value=fake), \
             patch.object(ib, "prepare_booking_request", side_effect=prepare):
            ib.run_booking(plan_text="1:1558:21:8:1", days=2, execute_at="20:00:00.920",
                           max_trials=1, logger=slow_logger)
        self.assertAlmostEqual(send_times[0], 1.92)
        self.assertTrue(all(when >= send_times[0] + 0.02 for when in log_times))
        self.assertTrue(all(when < send_times[0] for when in template_times))

    def test_timing_excludes_cooldown_and_measures_request_call(self):
        booker = self.booker()
        def request(*args, **kwargs):
            self.clock.sleep(0.2)
            return {}
        booker.request = request
        booker.book("21", self.begin, 1)
        booker.book("22", self.begin, 1)
        timing = booker.last_submission_timing
        self.assertAlmostEqual(timing["elapsed_ms"], 200)
        self.assertAlmostEqual((timing["sent_at"] - self.clock.origin).total_seconds(), 3.2)


class PollingTests(SpeedTestCase):
    def test_long_wait_uses_two_second_polling(self):
        job = {"status": "running", "execute_timestamp": self.clock.time() + 3600}
        self.assertEqual(web.job_poll_after_ms(job), 2000)

    def test_critical_window_waits_until_after_target(self):
        for remaining in (2, 1.2, 0.1):
            with self.subTest(remaining=remaining):
                job = {"status": "running", "execute_timestamp": self.clock.time() + remaining}
                delay = web.job_poll_after_ms(job) / 1000
                self.assertGreater(delay, remaining + 0.49)

    def test_after_send_polling_resumes(self):
        job = {"status": "running", "execute_timestamp": self.clock.time() - 1}
        self.assertEqual(web.job_poll_after_ms(job), 1000)


if __name__ == "__main__":
    unittest.main()
