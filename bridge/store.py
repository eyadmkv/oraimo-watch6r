"""Thread-safe data stores shared between the asyncio watch manager and the HTTP server thread.

Data-integrity rules enforced here:

- Unavailable measurements stay ``None`` and are never substituted with zero.
- Raw payloads and unknown trailing bytes are preserved as hex for diagnostics.
- Provisional interpretations carry an explicit ``provisional`` marker so the
  UI can label them instead of presenting them as verified.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from datetime import datetime

from oraimo_watch6r.models import (
    DeviceInfo,
    HeartRateHistory,
    HistoricalActivity,
    HourlyActivity,
    LiveActivity,
    SleepStage,
)

DIAGNOSTICS_MAX_EVENTS = 200

FEE1 = "0000fee1-0000-1000-8000-00805f9b34fb"
FEA1 = "0000fea1-0000-1000-8000-00805f9b34fb"
HEART_RATE = "00002a37-0000-1000-8000-00805f9b34fb"


def timestamp() -> dict:
    """Wall-clock stamp as epoch seconds plus a local-time ISO string."""
    epoch = time.time()
    iso = datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
    return {"epoch": round(epoch, 3), "iso": iso}


def bytes_hex(value) -> str | None:
    return value.hex(" ").upper() if value else None


# --- SDK model -> JSON transformations -------------------------------------


def device_info_dict(info: DeviceInfo) -> dict:
    return {
        "connected_name": info.connected_name,
        "manufacturer": info.manufacturer,
        "firmware": info.firmware,
        "software": info.software,
        "battery_percent": info.battery_percent,
    }


def live_activity_dict(activity: LiveActivity) -> dict:
    return {
        "steps": activity.steps,
        "distance_m": activity.distance_m,
        "calories_kcal": activity.calories_kcal,
        "trailing_bytes_hex": bytes_hex(activity.trailing_bytes),
        "raw_hex": bytes_hex(activity.raw),
        "provisional": True,
    }


def historical_activity_dict(activity: HistoricalActivity) -> dict:
    return {
        "day_index": activity.day_index,
        "steps": activity.steps,
        "distance_m": activity.distance_m,
        "calories_kcal": activity.calories_kcal,
        "trailing_bytes_hex": bytes_hex(activity.trailing_bytes),
        "provisional": True,
    }


def hourly_activity_dict(hourly: HourlyActivity) -> dict:
    return {
        "day_index": hourly.day_index,
        "steps_by_bucket": list(hourly.steps_by_bucket),
        "trailing_byte": hourly.trailing_byte,
        "provisional": True,
    }


def heart_rate_history_dict(history: HeartRateHistory) -> dict:
    return {
        "index": history.index,
        "days_ago": history.days_ago,
        "start_hour": history.start_hour,
        "samples_bpm": list(history.samples_bpm),
        "minimum_bpm": history.minimum_bpm,
        "maximum_bpm": history.maximum_bpm,
        "average_bpm": history.average_bpm,
    }


def sleep_stages_list(stages) -> list[dict] | None:
    if not stages:
        return None
    return [
        {
            "stage_code": stage.stage_code,
            "stage_name": stage.stage_name,
            "hour": stage.hour,
            "minute": stage.minute,
        }
        for stage in stages
    ]


# --- Stores -----------------------------------------------------------------


class SnapshotStore:
    """Latest live values. Every field is ``None`` until actually observed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict = {
            "steps": None,
            "steps_source": None,
            "steps_received_at": None,
            "distance_m": None,
            "distance_received_at": None,
            "calories_kcal": None,
            "calories_received_at": None,
            "battery_percent": None,
            "battery_received_at": None,
            "heart_rate_bpm": None,
            "heart_rate_received_at": None,
            "step_goal": None,
            "last_notification_at": None,
            "last_activity_trailing_hex": None,
        }

    def apply_step_count(self, steps: int) -> None:
        stamp = timestamp()
        with self._lock:
            self._data["steps"] = steps
            self._data["steps_source"] = "FEA1"
            self._data["steps_received_at"] = stamp
            self._data["last_notification_at"] = stamp

    def apply_live_activity(self, activity: LiveActivity) -> None:
        stamp = timestamp()
        with self._lock:
            self._data["steps"] = activity.steps
            self._data["steps_source"] = "FEE1"
            self._data["steps_received_at"] = stamp
            self._data["distance_m"] = activity.distance_m
            self._data["distance_received_at"] = stamp
            self._data["calories_kcal"] = activity.calories_kcal
            self._data["calories_received_at"] = stamp
            self._data["last_notification_at"] = stamp
            self._data["last_activity_trailing_hex"] = bytes_hex(activity.trailing_bytes)

    def apply_heart_rate(self, bpm: int) -> None:
        stamp = timestamp()
        with self._lock:
            self._data["heart_rate_bpm"] = bpm
            self._data["heart_rate_received_at"] = stamp
            self._data["last_notification_at"] = stamp

    def apply_battery(self, percent: int) -> None:
        with self._lock:
            self._data["battery_percent"] = percent
            self._data["battery_received_at"] = timestamp()

    def apply_step_goal(self, goal: int) -> None:
        with self._lock:
            self._data["step_goal"] = goal

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)


class CommandStore:
    """Thread-safe command state and latest successful on-demand telemetry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._commands: dict[str, dict] = {}
        self._latest_telemetry: dict | None = None

    def create(self, command_type: str) -> dict:
        command_id = str(uuid.uuid4())
        stamp = timestamp()
        command = {
            "command_id": command_id,
            "type": command_type,
            "state": "queued",
            "created_at": stamp,
            "updated_at": stamp,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._commands[command_id] = command
            return copy.deepcopy(command)

    def get(self, command_id: str) -> dict | None:
        with self._lock:
            command = self._commands.get(command_id)
            return copy.deepcopy(command) if command is not None else None

    def update(
        self,
        command_id: str,
        state: str,
        *,
        result: dict | None = None,
        error: str | None = None,
    ) -> dict:
        with self._lock:
            command = self._commands.get(command_id)
            if command is None:
                raise KeyError(f"unknown command ID: {command_id}")
            allowed = {
                "queued": {"running", "failed"},
                "running": {"succeeded", "failed"},
            }
            if state not in allowed.get(command["state"], set()):
                raise ValueError(f"invalid command transition: {command['state']} -> {state}")

            command["state"] = state
            command["updated_at"] = timestamp()
            command["result"] = copy.deepcopy(result)
            command["error"] = error
            if state == "succeeded":
                self._latest_telemetry = {
                    "command_id": command_id,
                    **copy.deepcopy(result or {}),
                }
            return copy.deepcopy(command)

    def latest_telemetry(self) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._latest_telemetry)


class HistoryStore:
    """Slower-moving data collected once per connection (and refreshed)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict = {
            "device_info": None,
            "step_goal": None,
            "past_activity": {"1": None, "2": None},
            "hourly_activity": None,
            "heart_rate_history": [],
            "sleep_today": None,
            "fetched_at": None,
        }

    def set_device_info(self, info: DeviceInfo) -> None:
        with self._lock:
            self._data["device_info"] = device_info_dict(info)

    def set_step_goal(self, goal: int) -> None:
        with self._lock:
            self._data["step_goal"] = goal

    def set_past_activity(self, day_index: int, activity: HistoricalActivity | None) -> None:
        with self._lock:
            key = str(day_index)
            self._data["past_activity"][key] = (
                historical_activity_dict(activity) if activity is not None else None
            )

    def set_hourly_activity(self, hourly: HourlyActivity | None) -> None:
        with self._lock:
            self._data["hourly_activity"] = (
                hourly_activity_dict(hourly) if hourly is not None else None
            )

    def set_heart_rate_history(self, blocks: list[HeartRateHistory | None]) -> None:
        with self._lock:
            self._data["heart_rate_history"] = [
                heart_rate_history_dict(block) for block in blocks if block is not None
            ]

    def set_sleep_today(self, stages) -> None:
        with self._lock:
            self._data["sleep_today"] = sleep_stages_list(stages)

    def mark_fetched(self) -> None:
        with self._lock:
            self._data["fetched_at"] = timestamp()

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)


class DiagnosticsStore:
    """Ring buffer of raw notifications, decode failures and connection events."""

    def __init__(self, max_events: int = DIAGNOSTICS_MAX_EVENTS) -> None:
        self._lock = threading.Lock()
        self._max_events = max_events
        self._notifications: list[dict] = []
        self._connection_events: list[dict] = []
        self._counters = {
            "notifications_total": 0,
            "decoded_total": 0,
            "rejected_total": 0,
            "connection_errors_total": 0,
        }

    def record_notification(self, uuid: str, payload_hex: str, *, status: str, error: str | None = None) -> None:
        entry = {
            "received_at": timestamp(),
            "uuid": uuid,
            "status": status,
            "payload_hex": payload_hex,
            "error": error,
        }
        with self._lock:
            self._counters["notifications_total"] += 1
            if status == "decode-error":
                self._counters["rejected_total"] += 1
            self._notifications.append(entry)
            if len(self._notifications) > self._max_events:
                del self._notifications[: len(self._notifications) - self._max_events]

    def record_decoded(self) -> None:
        with self._lock:
            self._counters["decoded_total"] += 1

    def record_connection_event(self, event: str, detail: str | None = None) -> None:
        entry = {"received_at": timestamp(), "event": event, "detail": detail}
        with self._lock:
            if event in ("connection-failed", "query-failed"):
                self._counters["connection_errors_total"] += 1
            self._connection_events.append(entry)
            if len(self._connection_events) > self._max_events:
                del self._connection_events[: len(self._connection_events) - self._max_events]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "notifications": copy.deepcopy(self._notifications),
                "connection_events": copy.deepcopy(self._connection_events),
                "counters": dict(self._counters),
            }
