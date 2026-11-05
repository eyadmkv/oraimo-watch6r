from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class WatchDevice:
    name: str
    address: str
    rssi: int | None = None
    # Keep the exact BLEDevice returned by BleakScanner. On Windows, passing this
    # object avoids a second address-based discovery step during connection.
    _ble_device: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    manufacturer: str | None
    firmware: str | None
    software: str | None
    battery_percent: int | None
    connected_name: str | None = None


@dataclass(frozen=True, slots=True)
class BatteryLevel:
    percent: int


@dataclass(frozen=True, slots=True)
class StepCount:
    steps: int


@dataclass(frozen=True, slots=True)
class HeartRateMeasurement:
    bpm: int
    sensor_contact_supported: bool
    sensor_contact_detected: bool
    energy_expended: int | None = None
    rr_intervals: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class LiveActivity:
    steps: int | None
    distance_m: int | None
    calories_kcal: int | None
    trailing_bytes: bytes
    raw: bytes


@dataclass(frozen=True, slots=True)
class HeartRateHistory:
    index: int
    days_ago: int
    start_hour: int
    samples_bpm: tuple[int, ...]
    trailing_bytes: bytes = b""

    @property
    def valid_samples(self) -> tuple[int, ...]:
        return tuple(value for value in self.samples_bpm if value != 0)

    @property
    def minimum_bpm(self) -> int | None:
        values = self.valid_samples
        return min(values) if values else None

    @property
    def maximum_bpm(self) -> int | None:
        values = self.valid_samples
        return max(values) if values else None

    @property
    def average_bpm(self) -> float | None:
        values = self.valid_samples
        return sum(values) / len(values) if values else None


@dataclass(frozen=True, slots=True)
class HistoricalActivity:
    day_index: int
    distance_m: int
    steps: int
    calories_kcal: int
    trailing_bytes: bytes
    raw: bytes


@dataclass(frozen=True, slots=True)
class SleepStage:
    stage_code: int
    stage_name: str
    hour: int
    minute: int


@dataclass(frozen=True, slots=True)
class HourlyActivity:
    day_index: int
    steps_by_bucket: tuple[int, ...]
    trailing_byte: int | None = None
    raw: bytes = b""


@dataclass(frozen=True, slots=True)
class WatchFaceLayout:
    time_position: int
    time_top: int
    time_bottom: int
    text_color_rgb565: int
    background_md5_nibbles: str
    trailing_bytes: bytes
    raw: bytes


@dataclass(frozen=True, slots=True)
class TimeRangeSetting:
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    raw: bytes


@dataclass(frozen=True, slots=True)
class MoveReminderSetting:
    period: int
    steps: int
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    trailing_bytes: bytes
    raw: bytes


@dataclass(frozen=True, slots=True)
class Alarm:
    index: int
    enabled: bool
    hour: int
    minute: int
    repeat_mask: int
    weekdays: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkoutSummary:
    start_epoch: int
    end_epoch: int
    start_time: datetime
    end_time: datetime
    valid_time_seconds: int
    entry_number: int
    workout_type: int
    workout_name: str
    steps: int
    distance_m: int
    calories_kcal: int
    trailing_bytes: bytes


@dataclass(frozen=True, slots=True)
class RawSetting:
    command: int
    payload: bytes
