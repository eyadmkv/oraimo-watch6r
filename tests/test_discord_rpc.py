"""Unit tests for the optional Discord Rich Presence publisher."""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.discord_rpc import (
    compose_activity,
    encode_frame,
    handshake,
    load_client_id,
    read_frame,
    send_set_activity,
)
from bridge.store import DiagnosticsStore, HistoryStore, SnapshotStore
from bridge.watch_manager import WatchManager


class FakePipe:
    """Minimal file-like double for the Discord IPC named pipe."""

    def __init__(self, responses=()):
        self.responses = [bytes(response) for response in responses]
        self.incoming = b""
        self.written: list[bytes] = []

    def write(self, data):
        self.written.append(bytes(data))
        if self.responses:
            self.incoming += self.responses.pop(0)
        return len(data)

    def read(self, size):
        chunk, self.incoming = self.incoming[:size], self.incoming[size:]
        return chunk

    def close(self):
        pass


def ready_frame() -> bytes:
    return encode_frame(1, {"cmd": "DISPATCH", "evt": "READY", "data": {}})


def parse_written(pipe: FakePipe, index: int = 0) -> tuple[int, dict]:
    header = pipe.written[index][:8]
    opcode, _length = struct.unpack("<II", header)
    payload = json.loads(pipe.written[index][8:].decode("utf-8"))
    return opcode, payload


class FrameTests(unittest.TestCase):
    def test_encode_read_round_trip(self):
        pipe = FakePipe()
        pipe.incoming += encode_frame(1, {"hello": "world"})
        opcode, payload = read_frame(pipe)
        self.assertEqual(opcode, 1)
        self.assertEqual(payload, {"hello": "world"})

    def test_read_frame_rejects_truncated_header(self):
        pipe = FakePipe()
        pipe.incoming += b"\x01\x00"
        self.assertIsNone(read_frame(pipe))


class ProtocolTests(unittest.TestCase):
    def test_handshake_sends_v1_and_client_id(self):
        pipe = FakePipe(responses=[ready_frame()])
        handshake(pipe, "1234567890")
        opcode, payload = parse_written(pipe)
        self.assertEqual(opcode, 0)
        self.assertEqual(payload, {"v": 1, "client_id": "1234567890"})

    def test_handshake_raises_without_ready_frame(self):
        pipe = FakePipe()  # never responds
        with self.assertRaises(OSError):
            handshake(pipe, "1234567890")

    def test_handshake_raises_on_error_event(self):
        pipe = FakePipe(responses=[encode_frame(1, {"evt": "ERROR"})])
        with self.assertRaises(OSError):
            handshake(pipe, "1234567890")

    def test_set_activity_sends_command_frame_with_pid(self):
        pipe = FakePipe(responses=[encode_frame(1, {"cmd": "SET_ACTIVITY", "evt": None})])
        activity = compose_activity("connected", 72, 6603, 1_700_000_000)
        send_set_activity(pipe, activity)
        opcode, payload = parse_written(pipe)
        self.assertEqual(opcode, 1)
        self.assertEqual(payload["cmd"], "SET_ACTIVITY")
        self.assertEqual(payload["args"]["activity"], activity)
        self.assertIsInstance(payload["args"]["pid"], int)
        self.assertIn("nonce", payload)


class ComposeActivityTests(unittest.TestCase):
    def test_connected_with_heart_rate_and_steps(self):
        activity = compose_activity("connected", 72, 6603, 1_700_000_000)
        self.assertEqual(activity["state"], "♥ 72 bpm")
        self.assertEqual(activity["details"], "6,603 steps")
        self.assertEqual(activity["timestamps"]["start"], 1_700_000_000)

    def test_connected_without_data_omits_values(self):
        activity = compose_activity("connected", None, None, 0)
        self.assertEqual(activity["state"], "Connected")
        self.assertEqual(activity["details"], "Oraimo Watch 6R")

    def test_connecting_states_show_connecting(self):
        for state in ("scanning", "connecting", "reconnecting"):
            with self.subTest(state=state):
                activity = compose_activity(state, None, None, 0)
                self.assertEqual(activity["state"], "Connecting…")
                self.assertEqual(activity["details"], "Oraimo Watch 6R")

    def test_stopped_shows_disconnected_even_with_data(self):
        activity = compose_activity("stopped", 72, 6603, 0)
        self.assertEqual(activity["state"], "Watch disconnected")

    def test_error_state_shows_disconnected(self):
        activity = compose_activity("error", None, None, 0)
        self.assertEqual(activity["state"], "Watch disconnected")


class LoadClientIdTests(unittest.TestCase):
    def test_explicit_value_wins_and_is_trimmed(self):
        self.assertEqual(load_client_id(Path("nowhere"), " 42 "), "42")

    def test_missing_file_disables(self):
        self.assertIsNone(load_client_id(Path("nowhere"), None))

    def test_empty_file_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "discord_client_id.txt"
            path.write_text("   \n", encoding="utf-8")
            self.assertIsNone(load_client_id(path, None))

    def test_file_value_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "discord_client_id.txt"
            path.write_text("999888777\n", encoding="utf-8")
            self.assertEqual(load_client_id(path, None), "999888777")


class FakePresence:
    def __init__(self):
        self.states: list[str] = []
        self.heart_rates: list = []
        self.steps: list = []

    def set_bridge_state(self, state):
        self.states.append(state)

    def update_heart_rate(self, bpm):
        self.heart_rates.append(bpm)

    def update_steps(self, steps):
        self.steps.append(steps)


class ManagerWiringTests(unittest.TestCase):
    def _manager(self, presence) -> WatchManager:
        return WatchManager(
            SnapshotStore(),
            HistoryStore(),
            DiagnosticsStore(),
            presence=presence,
        )

    def test_state_changes_reach_presence(self):
        presence = FakePresence()
        manager = self._manager(presence)
        manager._set_state("scanning")
        manager._set_state("connected", "oraimo Watch 6R_5947")
        self.assertEqual(presence.states, ["scanning", "connected"])

    def test_heart_rate_and_steps_reach_presence(self):
        presence = FakePresence()
        manager = self._manager(presence)
        manager._apply_heart_rate(SimpleNamespace(bpm=72))
        manager._apply_step_count(SimpleNamespace(steps=6603))
        manager._apply_live_activity(
            SimpleNamespace(
                steps=1625, distance_m=1186, calories_kcal=71, trailing_bytes=b"\x22\x00\x00"
            )
        )
        self.assertEqual(presence.heart_rates, [72])
        self.assertEqual(presence.steps, [6603, 1625])

    def test_none_presence_is_safe(self):
        manager = self._manager(None)
        manager._set_state("scanning")
        manager._apply_heart_rate(SimpleNamespace(bpm=72))
        manager._apply_step_count(SimpleNamespace(steps=1))


if __name__ == "__main__":
    unittest.main()
