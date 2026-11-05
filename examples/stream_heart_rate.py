import asyncio

from oraimo_watch6r import WatchClient


async def main() -> None:
    devices = await WatchClient.scan(timeout=12)
    if not devices:
        print("Oraimo Watch 6R not found.")
        return

    async with WatchClient(devices[0].address, name=devices[0].name) as watch:
        print("Listening for heart-rate notifications. Press Ctrl+C to stop.")
        try:
            async for measurement in watch.heart_rate_stream():
                print(f"{measurement.bpm} BPM")
        except asyncio.CancelledError:
            raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
