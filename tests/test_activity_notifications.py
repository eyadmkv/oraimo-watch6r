import asyncio
import unittest

from oraimo_watch6r.client import FEE1, FEA1, WatchClient
from oraimo_watch6r.decoders import decode_compact_step_count, decode_live_activity
from oraimo_watch6r.exceptions import ProtocolError
from oraimo_watch6r.models import StepCount

FEA1_SAMPLE = bytes.fromhex("01 04 1A 00")
FEE1_SAMPLE = bytes.fromhex("CB 19 00 D4 12 00 21 01 00 22 00 00")


class ActivityDecoderTests(unittest.TestCase):
    def test_fea1_compact_step_count(self):
        self.assertEqual(decode_compact_step_count(FEA1_SAMPLE), StepCount(steps=6660))

    def test_fee1_activity_fields(self):
        activity = decode_live_activity(FEE1_SAMPLE)
        self.assertEqual(activity.steps, 6603)
        self.assertEqual(activity.distance_m, 4820)
        self.assertEqual(activity.calories_kcal, 289)

    def test_fee1_fields_are_little_endian(self):
        activity = decode_live_activity(bytes.fromhex("0A 00 00 1C 00 00 05 00 00"))
        self.assertEqual(activity.steps, 10)
        self.assertEqual(activity.distance_m, 28)
        self.assertEqual(activity.calories_kcal, 5)

    def test_fee1_preserves_unknown_trailing_bytes(self):
        extended = FEE1_SAMPLE + bytes.fromhex("FF 7E")
        activity = decode_live_activity(extended)
        self.assertEqual(activity.trailing_bytes, bytes.fromhex("22 00 00 FF 7E"))
        self.assertEqual(activity.raw, extended)

    def test_truncated_fee1_notification_is_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_live_activity(FEE1_SAMPLE[:8])

    def test_invalid_fea1_notifications_are_rejected(self):
        malformed = (
            bytes.fromhex("01 04 1A"),          # too short
            bytes.fromhex("01 04 1A 00 00"),    # too long
            bytes.fromhex("02 04 1A 00"),       # unexpected event marker
        )
        for payload in malformed:
            with self.assertRaises(ProtocolError):
                decode_compact_step_count(payload)


class FakeBleakClient:
    def __init__(self, address, timeout=20.0):
        self.address = address
        self.timeout = timeout
        self.is_connected = False
        self.callbacks = {}
        self.start_notify_counts = {}

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, uuid, callback):
        self.callbacks[uuid] = callback
        self.start_notify_counts[uuid] = self.start_notify_counts.get(uuid, 0) + 1

    async def stop_notify(self, uuid):
        self.callbacks.pop(uuid, None)

    async def read_gatt_char(self, uuid):
        return b"\x00"

    def notify(self, uuid, data):
        self.callbacks[uuid](uuid, bytearray(data))


class ActivityNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeBleakClient("fake-address")
        self.watch = WatchClient(
            "fake-address",
            client_factory=lambda address, timeout=20.0: self.fake,
        )
        await self.watch.connect(verify=False)

    async def asyncTearDown(self):
        await self.watch.disconnect()

    async def _next(self, stream):
        return await asyncio.wait_for(anext(stream), timeout=0.2)

    async def test_subscriptions_are_independent_and_not_duplicated(self):
        await self.watch.start_step_notifications()
        await self.watch.start_activity_notifications()
        await self.watch.start_step_notifications()
        await self.watch.start_activity_notifications()
        self.assertEqual(self.fake.start_notify_counts.get(FEA1), 1)
        self.assertEqual(self.fake.start_notify_counts.get(FEE1), 1)
        self.assertIn(FEA1, self.fake.callbacks)
        self.assertIn(FEE1, self.fake.callbacks)

    async def test_step_notification_streams_as_step_count(self):
        await self.watch.start_step_notifications()
        self.fake.notify(FEA1, FEA1_SAMPLE)
        self.assertEqual(await self._next(self.watch.step_count_stream()), StepCount(steps=6660))

    async def test_activity_notification_streams_live_activity(self):
        await self.watch.start_activity_notifications()
        self.fake.notify(FEE1, FEE1_SAMPLE)
        activity = await self._next(self.watch.activity_stream())
        self.assertEqual(activity.steps, 6603)
        self.assertEqual(activity.distance_m, 4820)
        self.assertEqual(activity.calories_kcal, 289)
        self.assertEqual(activity.trailing_bytes, bytes.fromhex("22 00 00"))

    async def test_malformed_notifications_are_dropped(self):
        await self.watch.start_step_notifications()
        await self.watch.start_activity_notifications()
        self.fake.notify(FEE1, bytes.fromhex("D4 12 00"))
        self.fake.notify(FEA1, bytes.fromhex("02 04 1A 00"))
        with self.assertRaises(asyncio.TimeoutError):
            await self._next(self.watch.activity_stream())
        with self.assertRaises(asyncio.TimeoutError):
            await self._next(self.watch.step_count_stream())

    async def test_disconnect_stops_notifications(self):
        await self.watch.start_step_notifications()
        await self.watch.start_activity_notifications()
        await self.watch.disconnect()
        self.assertNotIn(FEA1, self.fake.callbacks)
        self.assertNotIn(FEE1, self.fake.callbacks)
        self.assertFalse(self.fake.is_connected)

    async def test_activity_raw_hook_sees_bytes_before_decoding(self):
        seen = []
        await self.watch.start_activity_notifications(on_raw=seen.append)
        self.fake.notify(FEE1, FEE1_SAMPLE)
        self.assertEqual(seen, [FEE1_SAMPLE])
        self.assertEqual((await self._next(self.watch.activity_stream())).steps, 6603)

    async def test_step_raw_hook_sees_every_payload(self):
        seen = []
        malformed = bytes.fromhex("02 04 1A 00")
        await self.watch.start_step_notifications(on_raw=seen.append)
        self.fake.notify(FEA1, FEA1_SAMPLE)
        self.fake.notify(FEA1, malformed)
        self.assertEqual(seen, [FEA1_SAMPLE, malformed])
        self.assertEqual(await self._next(self.watch.step_count_stream()), StepCount(steps=6660))

    async def test_decode_error_hook_reports_malformed_activity_payload(self):
        reported = []
        await self.watch.start_activity_notifications(
            on_decode_error=lambda payload, exc: reported.append((payload, exc)),
        )
        truncated = bytes.fromhex("D4 12 00")
        self.fake.notify(FEE1, truncated)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0][0], truncated)
        self.assertIsInstance(reported[0][1], ProtocolError)
        with self.assertRaises(asyncio.TimeoutError):
            await self._next(self.watch.activity_stream())

    async def test_decode_error_hook_reports_malformed_step_payload(self):
        reported = []
        await self.watch.start_step_notifications(
            on_decode_error=lambda payload, exc: reported.append((payload, exc)),
        )
        malformed = bytes.fromhex("02 04 1A 00")
        self.fake.notify(FEA1, malformed)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0][0], malformed)
        self.assertIsInstance(reported[0][1], ProtocolError)
        with self.assertRaises(asyncio.TimeoutError):
            await self._next(self.watch.step_count_stream())

    async def test_hooks_survive_deduplicated_restart_call(self):
        seen = []
        await self.watch.start_step_notifications(on_raw=seen.append)
        await self.watch.start_step_notifications()
        # The stream's internal start call (no hooks) must not clear the hooks.
        consumer = asyncio.create_task(anext(self.watch.step_count_stream()))
        await asyncio.sleep(0)
        consumer.cancel()
        self.fake.notify(FEA1, FEA1_SAMPLE)
        self.assertEqual(seen, [FEA1_SAMPLE])
        self.assertEqual(self.fake.start_notify_counts.get(FEA1), 1)


if __name__ == "__main__":
    unittest.main()
