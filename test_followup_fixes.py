"""Offline regression tests for the follow-up review; all upstream HTTP blocked."""

import time
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from libcs import (
    booking,
    client,
    configuration,
    constants,
    errors,
    operations,
    scheduling,
)
from libcs.web import jobs as web_jobs
from libcs.web import server as web_server
from test_local_optimization import FakeBooker, OfflineTestCase, run_fake_booking
from test_submission_speed import SpeedTestCase


class AmbiguousBookingTests(OfflineTestCase):
    def test_incomplete_success_is_confirmed_without_resubmission(self):
        for response in ({}, {"CODE": "ok"}, {"CODE": "ok", "DATA": {"result": "success"}}):
            with self.subTest(response=response):
                fake = FakeBooker([response])
                result = run_fake_booking(fake, max_trials=3, fallback_seats="22")
                self.assertEqual(result["confirmed_booking"]["id"], "123")
                self.assertEqual(fake.confirmation_calls, 1)
                self.assertEqual(len(fake.book_calls), 1)

    def test_unconfirmed_response_never_retries_or_switches_seat(self):
        for response in ({}, {"CODE": "ok", "DATA": {"result": "pending", "msg": constants.MSG_SEAT_UNAVAILABLE}},
                         {"CODE": "ok", "DATA": {"result": "success", "bookingId": "broken"}}):
            with self.subTest(response=response):
                fake = FakeBooker([response], commit=False)
                with self.assertRaises(errors.ResultUncertain):
                    run_fake_booking(fake, max_trials=3, fallback_seats="22")
                self.assertEqual(len(fake.book_calls), 1)
                self.assertEqual(fake.confirmation_calls, 3)

    def test_unknown_response_does_not_confirm_other_floor(self):
        fake = FakeBooker([{}], on_submit=lambda b: b.bookings[0].update(seat_id="999", floor_id="999"))
        with self.assertRaises(errors.ResultUncertain):
            run_fake_booking(fake)

    def test_stop_after_ambiguous_reply_still_confirms_result(self):
        stopped = [False]
        fake = FakeBooker([{}], on_submit=lambda _: stopped.__setitem__(0, True))
        result = run_fake_booking(fake, should_cancel=lambda: stopped[0])
        self.assertIsNotNone(result["confirmed_booking"])
        self.assertEqual(len(fake.book_calls), 1)

    def test_explicit_rejections_remain_failures(self):
        for response in ({"CODE": "notLogin"}, {"CODE": "ParamError", "DATA": {"result": "fail", "msg": "时长错误"}}):
            with self.subTest(response=response):
                fake = FakeBooker([response], commit=False)
                with self.assertRaises(RuntimeError) as caught:
                    run_fake_booking(fake, max_trials=3, fallback_seats="22")
                self.assertNotIsInstance(caught.exception, errors.ResultUncertain)
                self.assertEqual(fake.confirmation_calls, 0)
                self.assertEqual(len(fake.book_calls), 1)


class WarmBooker(FakeBooker):
    def __init__(self, warm_seats=("21", "22")):
        super().__init__()
        self.warm_seats = warm_seats
        self.last_seat_query_meta = {}

    def _query_seat_map_once(self, *args, **kwargs):
        self.last_seat_query_meta = {"is_recommend": 1}
        return [{"roomName": "四楼", "warm": True}]

    def find_seat(self, floors, floor_id, seat_num):
        warm = floors[0].get("warm")
        if warm and seat_num not in self.warm_seats:
            raise RuntimeError(f"找不到 {seat_num} 座")
        return floors[0], {"id": ("fresh-" if warm else "old-") + seat_num, "title": seat_num, "state": "0"}

    def book(self, seat_id, begin_time, duration_hours, **kwargs):
        self.last_template = kwargs["prepared"]
        result = super().book(seat_id, begin_time, duration_hours, **kwargs)
        self.bookings[0]["seat_num"] = seat_id.split("-")[-1]
        return result


class WarmupAndClockTests(SpeedTestCase):
    def scheduled(self, fake, **kwargs):
        options = dict(plan_text="1:1558:21:8:1", days=2, execute_at="20:00:06",
                       max_trials=1, logger=lambda _: None)
        options.update(kwargs)
        with patch.object(configuration, "load_config", return_value={"booking": {}}), \
             patch.object(client, "InstantBooker", return_value=fake):
            return booking.run_booking(**options)

    def adjust_clock(self, shift):
        original_now = self.clock.now
        self.clock.now = lambda: original_now() + timedelta(seconds=shift if self.clock.elapsed >= 1 else 0)
        patcher = patch.object(time, "time", lambda: self.clock.now().timestamp())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_fallback_does_not_block_primary(self):
        fake = WarmBooker(("21",))
        result = self.scheduled(fake, fallback_seats="22")
        self.assertEqual(fake.book_calls, ["fresh-21"])
        self.assertEqual([c["seat_num"] for c in result["seat_candidates"]], ["21"])
        self.assertFalse(result["used_fallback"])
        self.assertEqual(fake.last_template["payload"]["seats[0]"], "fresh-21")
        self.assertEqual(fake.last_template["payload"]["is_recommend"], 1)

    def test_dry_run_with_early_hold_never_locks_or_submits(self):
        fake = WarmBooker()
        fake.lock_seat = Mock(side_effect=AssertionError("Unexpected seat hold"))
        fake.book = Mock(return_value={"payload": {"seats[0]": "fake-seat"}})
        result = self.scheduled(fake, hold_before_minutes=1, dry_run_override=True, fallback_seats="22")
        fake.lock_seat.assert_not_called()
        self.assertTrue(result["dry_run"])
        self.assertEqual(fake.book.call_count, 2)
        for call in fake.book.call_args_list:
            self.assertIs(call.kwargs["dry_run"], True)
        self.assertIsNone(result["confirmed_booking"])

    def test_early_hold_then_book_at_execution_time(self):
        fake = WarmBooker()
        fake.lock_seat = Mock(return_value={
            "CODE": "ok", "DATA": {"result": "success", "time": self.clock.time()}
        })
        sent = []
        fake.on_submit = lambda _: sent.append(self.clock.now())
        result = self.scheduled(fake, hold_before_minutes=1)
        fake.lock_seat.assert_called_once()
        self.assertEqual(fake.lock_seat.call_args.args[0], "fresh-21")
        self.assertEqual(sent, [self.clock.origin.replace(hour=20, minute=0, second=6)])
        self.assertEqual(result["confirmed_booking"]["id"], "123")

    def test_hold_rejection_still_attempts_normal_booking_once(self):
        fake = WarmBooker()
        fake.lock_seat = Mock(return_value={
            "CODE": "ParamError", "DATA": {"result": "fail", "msg": "暂不可预留"}
        })
        result = self.scheduled(fake, hold_before_minutes=1)
        fake.lock_seat.assert_called_once()
        self.assertEqual(fake.book_calls, ["fresh-21"])
        self.assertIsNotNone(result["confirmed_booking"])

    def test_missing_primary_uses_first_surviving_fallback(self):
        fake = WarmBooker(("23", "24"))
        result = self.scheduled(fake, fallback_seats="22,23,24")
        self.assertEqual(fake.book_calls, ["fresh-23"])
        self.assertEqual([c["seat_num"] for c in result["seat_candidates"]], ["23", "24"])
        self.assertEqual(result["booked_seat_num"], "23")
        self.assertTrue(result["used_fallback"])
        self.assertEqual(fake.last_template["payload"]["seats[0]"], "fresh-23")

    def test_no_surviving_candidates_stops_without_post(self):
        fake = WarmBooker(())
        with self.assertRaisesRegex(RuntimeError, "没有可定位"):
            self.scheduled(fake, fallback_seats="22")
        self.assertEqual(fake.book_calls, [])

    def test_auth_and_captcha_still_stop_submission(self):
        def captcha(*args, **kwargs):
            fake.last_seat_query_meta = {"requires_image_code": 1}
            return [{"roomName": "四楼", "warm": True}]
        for failure in (errors.RequestFailure("登录失效", retryable=False), RuntimeError("响应结构错误"), None):
            with self.subTest(failure=failure):
                self.clock.elapsed = 0
                fake = WarmBooker()
                fake._query_seat_map_once = Mock(side_effect=failure) if failure else captcha
                with self.assertRaises(RuntimeError):
                    self.scheduled(fake)
                self.assertEqual(fake.book_calls, [])

    def test_transient_warmup_error_keeps_original_request(self):
        fake = WarmBooker()
        fake._query_seat_map_once = Mock(side_effect=errors.RequestFailure("timeout", retryable=True))
        self.scheduled(fake)
        self.assertEqual(fake.book_calls, ["old-21"])

    def test_backward_clock_step_does_not_send_early(self):
        self.adjust_clock(-2)
        fake = WarmBooker()
        sent = []
        fake.on_submit = lambda _: sent.append(self.clock.now())
        self.scheduled(fake)
        target = self.clock.origin.replace(hour=20, minute=0, second=6)
        self.assertGreaterEqual(sent[0], target)
        self.assertAlmostEqual(self.clock.elapsed, 9, places=4)

    def test_forward_clock_step_tracks_target(self):
        self.adjust_clock(2)
        fake = WarmBooker()
        sent = []
        fake.on_submit = lambda _: sent.append(self.clock.now())
        self.scheduled(fake)
        self.assertEqual(sent[0], self.clock.origin.replace(hour=20, minute=0, second=6))

    def test_large_forward_step_triggers_late_guard(self):
        self.adjust_clock(20)
        fake = WarmBooker()
        with self.assertRaisesRegex(RuntimeError, "错过执行时间"):
            self.scheduled(fake)
        self.assertEqual(fake.book_calls, [])

    def test_clock_step_after_main_wait_is_rechecked(self):
        fake = WarmBooker()
        real_wait = scheduling.wait_until
        calls = []
        def wait(*args, **kwargs):
            calls.append(True)
            result = real_wait(*args, **kwargs)
            if len(calls) == 1:
                self.clock.origin -= timedelta(seconds=2)
            return result
        sent = []
        fake.on_submit = lambda _: sent.append(self.clock.now())
        target = self.clock.origin.replace(hour=20, minute=0, second=6)
        with patch.object(scheduling, "wait_until", side_effect=wait):
            self.scheduled(fake)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(sent[0], target)

    def test_last_time_check_runs_after_cooldown_and_before_request(self):
        booker = self.booker()
        client.booking_gate_for(booker.uid).next_allowed_at = 3
        events = []
        def check():
            events.append(("check", self.clock.elapsed))
            raise RuntimeError("已经错过时间")
        with patch.object(booker, "request") as request, self.assertRaisesRegex(RuntimeError, "错过时间"):
            booker.book("21", self.begin, 1, before_submit=check)
        request.assert_not_called()
        self.assertAlmostEqual(events[0][1], 3)


class SeatActionAndListTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def booker(self):
        b = client.InstantBooker({"urls": {}, "session": {"headers": {}, "verify": True}})
        self.addCleanup(b.session.close)
        return b

    def test_empty_list_requires_recognized_structure(self):
        b = self.booker()
        valid = ([], {"CODE": "ok", "DATA": []}, {"content": {"children": [
            {"ui_type": "ht.Seat.OrderList", "children": []}]}})
        for response in valid:
            with self.subTest(response=response), patch.object(b, "request", return_value=response):
                self.assertEqual(b.current_bookings(), [])
        invalid = ({}, {"CODE": "ok"}, {"CODE": "ok", "DATA": {"unexpected": True}},
                   {"content": {"children": []}}, ["unexpected"], {"CODE": "notLogin", "DATA": []})
        for response in invalid:
            with self.subTest(response=response), patch.object(b, "request", return_value=response), \
                 self.assertRaises(errors.RequestFailure):
                b.current_bookings()

    def test_recognized_booking_item_is_still_parsed(self):
        b = self.booker()
        response = {"content": {"children": [{"ui_type": "ht.Seat.OrderListItem", "id": "123", "status": 0}]}}
        with patch.object(b, "request", return_value=response):
            self.assertEqual(b.current_bookings()[0]["status"], "0")

    def test_cancel_acknowledgement_needs_cancelled_state(self):
        b = self.booker()
        with patch.object(b, "request", return_value={"CODE": "ok", "DATA": {"result": "success"}}) as send, \
             patch.object(b, "current_bookings", side_effect=[
                 [{"id": "123", "status": "0"}], [{"id": "123", "status": "4"}]]):
            result = b.cancel_booking("123", check_limit=False)
        self.assertEqual(result["confirmed_booking"]["status"], "4")
        send.assert_called_once()

    def test_cancel_unknown_outcome_can_confirm_without_second_post(self):
        b = self.booker()
        for reply in ({}, errors.RequestFailure("timeout", outcome_unknown=True)):
            with self.subTest(reply=reply), patch.object(b, "request", side_effect=[reply]) as send, \
                 patch.object(b, "current_bookings", return_value=[{"id": "123", "status": "4"}]):
                self.assertEqual(b.cancel_booking("123", check_limit=False)["confirmed_booking"]["status"], "4")
                send.assert_called_once()

    def test_cancel_missing_record_or_unparsed_list_is_uncertain(self):
        b = self.booker()
        for rows in ([], [{"id": "123", "status": "0"}], errors.RequestFailure("unparsed list")):
            with self.subTest(rows=rows), patch.object(b, "request", return_value={"CODE": "ok", "DATA": {"result": "success"}}) as send, \
                 patch.object(b, "current_bookings", side_effect=[rows, rows, rows]), self.assertRaises(errors.ResultUncertain):
                b.cancel_booking("123", check_limit=False)
            send.assert_called_once()

    def test_explicit_cancel_rejection_is_never_swallowed(self):
        b = self.booker()
        with patch.object(b, "request", return_value={"CODE": "error", "MESSAGE": "cancel rejected"}), \
             patch.object(b, "current_bookings", return_value=[]) as lookup:
            with self.assertRaisesRegex(RuntimeError, "cancel rejected") as caught:
                b.cancel_booking("123", check_limit=False)
            self.assertNotIsInstance(caught.exception, errors.ResultUncertain)
        lookup.assert_not_called()

    def test_continue_timeout_and_incomplete_response_are_confirmed(self):
        for reply in (errors.RequestFailure("timeout", outcome_unknown=True), {}):
            with self.subTest(reply=reply):
                fake = Mock()
                fake.current_bookings.side_effect = [[{"id": "123", "status": "2"}], [{"id": "123", "status": "1"}]]
                fake.continue_booking.side_effect = [reply]
                with patch.object(operations, "create_booker", return_value=fake):
                    result = operations.continue_seat_by_id(booking_id="123", logger=lambda _: None)
                self.assertEqual(result["status_code_after"], "1")
                fake.continue_booking.assert_called_once_with("123")

    def test_continue_timeout_unconfirmed_is_uncertain(self):
        fake = Mock()
        fake.current_bookings.return_value = [{"id": "123", "status": "2"}]
        fake.continue_booking.side_effect = errors.RequestFailure("timeout", outcome_unknown=True)
        with patch.object(operations, "create_booker", return_value=fake), self.assertRaises(errors.ResultUncertain):
            operations.continue_seat_by_id(booking_id="123", logger=lambda _: None)
        fake.continue_booking.assert_called_once()

    def test_known_unsent_error_does_not_confirm_or_retry(self):
        b = self.booker()
        with patch.object(b, "request", side_effect=errors.RequestFailure("connect timeout", outcome_unknown=False)) as send, \
             patch.object(b, "current_bookings") as lookup, self.assertRaises(errors.RequestFailure):
            b.cancel_booking("123", check_limit=False)
        send.assert_called_once()
        lookup.assert_not_called()

    def test_web_exposes_missing_job_and_uncertain_outcome(self):
        handler = object.__new__(web_server.WebHandler)
        handler.send_json = Mock()
        with patch.dict(web_jobs.JOBS, {}, clear=True):
            handler.handle_json(lambda: web_jobs.job_snapshot("missing"))
        data, = handler.send_json.call_args.args
        self.assertEqual(data["code"], "job_not_found")
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 404)
        handler.handle_json(Mock(side_effect=errors.ResultUncertain("待确认")))
        data, = handler.send_json.call_args.args
        self.assertEqual(data["code"], "outcome_uncertain")
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 409)


if __name__ == "__main__":
    unittest.main()
