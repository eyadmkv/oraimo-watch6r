import asyncio

from oraimo_watch6r import WatchClient


async def main() -> None:
    devices = await WatchClient.scan(timeout=12)
    if not devices:
        print("Oraimo Watch 6R not found.")
        return

    for index, device in enumerate(devices):
        print(f"[{index}] {device.name} | {device.address} | RSSI={device.rssi}")

    selected = devices[0]
    async with WatchClient(selected) as watch:
        print("\nDevice information:")
        print(await watch.device_info())

        print("\nStep goal:", await watch.get_step_goal())
        print("Active watch face:", await watch.get_active_watch_face())

        history = await watch.get_heart_rate_history(0)
        if history is None:
            print("Heart-rate history did not respond.")
        else:
            print("\nHeart-rate history:")
            print(f"Valid samples: {len(history.valid_samples)} / {len(history.samples_bpm)}")
            print(f"Range: {history.minimum_bpm}–{history.maximum_bpm} BPM")
            print(f"Average: {history.average_bpm}")

        yesterday = await watch.get_past_activity(1)
        if yesterday is not None:
            print("\nYesterday:")
            print(f"Steps: {yesterday.steps:,}")
            print(f"Distance: {yesterday.distance_m / 1000:.3f} km")
            print(f"Calories: {yesterday.calories_kcal} kcal")
            if yesterday.trailing_bytes:
                print("Undecoded trailing bytes:", yesterday.trailing_bytes.hex(" "))


if __name__ == "__main__":
    asyncio.run(main())
