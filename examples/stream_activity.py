"""Stream live activity notifications from the Oraimo Watch 6R.

Diagnostic build: prints raw notification bytes the moment they arrive
(before decoding), reports the result of each subscription, marks payloads
rejected by the decoders instead of silently dropping them, and prints a
30-second summary of what each characteristic received. It keeps listening
after the summary until Ctrl+C.

Characteristics:

- FEA1, compact step count: 4 bytes where byte 0 is an event marker
  (observed as 0x01 on the Watch 6R) and bytes 1-3 are a little-endian
  uint24 step count. Observed example: 01 04 1A 00 -> 6,660 steps.
- FEE1, full activity: three little-endian uint24 fields in the order
  steps, distance (metres), calories (kcal), followed by unknown trailing
  bytes that are preserved verbatim. Observed example:
  CB 19 00 D4 12 00 21 01 00 22 00 00 -> 6,603 steps, 4,820 m, 289 kcal,
  trailing bytes 22 00 00.

Both layouts are interpretations of live captures from this specific watch
running MOYOUNG-V2 firmware: they are device-specific, subject to further
confirmation, and decoding is delegated to the SDK's shared decoders. Any
payload that does not match these layouts is printed raw and marked as
rejected rather than discarded.
"""

import asyncio
from datetime import datetime

from oraimo_watch6r import LiveActivity, StepCount, WatchClient

SUMMARY_INTERVAL_SECONDS = 30


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class CharacteristicStats:
    def __init__(self, label: str) -> None:
        self.label = label
        self.subscribed = False
        self.subscribe_error: str | None = None
        self.received = 0
        self.decoded = 0
        self.rejected = 0

    def summary_line(self) -> str:
        if self.subscribe_error is not None:
            return f"{self.label}: SUBSCRIPTION FAILED ({self.subscribe_error})"
        return (
            f"{self.label}: subscribed, {self.received} notification(s) received, "
            f"{self.decoded} decoded, {self.rejected} rejected by decoder"
        )


def format_step_update(update: StepCount) -> str:
    return f"[STEPS] {update.steps:,}"


def format_activity_update(update: LiveActivity) -> str:
    unknown = update.trailing_bytes.hex(" ").upper() or "(none)"
    return (
        f"[ACTIVITY] Steps: {update.steps:,} | "
        f"Distance: {update.distance_m / 1000:.3f} km | "
        f"Calories: {update.calories_kcal} kcal | Unknown: {unknown}"
    )


async def report_steps(watch: WatchClient, stats: CharacteristicStats) -> None:
    async for update in watch.step_count_stream():
        stats.decoded += 1
        print(f"[{_timestamp()}] {format_step_update(update)}")


async def report_activity(watch: WatchClient, stats: CharacteristicStats) -> None:
    async for update in watch.activity_stream():
        stats.decoded += 1
        print(f"[{_timestamp()}] {format_activity_update(update)}")


async def report_summary(stats: tuple[CharacteristicStats, ...]) -> None:
    await asyncio.sleep(SUMMARY_INTERVAL_SECONDS)
    print()
    print(f"[{_timestamp()}] {SUMMARY_INTERVAL_SECONDS}-SECOND DIAGNOSTIC SUMMARY")
    for item in stats:
        print(f"  {item.summary_line()}")
    print("Still listening. Press Ctrl+C to stop.")
    print()
    # Stay alive so the summary task never completes and stops the consumers.
    await asyncio.Event().wait()


def make_raw_handler(stats: CharacteristicStats, tag: str):
    def handler(payload: bytes) -> None:
        stats.received += 1
        print(f"[{_timestamp()}] [RAW {tag}] {payload.hex(' ').upper()}")
    return handler


def make_decode_error_handler(stats: CharacteristicStats, tag: str):
    def handler(payload: bytes, exc: Exception) -> None:
        stats.rejected += 1
        print(f"[{_timestamp()}] [REJECTED {tag}] {payload.hex(' ').upper()} -> {exc}")
    return handler


async def subscribe(
    watch: WatchClient,
    stats: CharacteristicStats,
    tag: str,
    starter,
) -> bool:
    try:
        await starter(
            on_raw=make_raw_handler(stats, tag),
            on_decode_error=make_decode_error_handler(stats, tag),
        )
    except Exception as exc:
        stats.subscribe_error = str(exc)
        print(f"[{_timestamp()}] [SUBSCRIBE FAILED] {tag}: {exc}")
        return False
    stats.subscribed = True
    print(f"[{_timestamp()}] [SUBSCRIBED] {tag}")
    return True


async def main() -> None:
    devices = await WatchClient.scan(timeout=12)
    if not devices:
        print("Oraimo Watch 6R not found.")
        return

    # Pass the WatchDevice itself so connection reuses the BLEDevice captured
    # during the scan instead of re-discovering by address on Windows.
    async with WatchClient(devices[0]) as watch:
        print("Oraimo Watch 6R connected.")
        print("Listening for activity notifications. Press Ctrl+C to stop.")
        print()

        fee1 = CharacteristicStats("FEE1 (full activity)")
        fea1 = CharacteristicStats("FEA1 (compact steps)")

        # Each subscription is independent; WatchClient deduplicates the
        # underlying start_notify calls, so the streams started below attach
        # to these same subscriptions without re-subscribing.
        activity_ok = await subscribe(watch, fee1, "FEE1", watch.start_activity_notifications)
        steps_ok = await subscribe(watch, fea1, "FEA1", watch.start_step_notifications)

        tasks = []
        if activity_ok:
            tasks.append(asyncio.create_task(report_activity(watch, fee1), name="activity"))
        if steps_ok:
            tasks.append(asyncio.create_task(report_steps(watch, fea1), name="steps"))
        tasks.append(asyncio.create_task(report_summary((fee1, fea1)), name="summary"))

        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task.get_name() != "summary" and not task.cancelled() and task.exception() is not None:
                    print(f"Notification stream ended with error: {task.exception()}")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
