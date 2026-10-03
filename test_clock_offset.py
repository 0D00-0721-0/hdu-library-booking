"""Offline checks for server clock bounds; no library requests are sent."""

import time
import unittest
from datetime import datetime
from unittest.mock import patch

from libcs import client as library_client
from libcs import operations, scheduling
from libcs.web import jobs as web_jobs
from libcs.web import queries as web_queries


class ClockOffsetTests(unittest.TestCase):
    def test_integer_second_does_not_claim_millisecond_accuracy(self):
        bounds = scheduling.clock_offset_bounds([{
            "server_time": 1000,
            "sent_at": 1000.124,
            "received_at": 1000.519,
        }])
        self.assertAlmostEqual(bounds["lower"], -0.519)
        self.assertAlmostEqual(bounds["upper"], 0.876)
        self.assertGreater(bounds["uncertainty"], 1.0)

    def test_samples_around_rollover_narrow_the_range(self):
        bounds = scheduling.clock_offset_bounds([
            {"server_time": 1000, "sent_at": 1001.20, "received_at": 1001.25},
            {"server_time": 1001, "sent_at": 1001.25, "received_at": 1001.30},
        ])
        self.assertAlmostEqual(bounds["lower"], -0.30)
        self.assertAlmostEqual(bounds["upper"], -0.20)
        self.assertLess(bounds["uncertainty"], 0.11)

    def test_fractional_timestamp_retains_its_resolution(self):
        bounds = scheduling.clock_offset_bounds([{
            "server_time": "1000.123",
            "sent_at": 1000.10,
            "received_at": 1000.15,
        }])
        self.assertAlmostEqual(bounds["lower"], -0.027)
        self.assertAlmostEqual(bounds["upper"], 0.024)

    def test_inconsistent_and_missing_samples_do_not_produce_false_offset(self):
        self.assertIsNone(scheduling.clock_offset_bounds([{}]))
        bounds = scheduling.clock_offset_bounds([
            {"server_time": 1000, "sent_at": 1000, "received_at": 1000.1},
            {"server_time": 1004, "sent_at": 1001, "received_at": 1001.1},
        ])
        self.assertFalse(bounds["consistent"])

    def test_seat_map_records_local_send_and_receive_times(self):
        config = {"urls": {"query_seats": "https://example.test/seats"},
                  "session": {"headers": {}, "verify": True}}
        client = library_client.InstantBooker(config)
        detail = {"space_category": {"category_id": 1, "content_id": 2}}
        response = {"nowTime": 1000, "allContent": {"children": [
            {}, {}, {"children": {"children": []}},
        ]}}
        with patch.object(client, "request", return_value=response), patch.object(
            time, "time", side_effect=[1000.124, 1000.519]
        ):
            client._query_seat_map_once(detail, datetime.now().astimezone(), 1)
        self.assertEqual(client.last_seat_query_meta["clock_sample"], {
            "server_time": 1000, "sent_at": 1000.124, "received_at": 1000.519,
        })

    def test_server_opening_recommendation_uses_slowest_possible_clock(self):
        recommendation = scheduling.recommend_booking_execute_time({
            "consistent": True, "lower": -0.159, "upper": 0.509,
            "uncertainty": 0.668,
        })
        self.assertEqual(recommendation["execute_at"], "20:00:00.159")
        self.assertIn("无法判断", scheduling.clock_offset_message({
            "consistent": True, "lower": -0.159, "upper": 0.509,
            "uncertainty": 0.668, "samples": 4,
        }))

    def test_recommendation_has_no_arbitrary_margin(self):
        self.assertEqual(scheduling.recommend_booking_execute_time({
            "consistent": True, "lower": -0.159, "upper": -0.050,
            "uncertainty": 0.109,
        })["execute_at"], "20:00:00.159")
        self.assertEqual(scheduling.recommend_booking_execute_time({
            "consistent": True, "lower": 0.05, "upper": 0.15,
            "uncertainty": 0.1,
        })["execute_at"], "20:00:00.000")
        self.assertEqual(scheduling.recommend_booking_execute_time({
            "consistent": True, "lower": -0.560, "upper": 0.189,
            "uncertainty": 0.749,
        })["execute_at"], "20:00:00.560")
        self.assertEqual(scheduling.recommend_booking_execute_time({
            "consistent": True, "lower": -0.5604, "upper": 0.189,
            "uncertainty": 0.7494,
        })["execute_at"], "20:00:00.561")

    def test_imminent_booking_skips_manual_measurement(self):
        job = {"job_type": "booking", "execute_timestamp": 1010}
        with patch.object(web_jobs, "running_job_locked", return_value=job), patch.object(
            time, "time", return_value=1000
        ), patch.object(operations, "measure_server_clock") as measure:
            with self.assertRaisesRegex(ValueError, "不足 20 秒"):
                web_queries.clock_offset_from_config("unused.yaml")
            measure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
