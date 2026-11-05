
import asyncio
from bleak import BleakClient, BleakScanner

FEE3 = "0000fee3-0000-1000-8000-00805f9b34fb"
FEE1 = "0000fee1-0000-1000-8000-00805f9b34fb"
FEA1 = "0000fea1-0000-1000-8000-00805f9b34fb"
HR = "00002a37-0000-1000-8000-00805f9b34fb"


def handler(sender, data):
    print(f"[{sender.uuid}] {bytes(data).hex(' ').upper()}")


async def main():
    print("Scanning...")

    devices = await BleakScanner.discover(timeout=15)

    watch = next(
        (
            d for d in devices
            if d.name and "oraimo watch 6r" in d.name.lower()
        ),
        None
    )

    if not watch:
        print("Watch not found.")
        return

    async with BleakClient(watch) as client:
        print("Connected:", client.is_connected)

        for uuid in [FEE3, FEE1, FEA1, HR]:
            await client.start_notify(uuid, handler)

        print("\nPASSIVE MODE")
        print("Open the blood-pressure screen on your watch.")
        print("Start its measurement manually.")
        print("No commands will be transmitted from the PC.")

        await asyncio.sleep(90)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDisconnected.")
