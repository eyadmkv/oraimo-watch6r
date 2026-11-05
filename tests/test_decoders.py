import unittest

from oraimo_watch6r.decoders import (
    decode_active_watch_face,
    decode_compact_step_count,
    decode_heart_rate_history,
    decode_heart_rate_measurement,
    decode_historical_activity,
    decode_hourly_activity,
    decode_language,
    decode_live_activity,
    decode_setting,
    decode_watch_face_layout,
    decode_workout_summaries,
)
from oraimo_watch6r.exceptions import ProtocolError
from oraimo_watch6r.models import RawSetting


class DecoderTests(unittest.TestCase):
    def test_active_watch_face(self):
        self.assertEqual(decode_active_watch_face(b"\x07"), 7)

    def test_watch_face_layout_preserves_trailing_bytes(self):
        raw = bytes.fromhex(
            "00 00 01 FF FF "
            "0A 02 06 00 0A 02 02 0B 00 02 02 09 00 "
            "0C 08 0C 0F 0B 09 04 08 0F 00 04 03 0F 0C 03 0B 0A 0C 09 00 "
            "F0 00 8C 00 F0 00 8C FF"
        )
        layout = decode_watch_face_layout(raw)
        self.assertEqual(layout.time_position, 0)
        self.assertEqual(layout.text_color_rgb565, 0xFFFF)
        self.assertEqual(len(layout.background_md5_nibbles), 32)
        self.assertEqual(layout.trailing_bytes, bytes.fromhex("00 F0 00 8C 00 F0 00 8C FF"))

    def test_language_includes_arabic(self):
        language = decode_language(bytes.fromhex("00 1E 00 11 F3"))
        self.assertEqual(language["current"], "english")
        self.assertIn("arabic", language["supported"])

    def test_historical_activity_real_capture_steps_first(self):
        # Real captures from readonly_probe.py. The watch showed 6,801 steps for
        # yesterday, so the first u24 is steps and the second is distance (metres).
        yesterday = decode_historical_activity(
            bytes.fromhex("01 91 1A 00 64 13 00 29 01 00 22 00 00")
        )
        self.assertEqual(yesterday.day_index, 1)
        self.assertEqual(yesterday.steps, 6801)
        self.assertEqual(yesterday.distance_m, 4964)
        self.assertEqual(yesterday.calories_kcal, 297)
        self.assertEqual(yesterday.trailing_bytes, bytes.fromhex("22 00 00"))

        day_before = decode_historical_activity(
            bytes.fromhex("02 C2 14 00 27 0F 00 E8 00 00 1E 00 00")
        )
        self.assertEqual(day_before.day_index, 2)
        self.assertEqual(day_before.steps, 5314)
        self.assertEqual(day_before.distance_m, 3879)
        self.assertEqual(day_before.calories_kcal, 232)
        self.assertEqual(day_before.trailing_bytes, bytes.fromhex("1E 00 00"))

    def test_heart_rate_history_statistics(self):
        history = decode_heart_rate_history(bytes([2, 60, 0, 80, 100]))
        self.assertEqual(history.days_ago, 0)
        self.assertEqual(history.start_hour, 12)
        self.assertEqual(history.valid_samples, (60, 80, 100))
        self.assertEqual(history.minimum_bpm, 60)
        self.assertEqual(history.maximum_bpm, 100)
        self.assertEqual(history.average_bpm, 80)

    def test_compact_step_count_event(self):
        self.assertEqual(decode_compact_step_count(bytes.fromhex("01 04 1A 00")).steps, 6660)

    def test_activity_characteristic_read_capture(self):
        activity = decode_live_activity(bytes.fromhex("59 06 00 A2 04 00 47 00 00 06 00 00"))
        self.assertEqual(activity.steps, 1625)
        self.assertEqual(activity.distance_m, 1186)
        self.assertEqual(activity.calories_kcal, 71)
        self.assertEqual(activity.trailing_bytes, bytes.fromhex("06 00 00"))

    def test_hr_measurement_standard_characteristic(self):
        measurement = decode_heart_rate_measurement(bytes.fromhex("06 4C"))
        self.assertEqual(measurement.bpm, 76)
        self.assertTrue(measurement.sensor_contact_supported)
        self.assertTrue(measurement.sensor_contact_detected)

    def test_hourly_activity(self):
        raw = bytes([0]) + bytes(46) + bytes.fromhex("54 02")
        hourly = decode_hourly_activity(raw)
        self.assertEqual(hourly.day_index, 0)
        self.assertEqual(len(hourly.steps_by_bucket), 24)
        self.assertEqual(hourly.steps_by_bucket[-1], 596)

    def test_unknown_power_setting_is_raw(self):
        result = decode_setting(164, b"\x00\x01")
        self.assertIsInstance(result, RawSetting)
        self.assertEqual(result.payload, b"\x00\x01")

    def test_malformed_hr_history(self):
        with self.assertRaises(ProtocolError):
            decode_heart_rate_history(b"")

    def test_empty_workout_summaries(self):
        self.assertEqual(decode_workout_summaries(b""), ())

    def test_move_reminder_real_capture(self):
        # Real capture. Layout is inferred (60 min, 50 steps, 10:00-22:00) and
        # still needs confirming against the watch's settings screen.
        raw = bytes.fromhex("3C 32 0A 00 16 00")
        result = decode_setting(131, raw)
        self.assertEqual(
            (
                result.period,
                result.steps,
                result.start_hour,
                result.start_minute,
                result.end_hour,
                result.end_minute,
            ),
            (60, 50, 10, 0, 22, 0),
        )
        self.assertEqual(result.trailing_bytes, b"")
        self.assertEqual(result.raw, raw)

    def test_short_move_reminder_stays_raw(self):
        result = decode_setting(131, bytes.fromhex("3C 32 0A 00"))
        self.assertIsInstance(result, RawSetting)
        self.assertEqual(result.payload, bytes.fromhex("3C 32 0A 00"))

    def test_step_goal_real_capture(self):
        self.assertEqual(decode_setting(38, bytes.fromhex("40 1F 00 00")), 8000)

    def test_heart_rate_history_real_block_with_partial_day(self):
        # Real block 0: six readings, then no data for the rest of the window.
        raw = bytes.fromhex("00 5F 5B 61 5E 6D 6A") + bytes(66)
        history = decode_heart_rate_history(raw)
        self.assertEqual(len(history.samples_bpm), 72)
        self.assertEqual(history.valid_samples, (95, 91, 97, 94, 109, 106))
        self.assertEqual((history.days_ago, history.start_hour), (0, 0))


if __name__ == "__main__":
    unittest.main()
