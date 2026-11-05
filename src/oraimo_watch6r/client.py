from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

from . import decoders
from .exceptions import (
    QueryTimeoutError,
    UnsupportedCommandError,
    WatchNotConnectedError,
)
from .models import (
    BatteryLevel,
    DeviceInfo,
    HeartRateMeasurement,
    LiveActivity,
    StepCount,
    WatchDevice,
)
from .protocol import PacketReassembler, ProtocolPacket, build_packet

FEE1 = "0000fee1-0000-1000-8000-00805f9b34fb"
FEE2 = "0000fee2-0000-1000-8000-00805f9b34fb"
FEE3 = "0000fee3-0000-1000-8000-00805f9b34fb"
FEA1 = "0000fea1-0000-1000-8000-00805f9b34fb"
BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
MANUFACTURER = "00002a29-0000-1000-8000-00805f9b34fb"
FIRMWARE = "00002a26-0000-1000-8000-00805f9b34fb"
SOFTWARE = "00002a28-0000-1000-8000-00805f9b34fb"
HEART_RATE = "00002a37-0000-1000-8000-00805f9b34fb"

MOYOUNG_MANUFACTURER = "MOYOUNG-V2"

READ_ONLY_COMMANDS = frozenset({
    33, 36, 38, 39, 40, 41, 42, 43, 45, 46, 50, 51, 53, 55, 57,
    89, 129, 130, 131, 136, 164,
})


class WatchClient:
    """Async read-only BLE client for the Oraimo Watch 6R / MOYOUNG-V2 protocol."""

    def __init__(
        self,
        address: str | WatchDevice,
        *,
        name: str | None = None,
        connect_timeout: float = 20.0,
        default_query_timeout: float = 5.0,
        client_factory: Any | None = None,
    ) -> None:
        if isinstance(address, WatchDevice):
            self.address = address.address
            self.name = name or address.name
            self._address_or_ble_device = address._ble_device or address.address
        else:
            self.address = address
            self.name = name
            self._address_or_ble_device = address
        self.connect_timeout = connect_timeout
        self.default_query_timeout = default_query_timeout
        self._client_factory = client_factory
        self._client: Any | None = None
        self._connected = False
        self._notify_started = False
        self._hr_notify_started = False
        self._activity_notify_started = False
        self._step_notify_started = False
        self._activity_raw_hook: Callable[[bytes], None] | None = None
        self._activity_decode_error_hook: Callable[[bytes, Exception], None] | None = None
        self._step_raw_hook: Callable[[bytes], None] | None = None
        self._step_decode_error_hook: Callable[[bytes, Exception], None] | None = None
        self._packet_reassembler = PacketReassembler()
        self._fragment_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._pending_packets: deque[ProtocolPacket] = deque()
        self._event_queue: asyncio.Queue[ProtocolPacket] = asyncio.Queue()
        self._heart_rate_queue: asyncio.Queue[HeartRateMeasurement] = asyncio.Queue()
        self._activity_queue: asyncio.Queue[LiveActivity] = asyncio.Queue()
        self._step_queue: asyncio.Queue[StepCount] = asyncio.Queue()
        self._query_lock = asyncio.Lock()

    @classmethod
    async def scan(
        cls,
        *,
        timeout: float = 10.0,
        name_contains: str = "oraimo watch 6r",
    ) -> list[WatchDevice]:
        try:
            from bleak import BleakScanner
        except ImportError as exc:
            raise RuntimeError(
                "Bleak is required for BLE scanning. Install with: pip install bleak"
            ) from exc

        # Use the same discovery method as the known-working probe_watch.py.
        found = await BleakScanner.discover(timeout=timeout)

        needle = name_contains.casefold()
        devices: list[WatchDevice] = []

        for device in found:
            device_name = getattr(device, "name", None) or ""

            if needle not in device_name.casefold():
                continue

            devices.append(
                WatchDevice(
                    name=device_name,
                    address=device.address,
                    rssi=getattr(device, "rssi", None),
                    _ble_device=device,
                )
            )

        return sorted(
            devices,
            key=lambda item: item.rssi if item.rssi is not None else -999,
            reverse=True,
        )
    
    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None and bool(self._client.is_connected)

    async def __aenter__(self) -> WatchClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.disconnect()

    async def connect(self, *, verify: bool = True) -> None:
        if self.is_connected:
            return

        if self._client_factory is None:
            try:
                from bleak import BleakClient
            except ImportError as exc:
                raise RuntimeError("Bleak is required for BLE connections. Install with: pip install bleak") from exc
            factory = BleakClient
        else:
            factory = self._client_factory

        self._client = factory(
            self._address_or_ble_device,
            timeout=self.connect_timeout,
        )
        await self._client.connect()
        self._connected = True

        try:
            if verify:
                manufacturer = await self.read_manufacturer()
                if manufacturer != MOYOUNG_MANUFACTURER:
                    raise RuntimeError(
                        f"unexpected manufacturer {manufacturer!r}; expected {MOYOUNG_MANUFACTURER!r}"
                    )
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        if self._client is None:
            self._connected = False
            return

        if self._client.is_connected:
            for started, uuid in (
                (self._hr_notify_started, HEART_RATE),
                (self._activity_notify_started, FEE1),
                (self._step_notify_started, FEA1),
                (self._notify_started, FEE3),
            ):
                if started:
                    try:
                        await self._client.stop_notify(uuid)
                    except Exception:
                        pass
            await self._client.disconnect()

        self._connected = False
        self._notify_started = False
        self._hr_notify_started = False
        self._activity_notify_started = False
        self._step_notify_started = False

    def _require_connection(self) -> Any:
        if not self.is_connected:
            raise WatchNotConnectedError("connect to the watch before calling this method")
        return self._client

    async def _ensure_protocol_notifications(self) -> None:
        client = self._require_connection()
        if not self._notify_started:
            await client.start_notify(FEE3, self._on_protocol_notification)
            self._notify_started = True

    def _on_protocol_notification(self, sender: Any, data: bytearray) -> None:
        self._fragment_queue.put_nowait(bytes(data))

    async def _receive_protocol_packets(self, timeout: float) -> list[ProtocolPacket]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return []
            try:
                fragment = await asyncio.wait_for(self._fragment_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return []
            packets = self._packet_reassembler.feed(fragment)
            if packets:
                return packets

    @staticmethod
    def _response_matches(command: int, payload: bytes, expected_payload: bytes) -> bool:
        if command not in (51, 53, 89) or not expected_payload:
            return True
        return bool(payload) and payload[0] == expected_payload[0]

    async def query(
        self,
        command: int,
        payload: bytes = b"",
        *,
        timeout: float | None = None,
        raise_on_timeout: bool = False,
    ) -> bytes | None:
        if command not in READ_ONLY_COMMANDS:
            raise UnsupportedCommandError(
                f"command 0x{command:02X} is not in the read-only command allowlist"
            )
        client = self._require_connection()
        timeout = self.default_query_timeout if timeout is None else timeout

        async with self._query_lock:
            await self._ensure_protocol_notifications()
            packet = build_packet(command, payload, mtu=247)
            await client.write_gatt_char(FEE2, packet, response=False)
            deadline = asyncio.get_running_loop().time() + timeout

            while True:
                if not self._pending_packets:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        if raise_on_timeout:
                            raise QueryTimeoutError(f"no response to command 0x{command:02X}")
                        return None

                    packets = await self._receive_protocol_packets(remaining)
                    self._pending_packets.extend(packets)
                    if not self._pending_packets:
                        if raise_on_timeout:
                            raise QueryTimeoutError(f"no response to command 0x{command:02X}")
                        return None

                response = self._pending_packets.popleft()
                if response.command == command and self._response_matches(
                    command, response.payload, payload
                ):
                    return response.payload
                self._event_queue.put_nowait(response)

    async def next_protocol_event(self, *, timeout: float | None = None) -> ProtocolPacket:
        if timeout is None:
            return await self._event_queue.get()
        return await asyncio.wait_for(self._event_queue.get(), timeout=timeout)

    async def read_manufacturer(self) -> str:
        raw = bytes(await self._require_connection().read_gatt_char(MANUFACTURER))
        return raw.decode("utf-8", errors="replace").strip("\x00").strip()

    async def read_firmware(self) -> str:
        raw = bytes(await self._require_connection().read_gatt_char(FIRMWARE))
        return raw.decode("utf-8", errors="replace").strip("\x00").strip()

    async def read_software(self) -> str:
        raw = bytes(await self._require_connection().read_gatt_char(SOFTWARE))
        return raw.decode("utf-8", errors="replace").strip("\x00").strip()

    async def read_battery(self) -> BatteryLevel:
        raw = bytes(await self._require_connection().read_gatt_char(BATTERY))
        return decoders.decode_battery(raw)

    async def device_info(self) -> DeviceInfo:
        manufacturer = await self.read_manufacturer()
        firmware = await self.read_firmware()
        software = await self.read_software()
        try:
            battery = (await self.read_battery()).percent
        except Exception:
            battery = None
        return DeviceInfo(
            manufacturer=manufacturer,
            firmware=firmware,
            software=software,
            battery_percent=battery,
            connected_name=self.name,
        )

    async def read_live_activity(self) -> LiveActivity:
        async with self._query_lock:
            raw = bytes(await self._require_connection().read_gatt_char(FEE1))
            return decoders.decode_live_activity(raw)

    async def get_setting(self, command: int, *, timeout: float | None = None):
        payload = await self.query(command, timeout=timeout)
        if payload is None:
            return None
        return decoders.decode_setting(command, payload)

    async def get_active_watch_face(self, *, timeout: float | None = None):
        return await self.get_setting(41, timeout=timeout)

    async def get_watch_face_layout(self, *, timeout: float | None = None):
        return await self.get_setting(57, timeout=timeout)

    async def get_time_format(self, *, timeout: float | None = None):
        return await self.get_setting(39, timeout=timeout)

    async def get_measurement_units(self, *, timeout: float | None = None):
        return await self.get_setting(42, timeout=timeout)

    async def get_language(self, *, timeout: float | None = None):
        return await self.get_setting(43, timeout=timeout)

    async def get_device_version(self, *, timeout: float | None = None):
        return await self.get_setting(46, timeout=timeout)

    async def get_step_goal(self, *, timeout: float | None = None):
        return await self.get_setting(38, timeout=timeout)

    async def get_alarms(self, *, timeout: float = 5.0):
        return await self.get_setting(33, timeout=timeout)

    async def get_dominant_hand(self, *, timeout: float | None = None):
        return await self.get_setting(36, timeout=timeout)

    async def get_quick_view(self, *, timeout: float | None = None):
        return await self.get_setting(40, timeout=timeout)

    async def get_sedentary_reminder(self, *, timeout: float | None = None):
        return await self.get_setting(45, timeout=timeout)

    async def get_dnd_schedule(self, *, timeout: float | None = None):
        return await self.get_setting(129, timeout=timeout)

    async def get_quick_view_schedule(self, *, timeout: float | None = None):
        return await self.get_setting(130, timeout=timeout)

    async def get_move_reminder(self, *, timeout: float | None = None):
        return await self.get_setting(131, timeout=timeout)

    async def get_power_saving_raw(self, *, timeout: float | None = None):
        return await self.get_setting(164, timeout=timeout)

    async def get_breathing_light(self, *, timeout: float | None = None):
        return await self.get_setting(136, timeout=timeout)

    async def get_today_sleep(self, *, timeout: float = 8.0):
        payload = await self.query(50, timeout=timeout)
        if payload is None:
            return None
        return decoders.decode_sleep_stages(payload)

    async def get_past_sleep(self, day_index: int, *, timeout: float = 8.0):
        if day_index not in (3, 4):
            raise ValueError("sleep day_index must be 3 (yesterday) or 4 (day before yesterday)")
        payload = await self.query(51, bytes((day_index,)), timeout=timeout)
        if payload is None:
            return None
        return decoders.decode_past_sleep(payload, expected_index=day_index)

    async def get_past_activity(self, day_index: int, *, timeout: float = 8.0):
        if day_index not in (1, 2):
            raise ValueError("activity day_index must be 1 (yesterday) or 2 (day before yesterday)")
        payload = await self.query(51, bytes((day_index,)), timeout=timeout)
        if payload is None:
            return None
        return decoders.decode_historical_activity(payload)

    async def get_hourly_activity(self, day_index: int = 0, *, timeout: float = 8.0):
        if not 0 <= day_index <= 2:
            raise ValueError("day_index must be 0, 1, or 2")
        payload = await self.query(89, bytes((day_index,)), timeout=timeout)
        if payload is None:
            return None
        return decoders.decode_hourly_activity(payload)

    async def get_heart_rate_history(self, index: int = 0, *, timeout: float = 6.0):
        if not 0 <= index <= 7:
            raise ValueError("heart-rate history index must be between 0 and 7")
        payload = await self.query(53, bytes((index,)), timeout=timeout)
        if payload is None:
            return None
        return decoders.decode_heart_rate_history(payload)

    async def get_workout_summaries(self, *, timeout: float = 8.0):
        payload = await self.query(55, timeout=timeout)
        if payload is None:
            return None
        return decoders.decode_workout_summaries(payload)

    async def start_heart_rate_notifications(self) -> None:
        client = self._require_connection()
        if not self._hr_notify_started:
            await client.start_notify(HEART_RATE, self._on_heart_rate_notification)
            self._hr_notify_started = True

    async def start_activity_notifications(
        self,
        *,
        on_raw: Callable[[bytes], None] | None = None,
        on_decode_error: Callable[[bytes, Exception], None] | None = None,
    ) -> None:
        """Subscribe to FEE1 activity notifications.

        ``on_raw`` is called with the undecoded payload of every notification
        as it arrives; ``on_decode_error`` is called when the payload does not
        match the expected layout. Without hooks, undecodable payloads are
        dropped silently (historic behavior). Hooks are bound when the
        subscription is first started and survive deduplicated re-calls.
        """
        client = self._require_connection()
        if not self._activity_notify_started:
            self._activity_raw_hook = on_raw
            self._activity_decode_error_hook = on_decode_error
            await client.start_notify(FEE1, self._on_activity_notification)
            self._activity_notify_started = True

    async def start_step_notifications(
        self,
        *,
        on_raw: Callable[[bytes], None] | None = None,
        on_decode_error: Callable[[bytes, Exception], None] | None = None,
    ) -> None:
        """Subscribe to FEA1 compact step-count notifications (see
        ``start_activity_notifications`` for the hook semantics)."""
        client = self._require_connection()
        if not self._step_notify_started:
            self._step_raw_hook = on_raw
            self._step_decode_error_hook = on_decode_error
            await client.start_notify(FEA1, self._on_step_notification)
            self._step_notify_started = True

    def _on_activity_notification(self, sender: Any, data: bytearray) -> None:
        payload = bytes(data)
        if self._activity_raw_hook is not None:
            self._activity_raw_hook(payload)
        try:
            value = decoders.decode_live_activity(payload)
        except Exception as exc:
            if self._activity_decode_error_hook is not None:
                self._activity_decode_error_hook(payload, exc)
            return
        self._activity_queue.put_nowait(value)

    def _on_step_notification(self, sender: Any, data: bytearray) -> None:
        payload = bytes(data)
        if self._step_raw_hook is not None:
            self._step_raw_hook(payload)
        try:
            value = decoders.decode_compact_step_count(payload)
        except Exception as exc:
            if self._step_decode_error_hook is not None:
                self._step_decode_error_hook(payload, exc)
            return
        self._step_queue.put_nowait(value)

    def _on_heart_rate_notification(self, sender: Any, data: bytearray) -> None:
        try:
            value = decoders.decode_heart_rate_measurement(bytes(data))
        except Exception:
            return
        self._heart_rate_queue.put_nowait(value)

    async def heart_rate_stream(self) -> AsyncIterator[HeartRateMeasurement]:
        await self.start_heart_rate_notifications()
        while self.is_connected:
            yield await self._heart_rate_queue.get()

    async def activity_stream(self) -> AsyncIterator[LiveActivity]:
        await self.start_activity_notifications()
        while self.is_connected:
            yield await self._activity_queue.get()

    async def step_count_stream(self) -> AsyncIterator[StepCount]:
        await self.start_step_notifications()
        while self.is_connected:
            yield await self._step_queue.get()
