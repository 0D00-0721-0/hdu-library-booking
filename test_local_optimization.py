"""Local regression tests. Upstream HTTP calls are blocked, never sent."""

import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from libcs import (
    booking,
    client,
    configuration,
    constants,
    errors,
    operations,
    records,
    scheduling,
)
from libcs.web import forms as web_forms
from libcs.web import jobs as web_jobs
from test_support import patch_runtime


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected HTTP request"))
        blocker.start()
        self.addCleanup(blocker.stop)


class FakeBooker:
    uid = "123"
    name = "tester"
    keepalive_interval = 0
    timeout = 1
    last_seat_query_meta = {}

    def __init__(self, responses=None, commit=True, on_submit=None):
        self.responses = list(responses or [{"CODE": "ok", "DATA": {"result": "success", "bookingId": 123}}])
        self.commit = commit
        self.on_submit = on_submit
        self.book_calls = []
        self.bookings = []
        self.confirmation_calls = 0

    def load_cookies(self):
        pass

    def resolve_user(self):
        pass

    def query_room_items(self):
        return [{"name": "自习室", "query": "x=1"}]

    def query_room_detail(self, *args):
        return {"range": {}}

    def validate_booking_time(self, *args, **kwargs):
        pass

    def query_seat_map(self, *args, **kwargs):
        return [{"roomName": "四楼"}]

    def find_seat(self, floors, floor_id, seat_num):
        return floors[0], {"id": "62585", "title": seat_num, "state": "0"}

    def keepalive(self):
        return "mock"

    def book(self, seat_id, begin_time, duration_hours, **kwargs):
        self.book_calls.append(seat_id)
        response = self.responses.pop(0)
        if self.commit and not (isinstance(response, errors.RequestFailure) and not response.outcome_unknown):
            self.bookings = [{
                "id": "123", "seat_id": seat_id, "seat_num": "21", "floor_id": "1558",
                "room_name": "四楼", "status": "0", "label": "四楼21座",
                "start_timestamp": int(begin_time.timestamp()), "duration_seconds": duration_hours * 3600,
            }]
        if self.on_submit:
            self.on_submit(self)
        if isinstance(response, Exception):
            raise response
        return response

    def current_bookings(self):
        self.confirmation_calls += 1
        return self.bookings


def run_fake_booking(fake, **kwargs):
    options = dict(plan_text="1:1558:21:8:1", days=2, execute_at="", max_trials=1, logger=lambda _: None)
    options.update(kwargs)
    clock = [time.monotonic()]
    def advance(seconds):
        clock[0] += seconds
    with patch.object(configuration, "load_config", return_value={"booking": {}}), \
         patch.object(client, "InstantBooker", return_value=fake), \
         patch.object(time, "sleep", side_effect=advance), \
         patch.object(time, "monotonic", side_effect=lambda: clock[0]):
        return booking.run_booking(**options)


class SubmissionTests(OfflineTestCase):
    def test_read_timeout_after_commit_is_confirmed_without_resubmitting(self):
        fake = FakeBooker([errors.RequestFailure("read timeout", retryable=True)])
        result = run_fake_booking(fake)
        self.assertEqual(result["confirmed_booking"]["id"], "123")
        self.assertEqual(len(fake.book_calls), 1)
        self.assertEqual(fake.confirmation_calls, 1)

    def test_unconfirmed_timeout_stops_retries_and_fallbacks(self):
        fake = FakeBooker([errors.RequestFailure("read timeout", retryable=True)], commit=False)
        with self.assertRaisesRegex(errors.ResultUncertain, "结果待确认"):
            run_fake_booking(fake, max_trials=3, fallback_seats="22")
        self.assertEqual(len(fake.book_calls), 1)
        self.assertEqual(fake.confirmation_calls, 3)

    def test_connect_timeout_can_retry(self):
        fake = FakeBooker([
            errors.RequestFailure("connect timeout", retryable=True, outcome_unknown=False),
            {"CODE": "ok", "DATA": {"result": "success", "bookingId": 123}},
        ])
        result = run_fake_booking(fake, max_trials=2, retry_delay=0.05)
        self.assertEqual(len(fake.book_calls), 2)
        self.assertIsNotNone(result["confirmed_booking"])

    def test_success_response_without_record_is_uncertain(self):
        fake = FakeBooker(commit=False)
        with self.assertRaises(errors.ResultUncertain):
            run_fake_booking(fake)

    def test_cancellation_after_commit_does_not_skip_confirmation(self):
        cancelled = threading.Event()
        fake = FakeBooker(on_submit=lambda _: cancelled.set())
        result = run_fake_booking(fake, should_cancel=cancelled.is_set)
        self.assertEqual(result["confirmed_booking"]["id"], "123")
        self.assertEqual(fake.confirmation_calls, 1)

    def test_cancellation_during_unknown_submit_remains_uncertain(self):
        cancelled = threading.Event()
        fake = FakeBooker([errors.RequestFailure("timeout", True)], commit=False,
                          on_submit=lambda _: cancelled.set())
        with self.assertRaises(errors.ResultUncertain):
            run_fake_booking(fake, should_cancel=cancelled.is_set)
        self.assertEqual(fake.confirmation_calls, 3)

    def test_cancellation_before_submission_sends_nothing(self):
        fake = FakeBooker()
        with self.assertRaises(errors.TaskCancelled):
            run_fake_booking(fake, should_cancel=lambda: True)
        self.assertEqual(fake.book_calls, [])

    def test_duplicate_booking_on_other_floor_is_rejected(self):
        duplicate = {"CODE": "ok", "DATA": {"result": "fail", "msg": constants.MSG_DUPLICATE}}
        def wrong_floor(fake):
            fake.bookings[0].update(floor_id="1559", room_name="六楼", seat_id="99999")
        fake = FakeBooker([duplicate], on_submit=wrong_floor)
        with self.assertRaisesRegex(RuntimeError, "已有预约"):
            run_fake_booking(fake)

    def test_duplicate_booking_in_target_room_is_confirmed(self):
        duplicate = {"CODE": "ok", "DATA": {"result": "fail", "msg": constants.MSG_DUPLICATE}}
        fake = FakeBooker([duplicate])
        self.assertEqual(run_fake_booking(fake)["confirmed_booking"]["id"], "123")


class BookingIdentityTests(OfflineTestCase):
    def test_stable_seat_id_takes_precedence_over_display_name(self):
        begin = scheduling.build_begin_time(8, 2)
        item = {"id": "123", "seat_id": "62585", "seat_num": "21", "room_name": "四楼（宋韵云图）",
                "start_timestamp": int(begin.timestamp()), "duration_seconds": 3600, "status": "0"}
        self.assertIs(records.find_matching_booking([item], "21", begin, 1,
                                              seat_id="62585", room_name="宋韵云图"), item)

    def test_missing_identity_is_not_enough_for_timeout_confirmation(self):
        begin = scheduling.build_begin_time(8, 2)
        item = {"id": "123", "seat_num": "21", "start_timestamp": int(begin.timestamp()),
                "duration_seconds": 3600, "status": "0"}
        self.assertIsNone(records.find_matching_booking([item], "21", begin, 1, room_name="四楼"))
        self.assertIs(records.find_matching_booking([item], "21", begin, 1, room_name="四楼",
                                              expected_booking_id="123"), item)

    def test_expected_id_is_applied_to_every_candidate(self):
        begin = scheduling.build_begin_time(8, 2)
        first = {"id": "111", "seat_num": "21", "start_timestamp": int(begin.timestamp()),
                 "duration_seconds": 3600, "status": "0"}
        second = {**first, "id": "123"}
        self.assertIs(records.find_matching_booking([first, second], "21", begin, 1,
                                              expected_booking_id="123"), second)

    def test_terminal_and_unknown_statuses_do_not_confirm_booking(self):
        begin = scheduling.build_begin_time(8, 2)
        for status in ("3", "4", "5", "6", "7", "9", "", None):
            with self.subTest(status=status):
                item = {"id": "123", "seat_num": "21", "start_timestamp": int(begin.timestamp()),
                        "duration_seconds": 3600, "status": status}
                self.assertIsNone(records.find_matching_booking([item], "21", begin, 1))

    def test_numeric_zero_remains_pending_and_cancelable(self):
        item = records.format_booking_item({"id": "123", "status": 0, "time": 1700000000})
        self.assertEqual(item["status"], "0")
        self.assertTrue(item["cancelable"])
        self.assertEqual(len(records.pending_check_in_tasks([item])), 1)


class CheckInTests(OfflineTestCase):
    def fake(self, response, statuses=("0", "0", "1")):
        start = int(time.time()) - 600
        rows = [[{"id": "123", "status": status, "status_label": constants.BOOKING_STATUS_LABELS[status],
                  "start_timestamp": start}] if status is not None else [] for status in statuses]
        fake = Mock()
        fake.current_bookings.side_effect = rows
        fake.check_in_booking.side_effect = response if isinstance(response, Exception) else None
        fake.check_in_booking.return_value = response
        return fake

    def run_check_in(self, fake, **kwargs):
        with patch.object(operations, "create_booker", return_value=fake), patch.object(time, "sleep"):
            return operations.run_auto_check_in(booking_id="123", logger=lambda _: None, **kwargs)

    def test_unknown_and_login_failure_responses_are_not_success(self):
        for response in ({}, {"CODE": "notLogin"}, {"CODE": "ok", "DATA": {"result": "pending"}}):
            with self.subTest(response=response), self.assertRaisesRegex(RuntimeError, "签到失败"):
                self.run_check_in(self.fake(response))

    def test_success_requires_active_state(self):
        fake = self.fake({"CODE": "ok", "DATA": {"result": "success"}}, ("0", "0", "0", "0", "0"))
        with self.assertRaises(errors.ResultUncertain):
            self.run_check_in(fake)

    def test_delayed_active_state_is_confirmed(self):
        fake = self.fake({"CODE": "ok", "DATA": {"result": "success"}}, ("0", "0", "0", "1"))
        result = self.run_check_in(fake)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code_after"], "1")
        fake.check_in_booking.assert_called_once_with("123")

    def test_timeout_after_success_is_confirmed_without_second_check_in(self):
        fake = self.fake(errors.RequestFailure("timeout", True))
        result = self.run_check_in(fake)
        self.assertTrue(result["ok"])
        fake.check_in_booking.assert_called_once()

    def test_disappeared_or_cancelled_booking_is_failure(self):
        for status in (None, "4", "6"):
            with self.subTest(status=status):
                fake = self.fake({}, ("0", status))
                with self.assertRaises(RuntimeError):
                    self.run_check_in(fake)
                fake.check_in_booking.assert_not_called()

    def test_already_checked_in_is_success_without_another_post(self):
        fake = self.fake({}, ("0", "1"))
        result = self.run_check_in(fake)
        self.assertTrue(result["ok"])
        self.assertFalse(result["sent"])
        fake.check_in_booking.assert_not_called()


class Clock:
    strptime = staticmethod(datetime.strptime)

    def __init__(self):
        self.elapsed = 0.0
        # Scheduled HH:MM values use the machine's local clock. Keep the fake
        # wall clock in that same zone instead of assuming the runner is UTC+8.
        self.start = datetime(2026, 9, 20, 19, 59, 59).astimezone()

    def now(self):
        return self.start + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class SchedulingBoundaryTests(OfflineTestCase):
    def clock(self):
        clock = Clock()
        for target, replacement in (("datetime", clock), ("time.monotonic", clock.monotonic),
                                    ("time.sleep", clock.sleep)):
            patcher = patch_runtime(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        return clock

    def test_warmup_is_skipped_when_time_is_too_short(self):
        clock = self.clock()
        warmup = Mock()
        scheduling.wait_until(clock.now() + timedelta(seconds=0.1), warmup=warmup, logger=lambda _: None)
        warmup.assert_not_called()
        self.assertAlmostEqual(clock.elapsed, 0.1)

    def test_warmup_runs_with_sufficient_budget(self):
        clock = self.clock()
        warmup = Mock(side_effect=lambda: clock.sleep(1))
        scheduling.wait_until(clock.now() + timedelta(seconds=6), warmup=warmup, logger=lambda _: None)
        warmup.assert_called_once()
        self.assertAlmostEqual(clock.elapsed, 6)

    def test_slow_heartbeat_does_not_add_a_stale_sleep(self):
        clock = self.clock()
        scheduling.wait_until(clock.now() + timedelta(seconds=12), heartbeat=lambda: clock.sleep(3),
                      heartbeat_interval=10, heartbeat_guard_seconds=0, logger=lambda _: None)
        self.assertAlmostEqual(clock.elapsed, 13)

    def test_waiting_cancels_within_a_quarter_second(self):
        clock = self.clock()
        with self.assertRaises(errors.TaskCancelled):
            scheduling.wait_until(clock.now() + timedelta(hours=1),
                          should_cancel=lambda: clock.elapsed >= 0.25, logger=lambda _: None)
        self.assertLessEqual(clock.elapsed, 0.25)

    def test_preparation_overrun_prevents_late_submission(self):
        clock = self.clock()
        fake = FakeBooker()
        def slow_map(*args, **kwargs):
            clock.sleep(10)
            return [{"roomName": "四楼"}]
        fake.query_seat_map = slow_map
        with self.assertRaisesRegex(RuntimeError, "错过执行时间"):
            run_fake_booking(fake, execute_at="20:00:00")
        self.assertEqual(fake.book_calls, [])


class RequestClassificationTests(OfflineTestCase):
    def test_transport_errors_distinguish_sent_from_not_sent(self):
        for error, unknown in ((requests.ConnectTimeout("connect"), False), (requests.ReadTimeout("read"), True)):
            with self.subTest(error=error):
                booker = client.InstantBooker({"urls": {}, "session": {"headers": {}}})
                with patch.object(booker.session, "post", side_effect=error), self.assertRaises(errors.RequestFailure) as exc:
                    booker.request("POST", "https://example.invalid")
                self.assertEqual(exc.exception.outcome_unknown, unknown)

    def test_rate_limit_and_server_error_have_different_outcomes(self):
        for code, unknown in ((429, False), (503, True)):
            with self.subTest(code=code):
                booker = client.InstantBooker({"urls": {}, "session": {"headers": {}}})
                with patch.object(booker.session, "post", return_value=Mock(status_code=code)), \
                     self.assertRaises(errors.RequestFailure) as exc:
                    booker.request("POST", "https://example.invalid")
                self.assertEqual(exc.exception.outcome_unknown, unknown)

    def test_login_error_is_not_an_empty_booking_list(self):
        booker = client.InstantBooker({"urls": {}, "session": {"headers": {}}})
        for response in ({}, {"CODE": "notLogin"}):
            with patch.object(booker, "request", return_value=response), self.assertRaises(errors.RequestFailure):
                booker.current_bookings()


class LocalJobAndConfigTests(OfflineTestCase):
    def test_real_booking_worker_reports_success_after_stop_during_submit(self):
        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        def cancel_during_submit(_):
            active = next(job for job in web_jobs.JOBS.values() if job["status"] == "running")
            active["cancel_event"].set()

        fake = FakeBooker(on_submit=cancel_during_submit)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web_jobs, "LOG_DIR", Path(directory)), patch.object(web_jobs, "JOBS", {}), \
             patch.object(configuration, "load_config", return_value={"booking": {}}), \
             patch.object(configuration, "load_config", return_value={"booking": {}}), \
             patch.object(client, "InstantBooker", return_value=fake), \
             patch.object(threading, "Thread", InlineThread):
            job_id = web_jobs.start_booking_job({"room_type": "1", "floor_id": "1558", "seat_num": "21",
                                           "start_hour": "8", "duration_hours": "1", "days": 2})
            snapshot = web_jobs.job_snapshot(job_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertIn("预约成功", snapshot["message"])
        self.assertEqual(fake.confirmation_calls, 1)

    def test_job_status_uses_business_outcome_even_after_stop_requested(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(web_jobs, "LOG_DIR", Path(directory)), \
             patch.object(web_jobs, "JOBS", {}):
            for outcome, expected in ((errors.ResultUncertain("结果待确认"), "uncertain"),
                                      (RuntimeError("实际错误"), "error"),
                                      (errors.TaskCancelled("停止"), "cancelled"),
                                      ({"ok": False, "message": "没有完成"}, "error"),
                                      ({"ok": True}, "done")):
                with self.subTest(outcome=outcome):
                    job = web_jobs.create_job_record("auto_check_in")
                    job["cancel_event"].set()
                    action = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
                    web_jobs.execute_job(job, action)
                    self.assertEqual(job["status"], expected)
                    self.assertEqual(job["logs"][-1].split("] ", 1)[1], job["message"])

    def test_save_without_final_newline_is_valid_and_keeps_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("request:\n  timeout: 10\nbooking:\n  plan: '1:1558:21:8:1'", encoding="utf-8")
            web_forms.write_booking_values(path, "1:1558:22:8:1", "23", 2, False, "20:00:00.500", 1, 0.2)
            config = configuration.load_config(path)
            self.assertEqual(config["booking"]["plan"], "1:1558:22:8:1")
            self.assertEqual(config["booking"]["fallback_seats"], "23")
            self.assertEqual(config["request"]["timeout"], 10)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_atomic_write_failure_preserves_original_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            original = "booking:\n  plan: '1:1558:21:8:1'\n"
            path.write_text(original, encoding="utf-8")
            with patch.object(os, "replace", side_effect=OSError("simulated disk failure")), self.assertRaises(OSError):
                web_forms.write_booking_values(path, "1:1558:22:8:1", "", 2, False, "", 1, 0.2)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
