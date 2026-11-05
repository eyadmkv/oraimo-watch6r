import asyncio
import unittest

from oraimo_watch6r.client import FEE2, FEE3, READ_ONLY_COMMANDS, WatchClient
from oraimo_watch6r.exceptions import UnsupportedCommandError, WatchNotConnectedError
from oraimo_watch6r.protocol import build_packet, parse_packet
from oraimo_watch6r.models import WatchDevice


class FakeBleakClient:
    def __init__(self, address, timeout=20):
        self.address = address
        self.timeout = timeout
        self.is_connected = False
        self.callbacks = {}
        self.read_calls = []

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, uuid, callback):
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        self.callbacks.pop(uuid, None)

    async def write_gatt_char(self, uuid, data, response=False):
        if uuid != FEE2:
            return
        request = parse_packet(bytes(data))
        if request.command == 38:
            payload = bytes.fromhex("40 1F 00 00")
        elif request.command == 53:
            payload = bytes((request.payload[0], 70, 0, 80))
        else:
            payload = b""
        response_packet = build_packet(request.command, payload)
        callback = self.callbacks[FEE3]
        if request.command == 38:
            combined = build_packet(0xF9, b"\x02\x00") + response_packet
            callback(FEE3, bytearray(combined[:3]))
            callback(FEE3, bytearray(combined[3:]))
        else:
            callback(FEE3, bytearray(response_packet))

    async def read_gatt_char(self, uuid):
        self.read_calls.append(uuid)
        values = {
            "00002a29-0000-1000-8000-00805f9b34fb": b"MOYOUNG-V2",
            "00002a26-0000-1000-8000-00805f9b34fb": b"JLQFNLKV3.5",
            "00002a28-0000-1000-8000-00805f9b34fb": b"823-8AW4-1.3.0",
            "00002a19-0000-1000-8000-00805f9b34fb": b"\x43",
            "0000fee1-0000-1000-8000-00805f9b34fb": bytes.fromhex("04 1A 00 04 13 00 20 01 00 22 00 00"),
        }
        return values[uuid]


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_and_activity_read_do_not_require_fee3(self):
        class NoFee3Client(FakeBleakClient):
            async def start_notify(self, uuid, callback):
                if uuid == FEE3:
                    raise RuntimeError(f"Characteristic {uuid} was not found")
                await super().start_notify(uuid, callback)

        watch = WatchClient("fake-address", client_factory=NoFee3Client)
        await watch.connect()
        activity = await watch.read_live_activity()
        self.assertEqual(activity.steps, 6660)
        self.assertFalse(watch._notify_started)

        with self.assertRaisesRegex(RuntimeError, "Characteristic .* was not found"):
            await watch.get_step_goal()
        await watch.disconnect()

    async def test_query_preserves_unrelated_packets(self):
        watch = WatchClient("fake-address", client_factory=FakeBleakClient)
        await watch.connect(verify=False)
        result = await watch.query(38)
        self.assertEqual(result, bytes.fromhex("40 1F 00 00"))
        event = await watch.next_protocol_event(timeout=0.1)
        self.assertEqual(event.command, 0xF9)
        await watch.disconnect()

    async def test_scanned_device_object_is_used_for_connection(self):
        native_device = object()
        device = WatchDevice(
            name="oraimo Watch 6R_test",
            address="9A:3D:EB:25:59:47",
            rssi=-28,
            _ble_device=native_device,
        )
        watch = WatchClient(device, client_factory=FakeBleakClient)
        await watch.connect(verify=False)
        self.assertIs(watch._client.address, native_device)
        await watch.disconnect()

    async def test_convenience_step_goal(self):
        async with WatchClient("fake-address", client_factory=FakeBleakClient) as watch:
            self.assertEqual(await watch.get_step_goal(), 8000)

    async def test_read_live_activity_uses_existing_gatt_client(self):
        watch = WatchClient("fake-address", client_factory=FakeBleakClient)
        await watch.connect(verify=False)
        client = watch._client
        activity = await watch.read_live_activity()
        self.assertIs(watch._client, client)
        self.assertEqual(activity.steps, 6660)
        self.assertEqual(activity.distance_m, 4868)
        self.assertEqual(activity.calories_kcal, 288)
        await watch.disconnect()

    async def test_live_activity_read_waits_for_protocol_query_lock(self):
        watch = WatchClient("fake-address", client_factory=FakeBleakClient)
        await watch.connect(verify=False)
        client = watch._client
        await watch._query_lock.acquire()
        try:
            read = asyncio.create_task(watch.read_live_activity())
            await asyncio.sleep(0)
            self.assertNotIn("0000fee1-0000-1000-8000-00805f9b34fb", client.read_calls)
        finally:
            watch._query_lock.release()
        await read
        self.assertIn("0000fee1-0000-1000-8000-00805f9b34fb", client.read_calls)
        await watch.disconnect()

    async def test_live_activity_read_rejects_disconnected_watch(self):
        watch = WatchClient("fake-address", client_factory=FakeBleakClient)
        with self.assertRaises(WatchNotConnectedError) as context:
            await watch.read_live_activity()
        self.assertIn("connect to the watch", str(context.exception))

    async def test_non_read_only_command_is_rejected(self):
        watch = WatchClient("fake-address", client_factory=FakeBleakClient)
        await watch.connect(verify=False)
        with self.assertRaises(UnsupportedCommandError):
            await watch.query(25, b"\x07")
        await watch.disconnect()

    def test_one_minute_history_is_not_in_api_allowlist(self):
        self.assertNotIn(54, READ_ONLY_COMMANDS)


if __name__ == "__main__":
    unittest.main()
