from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .exceptions import ProtocolError
from .models import (
    Alarm,
    BatteryLevel,
    HeartRateHistory,
    HeartRateMeasurement,
    HistoricalActivity,
    HourlyActivity,
    LiveActivity,
    MoveReminderSetting,
    RawSetting,
    SleepStage,
    StepCount,
    TimeRangeSetting,
    WatchFaceLayout,
    WorkoutSummary,
)

WATCH_TZ = timezone(timedelta(hours=8))

LANGUAGES = {
    0: "english", 1: "chinese", 2: "japanese", 3: "korean", 4: "german",
    5: "french", 6: "spanish", 7: "arabic", 8: "russian", 9: "traditional",
    10: "ukrainian", 11: "italian", 12: "portuguese", 13: "dutch", 14: "polish",
    15: "swedish", 16: "finnish", 17: "danish", 18: "norwegian", 19: "hungarian",
    20: "czech", 21: "bulgarian", 22: "romanian", 23: "slovak", 24: "latvian",
}

WORKOUT_TYPES = {
    0: "walk", 1: "run", 2: "biking", 3: "rope", 4: "badminton",
    5: "basketball", 6: "football", 7: "swim", 8: "mountaineering",
    9: "tennis", 10: "rugby", 11: "golf", 12: "yoga", 13: "fitness",
    14: "dancing", 15: "baseball", 16: "elliptical", 17: "indoor_cycling",
    18: "free_exercise", 19: "rowing_machine",
}

WEEKDAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


def _require_length(payload: bytes, minimum: int, label: str) -> None:
    if len(payload) < minimum:
        raise ProtocolError(f"{label} response requires at least {minimum} bytes; got {len(payload)}")


def decode_battery(payload: bytes) -> BatteryLevel:
    _require_length(payload, 1, "battery")
    if payload[0] > 100:
        raise ProtocolError(f"invalid battery percentage: {payload[0]}")
    return BatteryLevel(percent=payload[0])


def decode_heart_rate_measurement(payload: bytes) -> HeartRateMeasurement:
    _require_length(payload, 2, "heart-rate measurement")
    flags = payload[0]
    offset = 1

    if flags & 0x01:
        _require_length(payload, 3, "16-bit heart-rate measurement")
        bpm = int.from_bytes(payload[offset:offset + 2], "little")
        offset += 2
    else:
        bpm = payload[offset]
        offset += 1

    energy = None
    if flags & 0x08:
        _require_length(payload, offset + 2, "heart-rate energy")
        energy = int.from_bytes(payload[offset:offset + 2], "little")
        offset += 2

    rr: tuple[int, ...] = ()
    if flags & 0x10:
        if (len(payload) - offset) % 2:
            raise ProtocolError("heart-rate RR interval data has an odd byte count")
        rr = tuple(
            int.from_bytes(payload[i:i + 2], "little")
            for i in range(offset, len(payload), 2)
        )

    return HeartRateMeasurement(
        bpm=bpm,
        sensor_contact_supported=bool(flags & 0x04),
        sensor_contact_detected=bool(flags & 0x02),
        energy_expended=energy,
        rr_intervals=rr,
    )


def decode_live_activity(payload: bytes) -> LiveActivity:
    if len(payload) < 9:
        raise ProtocolError(f"live activity response requires at least 9 bytes; got {len(payload)}")
    return LiveActivity(
        steps=int.from_bytes(payload[0:3], "little"),
        distance_m=int.from_bytes(payload[3:6], "little"),
        calories_kcal=int.from_bytes(payload[6:9], "little"),
        trailing_bytes=bytes(payload[9:]),
        raw=bytes(payload),
    )


def decode_compact_step_count(payload: bytes) -> StepCount:
    if len(payload) != 4 or payload[0] != 0x01:
        raise ProtocolError(f"unexpected compact step-count notification: {payload.hex(' ')}")
    return StepCount(steps=int.from_bytes(payload[1:4], "little"))


def decode_active_watch_face(payload: bytes) -> int:
    _require_length(payload, 1, "active watch face")
    return payload[0]


def decode_watch_face_layout(payload: bytes) -> WatchFaceLayout:
    _require_length(payload, 37, "watch-face layout")
    md5 = "".join(f"{value & 0x0F:x}" for value in payload[5:37])
    return WatchFaceLayout(
        time_position=payload[0],
        time_top=payload[1],
        time_bottom=payload[2],
        text_color_rgb565=int.from_bytes(payload[3:5], "big"),
        background_md5_nibbles=md5,
        trailing_bytes=bytes(payload[37:]),
        raw=bytes(payload),
    )


def decode_time_format(payload: bytes) -> str:
    _require_length(payload, 1, "time format")
    values = {0: "12-hour", 1: "24-hour"}
    return values.get(payload[0], f"unknown ({payload[0]})")


def decode_measurement_units(payload: bytes) -> str:
    _require_length(payload, 1, "measurement units")
    values = {0: "metric", 1: "imperial"}
    return values.get(payload[0], f"unknown ({payload[0]})")


def decode_language(payload: bytes) -> dict[str, object]:
    _require_length(payload, 5, "device language")
    current_id = payload[0]
    mask = int.from_bytes(payload[1:5], "big")
    supported = tuple(name for index, name in LANGUAGES.items() if mask & (1 << index))
    return {
        "current_id": current_id,
        "current": LANGUAGES.get(current_id, f"unknown ({current_id})"),
        "supported": supported,
        "supported_mask": mask,
        "trailing_bytes": bytes(payload[5:]),
    }


def decode_device_version(payload: bytes) -> str:
    _require_length(payload, 1, "device version")
    values = {0: "chinese", 1: "international"}
    return values.get(payload[0], f"unknown ({payload[0]})")


def decode_step_goal(payload: bytes) -> int:
    _require_length(payload, 4, "step goal")
    return int.from_bytes(payload[:4], "little")


def decode_dominant_hand(payload: bytes) -> str:
    _require_length(payload, 1, "dominant hand")
    values = {0: "left", 1: "right"}
    return values.get(payload[0], f"unknown ({payload[0]})")


def decode_boolean_setting(command: int, payload: bytes) -> bool | RawSetting:
    if not payload:
        return RawSetting(command=command, payload=bytes(payload))
    if payload[0] not in (0, 1):
        return RawSetting(command=command, payload=bytes(payload))
    if len(payload) != 1:
        return RawSetting(command=command, payload=bytes(payload))
    return bool(payload[0])


def decode_time_range(command: int, payload: bytes) -> TimeRangeSetting | RawSetting:
    if len(payload) != 4:
        return RawSetting(command=command, payload=bytes(payload))
    return TimeRangeSetting(
        start_hour=payload[0],
        start_minute=payload[1],
        end_hour=payload[2],
        end_minute=payload[3],
        raw=bytes(payload),
    )


def decode_move_reminder(payload: bytes) -> MoveReminderSetting | RawSetting:
    # Layout inferred from one capture (3C 32 0A 00 16 00: every 60 min, 50 steps,
    # 10:00-22:00). Not yet confirmed against the watch's own settings screen.
    if len(payload) < 6:
        return RawSetting(command=131, payload=bytes(payload))
    return MoveReminderSetting(
        period=payload[0],
        steps=payload[1],
        start_hour=payload[2],
        start_minute=payload[3],
        end_hour=payload[4],
        end_minute=payload[5],
        trailing_bytes=bytes(payload[6:]),
        raw=bytes(payload),
    )


def decode_heart_rate_history(payload: bytes) -> HeartRateHistory:
    _require_length(payload, 1, "heart-rate history")
    index = payload[0]
    samples = tuple(payload[1:])
    if len(samples) > 72:
        samples = samples[:72]
    return HeartRateHistory(
        index=index,
        days_ago=index // 4,
        start_hour=(index % 4) * 6,
        samples_bpm=samples,
        trailing_bytes=bytes(payload[73:]),
    )


def decode_historical_activity(payload: bytes) -> HistoricalActivity:
    _require_length(payload, 10, "historical activity")
    index = payload[0]
    data = payload[1:10]
    return HistoricalActivity(
        day_index=index,
        steps=int.from_bytes(data[0:3], "little"),
        distance_m=int.from_bytes(data[3:6], "little"),
        calories_kcal=int.from_bytes(data[6:9], "little"),
        trailing_bytes=bytes(payload[10:]),
        raw=bytes(payload),
    )


def decode_sleep_stages(payload: bytes) -> tuple[SleepStage, ...]:
    if len(payload) % 3:
        raise ProtocolError(f"sleep response length must be a multiple of 3; got {len(payload)}")
    names = {0: "sober", 1: "light", 2: "restful", 3: "rem"}
    stages = []
    for offset in range(0, len(payload), 3):
        code, hour, minute = payload[offset:offset + 3]
        stages.append(SleepStage(
            stage_code=code,
            stage_name=names.get(code, f"unknown ({code})"),
            hour=hour,
            minute=minute,
        ))
    return tuple(stages)


def decode_past_sleep(payload: bytes, *, expected_index: int | None = None) -> tuple[SleepStage, ...]:
    _require_length(payload, 1, "historical sleep")
    if expected_index is not None and payload[0] != expected_index:
        raise ProtocolError(f"sleep history index mismatch: expected {expected_index}, got {payload[0]}")
    return decode_sleep_stages(payload[1:])


def decode_hourly_activity(payload: bytes) -> HourlyActivity:
    _require_length(payload, 1, "hourly activity")
    body = payload[1:]
    usable_length = len(body) - (len(body) % 2)
    buckets = tuple(
        int.from_bytes(body[i:i + 2], "little")
        for i in range(0, usable_length, 2)
    )
    trailing = body[-1] if len(body) % 2 else None
    return HourlyActivity(
        day_index=payload[0],
        steps_by_bucket=buckets,
        trailing_byte=trailing,
        raw=bytes(payload),
    )


def decode_alarms(payload: bytes) -> tuple[Alarm, ...]:
    data = bytes(payload)
    if len(data) % 8 == 3:
        count = data[2]
        data = data[3:]
    elif len(data) % 8 == 0:
        count = len(data) // 8
    else:
        raise ProtocolError(f"invalid alarm payload length: {len(payload)}")

    alarms = []
    for i in range(min(count, len(data) // 8)):
        entry = data[i * 8:i * 8 + 8]
        mask = entry[7]
        weekdays = tuple(day for bit, day in enumerate(WEEKDAYS) if mask & (1 << bit))
        alarms.append(Alarm(
            index=entry[0],
            enabled=bool(entry[1]),
            hour=entry[3],
            minute=entry[4],
            repeat_mask=mask,
            weekdays=weekdays,
        ))
    return tuple(alarms)


def _watch_datetime(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=WATCH_TZ).replace(tzinfo=None)


def decode_workout_summaries(payload: bytes) -> tuple[WorkoutSummary, ...]:
    records = []
    for offset in range(0, len(payload) - 23, 24):
        entry = payload[offset:offset + 24]
        if not any(entry):
            continue
        start = int.from_bytes(entry[0:4], "little")
        end = int.from_bytes(entry[4:8], "little")
        workout_type = entry[11]
        records.append(WorkoutSummary(
            start_epoch=start,
            end_epoch=end,
            start_time=_watch_datetime(start),
            end_time=_watch_datetime(end),
            valid_time_seconds=int.from_bytes(entry[8:10], "little"),
            entry_number=entry[10],
            workout_type=workout_type,
            workout_name=WORKOUT_TYPES.get(workout_type, f"unknown ({workout_type})"),
            steps=int.from_bytes(entry[12:16], "little"),
            distance_m=int.from_bytes(entry[16:20], "little"),
            calories_kcal=int.from_bytes(entry[20:22], "little"),
            trailing_bytes=bytes(entry[22:24]),
        ))
    return tuple(records)


def decode_setting(command: int, payload: bytes):
    decoders = {
        41: decode_active_watch_face,
        57: decode_watch_face_layout,
        39: decode_time_format,
        42: decode_measurement_units,
        43: decode_language,
        46: decode_device_version,
        38: decode_step_goal,
        36: decode_dominant_hand,
        40: lambda data: decode_boolean_setting(command, data),
        45: lambda data: decode_boolean_setting(command, data),
        129: lambda data: decode_time_range(command, data),
        130: lambda data: decode_time_range(command, data),
        131: decode_move_reminder,
        164: lambda data: RawSetting(command=command, payload=bytes(data)),
        136: lambda data: decode_boolean_setting(command, data),
        33: decode_alarms,
    }
    decoder = decoders.get(command)
    if decoder is None:
        return RawSetting(command=command, payload=bytes(payload))
    return decoder(payload)
