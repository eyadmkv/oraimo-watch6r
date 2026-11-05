"""Hardware-independent tests for the local bridge: stores, transforms, HTTP API and manager."""

import asyncio
import http.client
import json
import threading
import unittest
import urllib.parse

from oraimo_watch6r.decoders import (
    decode_compact_step_count,
    decode_live_activity,
)
from oraimo_watch6r.models import (
    BatteryLevel,
    DeviceInfo,
    HeartRateHistory,
    HistoricalActivity,
    HourlyActivity,
    LiveActivity,
    WatchDevice,
)

from bridge.server import (
    BridgeServer,
    DEFAULT_EXTENSION_ORIGIN,
    load_extension_origin,
    load_or_create_token,
    normalize_extension_origin,
)
from bridge.store import (
    CommandStore,
    DiagnosticsStore,
    HistoryStore,
    SnapshotStore,
    live_activity_dict,
    device_info_dict,
    heart_rate_history_dict,
    historical_activity_dict,
    hourly_activity_dict,
    sleep_stages_list,
)
from bridge.watch_manager import WatchManager

TOKEN = "test-token-123"
FEA1_SAMPLE = bytes.fromhex("01 04 1A 00")
FEE1_SAMPLE = bytes.fromhex("CB 19 00 D4 12 00 21 01 00 22 00 00")


def make_stores():
    return SnapshotStore(), HistoryStore(), DiagnosticsStore()


class StoreTests(unittest.TestCase):
    def test_snapshot_starts_unavailable_not_zero(self):
        snapshot = SnapshotStore().snapshot()
        self.assertIsNone(snapshot["steps"])
        self.assertIsNone(snapshot["distance_m"])
        self.assertIsNone(snapshot["heart_rate_bpm"])
        self.assertIsNone(snapshot["battery_percent"])

    def test_step_count_updates_only_steps(self):
        store = SnapshotStore()
        store.apply_step_count(decode_compact_step_count(FEA1_SAMPLE).steps)
        data = store.snapshot()
        self.assertEqual(data["steps"], 6660)
        self.assertEqual(data["steps_source"], "FEA1")
        self.assertIsNone(data["distance_m"], "FEA1 carries no distance; it must stay unavailable")
        self.assertIsNotNone(data["last_notification_at"])

    def test_live_activity_updates_steps_distance_calories(self):
        store = SnapshotStore()
        store.apply_live_activity(decode_live_activity(FEE1_SAMPLE))
        data = store.snapshot()
        self.assertEqual(data["steps"], 6603)
        self.assertEqual(data["distance_m"], 4820)
        self.assertEqual(data["calories_kcal"], 289)
        self.assertEqual(data["steps_source"], "FEE1")

    def test_heart_rate_and_battery(self):
        store = SnapshotStore()
        store.apply_heart_rate(91)
        store.apply_battery(67)
        data = store.snapshot()
        self.assertEqual(data["heart_rate_bpm"], 91)
        self.assertEqual(data["battery_percent"], 67)
        self.assertIsNotNone(data["heart_rate_received_at"])

    def test_diagnostics_ring_buffer_and_counters(self):
        diag = DiagnosticsStore(max_events=10)
        for i in range(25):
            diag.record_notification("0000fea1-0000-1000-8000-00805f9b34fb", f"{i:02X}", status="raw")
        diag.record_notification("0000fee1-0000-1000-8000-00805f9b34fb", "D4 12 00", status="decode-error", error="too short")
        diag.record_decoded()
        diag.record_connection_event("connection-failed", "boom")
        data = diag.snapshot()
        self.assertEqual(len(data["notifications"]), 10)
        self.assertEqual(data["notifications"][0]["payload_hex"], "10")  # last 10 entries: loop 16..24 + decode-error
        self.assertEqual(data["counters"]["notifications_total"], 26)
        self.assertEqual(data["counters"]["rejected_total"], 1)
        self.assertEqual(data["counters"]["decoded_total"], 1)
        self.assertEqual(data["counters"]["connection_errors_total"], 1)
        self.assertEqual(data["connection_events"][-1]["event"], "connection-failed")


class TransformTests(unittest.TestCase):
    def test_live_activity_preserves_unknown_bytes_and_flags_provisional(self):
        activity = decode_live_activity(FEE1_SAMPLE)
        data = live_activity_dict(activity)
        self.assertEqual(data["steps"], 6603)
        self.assertEqual(data["distance_m"], 4820)
        self.assertEqual(data["calories_kcal"], 289)
        self.assertEqual(data["trailing_bytes_hex"], "22 00 00")
        self.assertEqual(data["raw_hex"], "CB 19 00 D4 12 00 21 01 00 22 00 00")
        self.assertTrue(data["provisional"])

    def test_historical_activity_roundtrip(self):
        raw = bytes.fromhex("01 91 1A 00 64 13 00 29 01 00 22 00 00")
        from oraimo_watch6r.decoders import decode_historical_activity

        data = historical_activity_dict(decode_historical_activity(raw))
        self.assertEqual(data["steps"], 6801)
        self.assertEqual(data["distance_m"], 4964)
        self.assertEqual(data["calories_kcal"], 297)
        self.assertEqual(data["trailing_bytes_hex"], "22 00 00")

    def test_hourly_activity_includes_buckets_and_trailing(self):
        raw = bytes([0]) + bytes(46) + bytes.fromhex("54 02")
        from oraimo_watch6r.decoders import decode_hourly_activity

        data = hourly_activity_dict(decode_hourly_activity(raw))
        self.assertEqual(data["day_index"], 0)
        self.assertEqual(len(data["steps_by_bucket"]), 24)
        self.assertEqual(data["steps_by_bucket"][-1], 596)
        self.assertIsNone(data["trailing_byte"])
        self.assertTrue(data["provisional"])

    def test_heart_rate_history_statistics(self):
        data = heart_rate_history_dict(HeartRateHistory(index=2, days_ago=0, start_hour=12, samples_bpm=(60, 0, 80, 100)))
        self.assertEqual(data["minimum_bpm"], 60)
        self.assertEqual(data["maximum_bpm"], 100)
        self.assertEqual(data["average_bpm"], 80)

    def test_device_info(self):
        info = DeviceInfo(manufacturer="MOYOUNG-V2", firmware="JLQFNLKV3.5", software="823-8AW4-1.3.0", battery_percent=67, connected_name="oraimo Watch 6R_5947")
        data = device_info_dict(info)
        self.assertEqual(data["manufacturer"], "MOYOUNG-V2")
        self.assertEqual(data["connected_name"], "oraimo Watch 6R_5947")

    def test_empty_sleep_is_none_not_empty_list(self):
        self.assertIsNone(sleep_stages_list(()))
        self.assertIsNone(sleep_stages_list(None))

    def test_sleep_stages_preserve_codes(self):
        from oraimo_watch6r.models import SleepStage

        stages = sleep_stages_list([SleepStage(stage_code=2, stage_name="restful", hour=23, minute=10)])
        self.assertEqual(stages, [{"stage_code": 2, "stage_name": "restful", "hour": 23, "minute": 10}])


class FakeManager:
    def __init__(self):
        self.reconnect_requested = False
        self.commands = CommandStore()

    def status(self):
        return {
            "state": "connected",
            "detail": None,
            "watch_name": "oraimo Watch 6R_5947",
            "watch_address": "9A:3D:EB:25:59:47",
            "last_error": None,
            "connected_since": None,
        }

    def request_reconnect(self):
        self.reconnect_requested = True

    def submit_fetch_steps(self):
        return self.commands.create("FETCH_STEPS")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        snapshot, history, diagnostics = make_stores()
        snapshot.apply_step_count(6660)
        snapshot.apply_heart_rate(91)
        history.set_device_info(DeviceInfo(manufacturer="MOYOUNG-V2", firmware="JLQFNLKV3.5", software="823-8AW4-1.3.0", battery_percent=67))
        history.set_past_activity(1, HistoricalActivity(day_index=1, distance_m=3879, steps=5314, calories_kcal=232, trailing_bytes=b"\x1e\x00\x00", raw=b""))
        diagnostics.record_notification("0000fea1-0000-1000-8000-00805f9b34fb", "01 04 1A 00", status="raw")
        cls.manager = FakeManager()
        cls.server = BridgeServer(
            ("127.0.0.1", 0),
            TOKEN,
            cls.manager,
            (snapshot, history, diagnostics),
            allowed_origin=DEFAULT_EXTENSION_ORIGIN,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, path, method="GET", headers=None, origin=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = dict(headers or {})
        if origin:
            request_headers["Origin"] = origin
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        parsed = json.loads(body) if body else None
        return response.status, dict(response.getheaders()), parsed

    def test_requires_token(self):
        status, _, body = self.request("/api/snapshot")
        self.assertEqual(status, 401)
        self.assertFalse(body["ok"])

    def test_rejects_wrong_token(self):
        status, _, _ = self.request("/api/snapshot", headers={"X-Oraimo-Token": "nope"})
        self.assertEqual(status, 401)

    def test_token_header_accepted(self):
        status, _, body = self.request("/api/snapshot", headers={"X-Oraimo-Token": TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["steps"], 6660)
        self.assertIsNone(body["data"]["distance_m"])
        self.assertIn("generated_at", body)

    def test_token_query_parameter_accepted(self):
        status, _, body = self.request(f"/api/snapshot?token={urllib.parse.quote(TOKEN)}")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["steps"], 6660)

    def test_status_endpoint_shape(self):
        status, _, body = self.request("/api/status", headers={"X-Oraimo-Token": TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["state"], "connected")
        self.assertEqual(body["watch_name"], "oraimo Watch 6R_5947")
        self.assertEqual(body["bridge_version"], "1.0.0")

    def test_history_endpoint_shape(self):
        status, _, body = self.request("/api/history", headers={"X-Oraimo-Token": TOKEN})
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual(data["device_info"]["manufacturer"], "MOYOUNG-V2")
        self.assertEqual(data["past_activity"]["1"]["steps"], 5314)
        self.assertIsNone(data["past_activity"]["2"])
        self.assertIsNone(data["sleep_today"], "no sleep data was collected; must not be fabricated")

    def test_diagnostics_endpoint_shape(self):
        status, _, body = self.request("/api/diagnostics", headers={"X-Oraimo-Token": TOKEN})
        self.assertEqual(status, 200)
        entry = body["data"]["notifications"][-1]
        self.assertEqual(entry["payload_hex"], "01 04 1A 00")

    def test_unknown_route_is_404(self):
        status, _, _ = self.request("/api/nope", headers={"X-Oraimo-Token": TOKEN})
        self.assertEqual(status, 404)

    def test_cors_reflects_extension_origin_exactly(self):
        origin = DEFAULT_EXTENSION_ORIGIN
        status, headers, _ = self.request("/api/snapshot", headers={"X-Oraimo-Token": TOKEN}, origin=origin)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), origin)
        self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), "*")

    def test_cors_denied_for_web_origin(self):
        status, headers, _ = self.request("/api/snapshot", headers={"X-Oraimo-Token": TOKEN}, origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_preflight_allowed_for_extension_origin(self):
        origin = DEFAULT_EXTENSION_ORIGIN
        status, headers, _ = self.request(
            "/api/reconnect",
            method="OPTIONS",
            origin=origin,
            headers={
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, authorization, x-oraimo-token",
            },
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), origin)
        self.assertEqual(headers.get("Access-Control-Allow-Methods"), "GET, POST, OPTIONS")
        self.assertIn("X-Oraimo-Token", headers.get("Access-Control-Allow-Headers", ""))
        self.assertIn("Content-Type", headers.get("Access-Control-Allow-Headers", ""))
        self.assertIn("Authorization", headers.get("Access-Control-Allow-Headers", ""))

    def test_preflight_denied_without_extension_origin(self):
        status, _, _ = self.request(
            "/api/reconnect",
            method="OPTIONS",
            origin="https://evil.example",
            headers={"Access-Control-Request-Method": "POST"},
        )
        self.assertEqual(status, 403)

    def test_preflight_denied_for_other_extension_id(self):
        status, _, _ = self.request(
            "/api/reconnect",
            method="OPTIONS",
            origin="chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            headers={"Access-Control-Request-Method": "POST"},
        )
        self.assertEqual(status, 403)

    def test_preflight_rejects_unapproved_method_or_header(self):
        origin = DEFAULT_EXTENSION_ORIGIN
        for requested, expected in (
            ({"Access-Control-Request-Method": "DELETE"}, 403),
            (
                {
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "x-unapproved-header",
                },
                403,
            ),
            ({"Access-Control-Request-Method": "GET"}, 204),
            ({"Access-Control-Request-Method": "OPTIONS"}, 204),
        ):
            status, _, _ = self.request(
                "/api/status",
                method="OPTIONS",
                origin=origin,
                headers=requested,
            )
            self.assertEqual(status, expected)

    def test_reconnect_requests_manager_restart(self):
        status, _, body = self.request("/api/reconnect", method="POST", headers={"X-Oraimo-Token": TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(self.manager.reconnect_requested)
        self.assertTrue(body["data"]["reconnect_requested"])

    def test_pinned_origin_locks_cors_down(self):
        snapshot, history, diagnostics = make_stores()
        pinned = BridgeServer(
            ("127.0.0.1", 0),
            TOKEN,
            FakeManager(),
            (snapshot, history, diagnostics),
            allowed_origin=DEFAULT_EXTENSION_ORIGIN,
        )
        thread = threading.Thread(target=pinned.serve_forever, daemon=True)
        thread.start()
        try:
            port = pinned.server_address[1]
            for origin, expected in (
                (DEFAULT_EXTENSION_ORIGIN, 204),
                (DEFAULT_EXTENSION_ORIGIN + "/", 403),
                ("chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 403),
            ):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(
                    "OPTIONS",
                    "/api/snapshot",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "GET",
                    },
                )
                response = conn.getresponse()
                response.read()
                conn.close()
                self.assertEqual(response.status, expected)
        finally:
            pinned.shutdown()
            pinned.server_close()

    def test_preflight_denied_until_extension_origin_is_configured(self):
        snapshot, history, diagnostics = make_stores()
        unconfigured = BridgeServer(
            ("127.0.0.1", 0),
            TOKEN,
            FakeManager(),
            (snapshot, history, diagnostics),
        )
        thread = threading.Thread(target=unconfigured.serve_forever, daemon=True)
        thread.start()
        try:
            port = unconfigured.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "OPTIONS",
                "/api/commands",
                headers={
                    "Origin": DEFAULT_EXTENSION_ORIGIN,
                    "Access-Control-Request-Method": "POST",
                },
            )
            response = conn.getresponse()
            response.read()
            conn.close()
            self.assertEqual(response.status, 403)
        finally:
            unconfigured.shutdown()
            unconfigured.server_close()

    def test_host_header_must_match_loopback_listener(self):
        status, _, body = self.request(
            "/api/status",
            headers={"X-Oraimo-Token": TOKEN, "Host": "localhost:8765"},
        )
        self.assertEqual(status, 403)
        self.assertIn("Host header", body["error"])

    def test_fetch_steps_command_and_telemetry_endpoints(self):
        status, _, response = self.request(
            "/api/commands",
            method="POST",
            headers={"X-Oraimo-Token": TOKEN, "Content-Type": "application/json"},
            body=json.dumps({"type": "FETCH_STEPS"}),
        )
        self.assertEqual(status, 202)
        command = response["data"]
        self.assertEqual(command["type"], "FETCH_STEPS")
        self.assertEqual(command["state"], "queued")

        status, _, body = self.request(
            f"/api/commands/{command['command_id']}",
            headers={"X-Oraimo-Token": TOKEN},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["state"], "queued")

        self.manager.commands.update(
            command["command_id"],
            "running",
        )
        result = {
            "steps": 1625,
            "distance_meters": 1186,
            "calories_kcal": 71,
            "measured_at": "2026-11-04T22:00:00+02:00",
            "source_characteristic": "0000fee1-0000-1000-8000-00805f9b34fb",
        }
        self.manager.commands.update(command["command_id"], "succeeded", result=result)
        status, _, body = self.request(
            f"/api/commands/{command['command_id']}",
            headers={"X-Oraimo-Token": TOKEN},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["state"], "succeeded")
        self.assertEqual(body["data"]["result"], result)

        status, _, body = self.request("/api/telemetry/latest", headers={"X-Oraimo-Token": TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["steps"], 1625)

    def test_fetch_steps_rejects_unknown_command_type(self):
        status, _, body = self.request(
            "/api/commands",
            method="POST",
            headers={"X-Oraimo-Token": TOKEN, "Content-Type": "application/json"},
            body=json.dumps({"type": "FETCH_HISTORY"}),
        )
        self.assertEqual(status, 400)
        self.assertIn("FETCH_STEPS", body["error"])


class FakeWatchClient:
    """Duck-typed WatchClient for manager tests; no BLE at all."""

    instances = []

    def __init__(self, device):
        self.device = device
        self.is_connected = False
        self.disconnected = False
        self.activity_hooks = {}
        self.active_reads = 0
        self.maximum_active_reads = 0
        FakeWatchClient.instances.append(self)

    async def connect(self, verify: bool = True) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False
        self.disconnected = True

    async def device_info(self):
        return DeviceInfo(manufacturer="MOYOUNG-V2", firmware="F1", software="S1", battery_percent=55, connected_name=self.device.name)

    async def read_battery(self):
        return BatteryLevel(percent=55)

    async def get_step_goal(self):
        return 8000

    async def get_past_activity(self, day_index):
        if day_index == 1:
            return HistoricalActivity(day_index=1, distance_m=1000, steps=500, calories_kcal=20, trailing_bytes=b"", raw=b"")
        return None

    async def get_hourly_activity(self, day_index=0):
        return HourlyActivity(day_index=0, steps_by_bucket=tuple(24 * [0]), trailing_byte=None, raw=b"")

    async def get_today_sleep(self):
        return None

    async def get_heart_rate_history(self, index):
        return None

    async def read_live_activity(self):
        self.active_reads += 1
        self.maximum_active_reads = max(self.maximum_active_reads, self.active_reads)
        try:
            await asyncio.sleep(0.02)
            return decode_live_activity(FEE1_SAMPLE)
        finally:
            self.active_reads -= 1

    async def start_activity_notifications(self, *, on_raw=None, on_decode_error=None):
        self.activity_hooks["fee1_raw"] = on_raw
        self.activity_hooks["fee1_error"] = on_decode_error

    async def start_step_notifications(self, *, on_raw=None, on_decode_error=None):
        self.activity_hooks["fea1_raw"] = on_raw
        self.activity_hooks["fea1_error"] = on_decode_error

    async def start_heart_rate_notifications(self):
        pass

    def activity_stream(self):
        return self._stream(LiveActivity(steps=6603, distance_m=4820, calories_kcal=289, trailing_bytes=b"\x22\x00\x00", raw=FEE1_SAMPLE))

    def step_count_stream(self):
        return self._stream(decode_compact_step_count(FEA1_SAMPLE))

    def heart_rate_stream(self):
        return self._idle_stream()

    async def _stream(self, value):
        yield value
        await asyncio.sleep(3600)  # stay "live" afterwards

    async def _idle_stream(self):
        while True:
            # An async generator that never yields: models a subscribed
            # characteristic that sends no notifications during the test.
            await asyncio.sleep(3600)
            yield None  # pragma: no cover - unreachable within test time


def fake_scan(devices):
    async def scan(*, timeout: float = 10.0, name_contains: str = ""):
        return devices
    return scan


DEVICE = WatchDevice(name="oraimo Watch 6R_5947", address="9A:3D:EB:25:59:47", rssi=-40)


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for(self, predicate):
        for _ in range(150):
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("manager did not reach expected state")

    async def test_collects_initial_data_and_stream_values(self):
        snapshot, history, diagnostics = make_stores()
        manager = WatchManager(snapshot, history, diagnostics, scan_fn=fake_scan([DEVICE]), client_class=FakeWatchClient)
        manager.start()
        for _ in range(100):
            data = snapshot.snapshot()
            hist = history.snapshot()
            if hist["device_info"] is not None and data["distance_m"] == 4820:
                break
            await asyncio.sleep(0.02)
        data = snapshot.snapshot()
        hist = history.snapshot()
        self.assertIn(data["steps"], (6603, 6660), "steps reflect the most recent notification from either source")
        self.assertEqual(data["distance_m"], 4820)
        self.assertEqual(data["calories_kcal"], 289)
        self.assertEqual(data["battery_percent"], 55)
        self.assertEqual(data["step_goal"], 8000)
        self.assertEqual(hist["device_info"]["manufacturer"], "MOYOUNG-V2")
        self.assertEqual(hist["past_activity"]["1"]["steps"], 500)
        self.assertIsNone(hist["past_activity"]["2"])
        self.assertIsNone(hist["sleep_today"])
        self.assertEqual(manager.state, "connected")
        self.assertEqual(manager.watch_name, "oraimo Watch 6R_5947")
        await manager.stop()
        self.assertEqual(manager.state, "stopped")

    async def test_fetch_steps_uses_connected_client_and_serializes_commands(self):
        snapshot, history, diagnostics = make_stores()
        manager = WatchManager(
            snapshot,
            history,
            diagnostics,
            scan_fn=fake_scan([DEVICE]),
            client_class=FakeWatchClient,
        )
        manager.start()
        await self._wait_for(lambda: manager.state == "connected")
        first = manager.submit_fetch_steps()
        second = manager.submit_fetch_steps()
        await self._wait_for(
            lambda: all(
                manager.commands.get(command["command_id"])["state"] in ("succeeded", "failed")
                for command in (first, second)
            )
        )

        first_result = manager.commands.get(first["command_id"])
        second_result = manager.commands.get(second["command_id"])
        self.assertEqual(first_result["state"], "succeeded")
        self.assertEqual(first_result["result"]["steps"], 6603)
        self.assertEqual(first_result["result"]["distance_meters"], 4820)
        self.assertEqual(first_result["result"]["calories_kcal"], 289)
        self.assertEqual(
            first_result["result"]["source_characteristic"],
            "0000fee1-0000-1000-8000-00805f9b34fb",
        )
        self.assertIsNotNone(first_result["result"]["measured_at"])
        self.assertEqual(second_result["state"], "succeeded")
        self.assertEqual(FakeWatchClient.instances[-1].maximum_active_reads, 1)
        self.assertEqual(manager.commands.latest_telemetry()["steps"], 6603)
        await manager.stop()

    async def test_fetch_steps_survives_unavailable_notification_characteristics(self):
        class NoNotificationsClient(FakeWatchClient):
            async def start_activity_notifications(self, *, on_raw=None, on_decode_error=None):
                raise RuntimeError("Characteristic 0000fee3-0000-1000-8000-00805f9b34fb was not found")

            async def start_step_notifications(self, *, on_raw=None, on_decode_error=None):
                raise RuntimeError("step notification characteristic is unavailable")

            async def start_heart_rate_notifications(self):
                raise RuntimeError("heart-rate notification characteristic is unavailable")

        snapshot, history, diagnostics = make_stores()
        manager = WatchManager(
            snapshot,
            history,
            diagnostics,
            scan_fn=fake_scan([DEVICE]),
            client_class=NoNotificationsClient,
        )
        manager.start()
        await self._wait_for(
            lambda: manager.state == "connected"
            and diagnostics.snapshot()["connection_events"]
            and diagnostics.snapshot()["connection_events"][-1]["event"]
            == "notification-subscribe-failed"
        )
        command = manager.submit_fetch_steps()
        await self._wait_for(
            lambda: manager.commands.get(command["command_id"])["state"] in ("succeeded", "failed")
        )
        result = manager.commands.get(command["command_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["steps"], 6603)
        errors = [
            event["detail"]
            for event in diagnostics.snapshot()["connection_events"]
            if event["event"] == "notification-subscribe-failed"
        ]
        self.assertEqual(len(errors), 3)
        await manager.stop()

    async def test_missing_fee3_skips_protocol_queries_but_keeps_fetch_steps(self):
        class NoFee3ProtocolClient(FakeWatchClient):
            def __init__(self, device):
                super().__init__(device)
                self.protocol_query_calls = []
                self.activity_read_calls = 0

            async def get_step_goal(self):
                self.protocol_query_calls.append("step_goal")
                raise RuntimeError(
                    "Characteristic 0000fee3-0000-1000-8000-00805f9b34fb was not found"
                )

            async def get_past_activity(self, day_index):
                self.protocol_query_calls.append(f"past_activity_{day_index}")
                return await super().get_past_activity(day_index)

            async def get_hourly_activity(self, day_index=0):
                self.protocol_query_calls.append("hourly_activity")
                return await super().get_hourly_activity(day_index)

            async def get_today_sleep(self):
                self.protocol_query_calls.append("sleep")
                return None

            async def get_heart_rate_history(self, index):
                self.protocol_query_calls.append(f"heart_rate_history_{index}")
                return None

            async def read_live_activity(self):
                self.activity_read_calls += 1
                return await super().read_live_activity()

        snapshot, history, diagnostics = make_stores()
        manager = WatchManager(
            snapshot,
            history,
            diagnostics,
            scan_fn=fake_scan([DEVICE]),
            client_class=NoFee3ProtocolClient,
        )
        manager.start()
        await self._wait_for(
            lambda: manager.state == "connected"
            and manager.protocol_channel_error is not None
        )
        client = FakeWatchClient.instances[-1]
        command = manager.submit_fetch_steps()
        await self._wait_for(
            lambda: manager.commands.get(command["command_id"])["state"] in ("succeeded", "failed")
        )

        result = manager.commands.get(command["command_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["steps"], 6603)
        self.assertEqual(client.protocol_query_calls, ["step_goal"])
        self.assertEqual(client.activity_read_calls, 1)
        events = diagnostics.snapshot()["connection_events"]
        self.assertEqual(
            sum(event["event"] == "protocol-channel-unavailable" for event in events),
            1,
        )
        await manager.stop()

    async def test_fetch_steps_fails_when_watch_is_disconnected(self):
        snapshot, history, diagnostics = make_stores()
        manager = WatchManager(
            snapshot,
            history,
            diagnostics,
            scan_fn=fake_scan([]),
            client_class=FakeWatchClient,
        )
        manager.start()
        await self._wait_for(lambda: manager.state == "no-watch-found")
        command = manager.submit_fetch_steps()
        await self._wait_for(
            lambda: manager.commands.get(command["command_id"])["state"] == "failed"
        )
        result = manager.commands.get(command["command_id"])
        self.assertIn("Watch is not connected", result["error"])
        await manager.stop()

    async def test_fetch_steps_reports_ble_read_timeout(self):
        class SlowWatchClient(FakeWatchClient):
            async def read_live_activity(self):
                await asyncio.sleep(1)

        snapshot, history, diagnostics = make_stores()
        manager = WatchManager(
            snapshot,
            history,
            diagnostics,
            scan_fn=fake_scan([DEVICE]),
            client_class=SlowWatchClient,
            fetch_steps_timeout=0.01,
        )
        manager.start()
        await self._wait_for(lambda: manager.state == "connected")
        command = manager.submit_fetch_steps()
        await self._wait_for(
            lambda: manager.commands.get(command["command_id"])["state"] == "failed"
        )
        result = manager.commands.get(command["command_id"])
        self.assertIn("timed out", result["error"])
        self.assertIsNone(manager.commands.latest_telemetry())
        await manager.stop()

    async def test_notification_hooks_feed_diagnostics(self):
        snapshot, history, diagnostics = make_stores()
        previous_instances = len(FakeWatchClient.instances)
        manager = WatchManager(snapshot, history, diagnostics, scan_fn=fake_scan([DEVICE]), client_class=FakeWatchClient)
        manager.start()
        for _ in range(100):
            if (
                len(FakeWatchClient.instances) > previous_instances
                and FakeWatchClient.instances[-1].activity_hooks.get("fea1_raw")
            ):
                break
            await asyncio.sleep(0.02)
        client = FakeWatchClient.instances[-1]
        client.activity_hooks["fea1_raw"](bytes.fromhex("02 00 00 00"))
        client.activity_hooks["fee1_error"](bytes.fromhex("D4 12 00"), ValueError("too short"))
        for _ in range(50):
            if diagnostics.snapshot()["counters"]["notifications_total"] >= 2:
                break
            await asyncio.sleep(0.02)
        counters = diagnostics.snapshot()["counters"]
        self.assertGreaterEqual(counters["notifications_total"], 2)
        self.assertEqual(counters["rejected_total"], 1)
        await manager.stop()

    async def test_no_watch_found_then_stopped(self):
        snapshot, history, diagnostics = make_stores()
        manager = WatchManager(snapshot, history, diagnostics, scan_fn=fake_scan([]), client_class=FakeWatchClient)
        manager.start()
        for _ in range(100):
            if manager.state == "no-watch-found":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(manager.state, "no-watch-found")
        await manager.stop()

    async def test_connection_error_is_recorded(self):
        snapshot, history, diagnostics = make_stores()

        class ExplodingClient(FakeWatchClient):
            async def connect(self, verify: bool = True) -> None:
                raise RuntimeError("GATT timeout")

        manager = WatchManager(snapshot, history, diagnostics, scan_fn=fake_scan([DEVICE]), client_class=ExplodingClient)
        manager.start()
        for _ in range(100):
            if diagnostics.snapshot()["counters"]["connection_errors_total"]:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(manager.state, "error")
        self.assertIn("GATT timeout", manager.last_error)
        await manager.stop()

    async def test_reconnect_request_is_honoured(self):
        snapshot, history, diagnostics = make_stores()
        manager = WatchManager(snapshot, history, diagnostics, scan_fn=fake_scan([DEVICE]), client_class=FakeWatchClient)
        manager.start()
        for _ in range(100):
            if FakeWatchClient.instances and FakeWatchClient.instances[-1].is_connected:
                break
            await asyncio.sleep(0.02)
        first = FakeWatchClient.instances[-1]
        manager.request_reconnect()
        for _ in range(100):
            if len(FakeWatchClient.instances) >= 2 and FakeWatchClient.instances[-1] is not first and FakeWatchClient.instances[-1].is_connected:
                break
            await asyncio.sleep(0.02)
        self.assertIsNot(first, FakeWatchClient.instances[-1])
        self.assertTrue(first.disconnected, "previous client must be disconnected on reconnect")
        await manager.stop()


class TokenFileTests(unittest.TestCase):
    def test_extension_origin_is_normalized_and_pinned(self):
        origin = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        self.assertEqual(normalize_extension_origin(origin + "/"), origin)
        with self.assertRaises(ValueError):
            normalize_extension_origin("chrome-extension://other/")

    def test_extension_origin_loads_from_local_file(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "extension_origin.txt"
            path.write_text("chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/\n", encoding="utf-8")
            self.assertEqual(
                load_extension_origin(path, None),
                "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            )
            self.assertEqual(
                load_extension_origin(path, "chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
                "chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            )

    def test_extension_origin_defaults_to_current_dashboard_origin(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            origin = load_extension_origin(Path(tmp) / "missing-origin.txt", None)
        self.assertEqual(origin, DEFAULT_EXTENSION_ORIGIN)

    def test_explicit_token_wins_and_no_file_is_written(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.txt"
            token = load_or_create_token(path, "explicit")
            self.assertEqual(token, "explicit")
            self.assertFalse(path.exists())

    def test_generated_token_is_persisted_and_reused(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.txt"
            first = load_or_create_token(path, None)
            self.assertTrue(first)
            self.assertEqual(path.read_text(encoding="utf-8"), first)
            self.assertEqual(load_or_create_token(path, None), first)


if __name__ == "__main__":
    unittest.main()
