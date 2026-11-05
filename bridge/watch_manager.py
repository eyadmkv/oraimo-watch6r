"""Asyncio connection manager: keeps a WatchClient connected and feeds the stores.

Read-only by construction — it only ever calls the SDK's scan, read and
notification APIs. The scan function and client class are injectable so the
manager can be exercised without BLE hardware.
"""

from __future__ import annotations

import asyncio
from typing import Any

from oraimo_watch6r.client import FEE3, WatchClient
from oraimo_watch6r.models import LiveActivity, StepCount

from .store import (
    FEA1,
    FEE1,
    CommandStore,
    DiagnosticsStore,
    HistoryStore,
    SnapshotStore,
    bytes_hex,
    timestamp,
)
from oraimo_watch6r.exceptions import WatchNotConnectedError

BACKOFF_SCHEDULE_S = (2.0, 5.0, 10.0, 15.0, 30.0, 60.0)
SCAN_RETRY_S = 15.0
MAINTENANCE_INTERVAL_S = 60.0
HISTORY_REFRESH_CYCLES = 5
FETCH_STEPS_TIMEOUT_S = 10.0


class WatchManager:
    def __init__(
        self,
        snapshot: SnapshotStore,
        history: HistoryStore,
        diagnostics: DiagnosticsStore,
        *,
        scan_fn=None,
        client_class=WatchClient,
        scan_timeout: float = 12.0,
        command_store: CommandStore | None = None,
        fetch_steps_timeout: float = FETCH_STEPS_TIMEOUT_S,
    ) -> None:
        self.snapshot = snapshot
        self.history = history
        self.diagnostics = diagnostics
        self._scan_fn = scan_fn or WatchClient.scan
        self._client_class = client_class
        self._scan_timeout = scan_timeout
        self.commands = command_store or CommandStore()
        self._fetch_steps_timeout = fetch_steps_timeout

        self.state = "stopped"
        self.detail: str | None = None
        self.watch_name: str | None = None
        self.watch_address: str | None = None
        self.last_error: str | None = None
        self.connected_since: dict | None = None
        self.protocol_channel_error: str | None = None

        self._task: asyncio.Task | None = None
        self._command_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._command_queue: asyncio.Queue[str] | None = None
        self._command_idle = asyncio.Event()
        self._command_idle.set()
        self._active_client: WatchClient | None = None
        self._stop = asyncio.Event()
        self._reconnect = asyncio.Event()
        self._last_device: Any | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._loop = asyncio.get_running_loop()
            self._command_queue = asyncio.Queue()
            self._stop.clear()
            self._reconnect.clear()
            self._set_state("scanning", "starting")
            self._command_task = self._loop.create_task(self._run_command_queue())
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        self._reconnect.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._command_task is not None:
            self._command_task.cancel()
            await asyncio.gather(self._command_task, return_exceptions=True)
            self._command_task = None
        self._loop = None
        self._command_queue = None
        self._set_state("stopped")

    def submit_fetch_steps(self) -> dict:
        command = self.commands.create("FETCH_STEPS")
        loop = self._loop
        queue = self._command_queue
        if loop is None or loop.is_closed() or queue is None:
            return self.commands.update(
                command["command_id"],
                "failed",
                error="The bridge command worker is not running.",
            )
        loop.call_soon_threadsafe(queue.put_nowait, command["command_id"])
        return command

    def request_reconnect(self) -> None:
        """Force the current connection attempt (or live connection) to restart."""
        self._reconnect.set()

    def status(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "watch_name": self.watch_name,
            "watch_address": self.watch_address,
            "last_error": self.last_error,
            "connected_since": self.connected_since,
            "protocol_channel_error": self.protocol_channel_error,
        }

    # -- internals -----------------------------------------------------------

    def _set_state(self, state: str, detail: str | None = None) -> None:
        self.state = state
        self.detail = detail
        if state == "connected":
            from .store import timestamp

            self.connected_since = timestamp()
            self.last_error = None
            self.diagnostics.record_connection_event("connected", detail)
        elif state in ("error", "reconnecting", "no-watch-found") and detail:
            self.last_error = detail

    async def _sleep_or_interrupt(self, seconds: float) -> None:
        """Sleep, returning early when stop or reconnect is requested."""
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            if self._stop.is_set() or self._reconnect.is_set():
                return
            await asyncio.sleep(min(0.25, max(0.0, deadline - asyncio.get_running_loop().time())))

    async def _run(self) -> None:
        backoff_index = 0
        while not self._stop.is_set():
            reconnect_requested = self._reconnect.is_set()
            if reconnect_requested:
                self._reconnect.clear()
                backoff_index = 0

            try:
                if reconnect_requested and self._last_device is not None:
                    device = self._last_device
                else:
                    self._set_state("scanning")
                    devices = await self._scan_fn(timeout=self._scan_timeout)
                    if not devices:
                        self._set_state("no-watch-found", "scan found no matching watch")
                        await self._sleep_or_interrupt(SCAN_RETRY_S)
                        continue
                    device = devices[0]
                self._last_device = device

                self._set_state("connecting", device.name)
                client = self._client_class(device)
                await client.connect()
                self._active_client = client
                self.protocol_channel_error = None
                backoff_index = 0
                self.watch_name = device.name
                self.watch_address = device.address
                self._set_state("connected", device.name)

                try:
                    await self._collect_initial(client)
                    await self._run_streams(client)
                finally:
                    self._active_client = None
                    await self._command_idle.wait()
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

                if self._stop.is_set():
                    break
                self._set_state("reconnecting", "connection ended")
                await self._sleep_or_interrupt(BACKOFF_SCHEDULE_S[0])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_state("error", str(exc))
                self.diagnostics.record_connection_event("connection-failed", str(exc))
                delay = BACKOFF_SCHEDULE_S[min(backoff_index, len(BACKOFF_SCHEDULE_S) - 1)]
                backoff_index += 1
                await self._sleep_or_interrupt(delay)

        self._set_state("stopped")

    async def _run_command_queue(self) -> None:
        queue = self._command_queue
        if queue is None:
            return
        while not self._stop.is_set():
            command_id = await queue.get()
            try:
                client = self._active_client
                if client is None or not client.is_connected:
                    self.commands.update(
                        command_id,
                        "failed",
                        error=f"Watch is not connected (bridge state: {self.state}).",
                    )
                    continue

                self.commands.update(command_id, "running")
                self._command_idle.clear()
                try:
                    activity = await asyncio.wait_for(
                        client.read_live_activity(),
                        timeout=self._fetch_steps_timeout,
                    )
                    result = {
                        "steps": activity.steps,
                        "distance_meters": activity.distance_m,
                        "calories_kcal": activity.calories_kcal,
                        "measured_at": timestamp()["iso"],
                        "source_characteristic": FEE1,
                    }
                    self.snapshot.apply_live_activity(activity)
                    self.commands.update(command_id, "succeeded", result=result)
                except asyncio.TimeoutError:
                    self.commands.update(
                        command_id,
                        "failed",
                        error=f"BLE activity read timed out after {self._fetch_steps_timeout:g} seconds.",
                    )
                except WatchNotConnectedError as exc:
                    self.commands.update(command_id, "failed", error=str(exc))
                except Exception as exc:
                    self.commands.update(
                        command_id,
                        "failed",
                        error=f"BLE activity read failed: {exc}",
                    )
                finally:
                    self._command_idle.set()
            finally:
                queue.task_done()

    # -- data collection -----------------------------------------------------

    async def _collect_initial(self, client: WatchClient) -> None:
        guarded = [
            ("device_info", lambda: client.device_info(), self._apply_device_info),
            ("battery", lambda: client.read_battery(), self._apply_battery),
            ("step_goal", lambda: client.get_step_goal(), self._apply_step_goal),
            ("past_activity_1", lambda: client.get_past_activity(1), lambda value: self.history.set_past_activity(1, value)),
            ("past_activity_2", lambda: client.get_past_activity(2), lambda value: self.history.set_past_activity(2, value)),
            ("hourly_activity", lambda: client.get_hourly_activity(0), self.history.set_hourly_activity),
            ("sleep_today", lambda: client.get_today_sleep(), self.history.set_sleep_today),
            (
                "heart_rate_history",
                lambda: self._fetch_heart_rate_history(client),
                self.history.set_heart_rate_history,
            ),
        ]
        for _, query, apply in guarded:
            await self._safe(query, apply)
        self.history.mark_fetched()

    async def _fetch_heart_rate_history(self, client: WatchClient):
        blocks = []
        for index in range(8):
            if self.protocol_channel_error is not None:
                break
            blocks.append(await self._safe_value(client.get_heart_rate_history(index)))
        return [block for block in blocks if block is not None]

    async def _safe(self, query, apply) -> None:
        """Run one query; failures are logged and never break the connection.

        ``query`` is either an awaitable or a zero-arg callable returning one.
        """
        if self.protocol_channel_error is not None:
            return
        try:
            value = await (query() if callable(query) else query)
        except Exception as exc:
            detail = str(exc)
            if FEE3.casefold() in detail.casefold():
                self.protocol_channel_error = detail
                self.diagnostics.record_connection_event(
                    "protocol-channel-unavailable",
                    detail,
                )
            else:
                self.diagnostics.record_connection_event("query-failed", detail)
            return
        try:
            if value is not None:
                apply(value)
        except Exception as exc:
            self.diagnostics.record_connection_event("query-failed", f"apply: {exc}")

    async def _safe_value(self, query):
        try:
            return await query
        except Exception as exc:
            detail = str(exc)
            if FEE3.casefold() in detail.casefold():
                self.protocol_channel_error = detail
                self.diagnostics.record_connection_event(
                    "protocol-channel-unavailable",
                    detail,
                )
            else:
                self.diagnostics.record_connection_event("query-failed", detail)
            return None

    def _apply_device_info(self, info) -> None:
        self.history.set_device_info(info)
        if info.battery_percent is not None:
            self.snapshot.apply_battery(info.battery_percent)

    def _apply_battery(self, battery) -> None:
        self.snapshot.apply_battery(battery.percent)

    def _apply_step_goal(self, goal) -> None:
        self.snapshot.apply_step_goal(goal)
        self.history.set_step_goal(goal)

    def _apply_live_activity(self, activity: LiveActivity) -> None:
        self.snapshot.apply_live_activity(activity)
        self.diagnostics.record_decoded()

    def _apply_step_count(self, value: StepCount) -> None:
        self.snapshot.apply_step_count(value.steps)
        self.diagnostics.record_decoded()

    def _apply_heart_rate(self, measurement) -> None:
        self.snapshot.apply_heart_rate(measurement.bpm)
        self.diagnostics.record_decoded()

    # -- notification streams -------------------------------------------------

    async def _run_streams(self, client: WatchClient) -> None:
        consumers = []
        subscriptions = (
            (
                "activity",
                lambda: client.start_activity_notifications(
                    on_raw=lambda payload: self.diagnostics.record_notification(
                        FEE1, bytes_hex(payload) or "", status="raw"
                    ),
                    on_decode_error=lambda payload, exc: self.diagnostics.record_notification(
                        FEE1, bytes_hex(payload) or "", status="decode-error", error=str(exc)
                    ),
                ),
                lambda: self._pump(client.activity_stream(), self._apply_live_activity),
            ),
            (
                "step count",
                lambda: client.start_step_notifications(
                    on_raw=lambda payload: self.diagnostics.record_notification(
                        FEA1, bytes_hex(payload) or "", status="raw"
                    ),
                    on_decode_error=lambda payload, exc: self.diagnostics.record_notification(
                        FEA1, bytes_hex(payload) or "", status="decode-error", error=str(exc)
                    ),
                ),
                lambda: self._pump(client.step_count_stream(), self._apply_step_count),
            ),
            (
                "heart rate",
                client.start_heart_rate_notifications,
                lambda: self._pump(client.heart_rate_stream(), self._apply_heart_rate),
            ),
        )
        for label, subscribe, make_consumer in subscriptions:
            try:
                await subscribe()
            except Exception as exc:
                self.diagnostics.record_connection_event(
                    "notification-subscribe-failed",
                    f"{label}: {exc}",
                )
                continue
            consumers.append(asyncio.create_task(make_consumer()))

        consumers.append(asyncio.create_task(self._maintenance(client)))
        interrupts = [
            asyncio.create_task(self._stop.wait()),
            asyncio.create_task(self._reconnect.wait()),
        ]
        try:
            done, pending = await asyncio.wait(
                consumers + interrupts, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    raise task.exception()
        finally:
            for task in consumers + interrupts:
                if not task.done():
                    task.cancel()

    async def _pump(self, stream, apply) -> None:
        async for value in stream:
            apply(value)

    async def _maintenance(self, client: WatchClient) -> None:
        """Refresh battery frequently; hourly activity and sleep occasionally."""
        cycle = 0
        while client.is_connected:
            await self._sleep_or_interrupt(MAINTENANCE_INTERVAL_S)
            if self._stop.is_set() or self._reconnect.is_set() or not client.is_connected:
                return
            cycle += 1
            battery = await self._safe_value(client.read_battery())
            if battery is not None:
                self.snapshot.apply_battery(battery.percent)
            if cycle % HISTORY_REFRESH_CYCLES == 0 and self.protocol_channel_error is None:
                hourly = await self._safe_value(client.get_hourly_activity(0))
                if hourly is not None:
                    self.history.set_hourly_activity(hourly)
                if self.protocol_channel_error is None:
                    sleep = await self._safe_value(client.get_today_sleep())
                    self.history.set_sleep_today(sleep)
