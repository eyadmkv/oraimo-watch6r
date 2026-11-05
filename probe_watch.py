
import asyncio

from bleak import BleakClient, BleakScanner

READ_UUIDS = {
    "00002a19-0000-1000-8000-00805f9b34fb": "BATTERY",
    "00002a26-0000-1000-8000-00805f9b34fb": "FIRMWARE",
    "00002a28-0000-1000-8000-00805f9b34fb": "SOFTWARE",
    "00002a29-0000-1000-8000-00805f9b34fb": "MANUFACTURER",
    "0000fee1-0000-1000-8000-00805f9b34fb": "STEPS",
    "0000fee4-0000-1000-8000-00805f9b34fb": "FEE4_DATA",
    "0000fec9-0000-1000-8000-00805f9b34fb": "FEC9_DATA",
    "0000fea1-0000-1000-8000-00805f9b34fb": "FEA1_DATA",
}

NOTIFY_UUIDS = {
    "00002a19-0000-1000-8000-00805f9b34fb",
    "00002a37-0000-1000-8000-00805f9b34fb",
    "0000fee1-0000-1000-8000-00805f9b34fb",
    "0000fee3-0000-1000-8000-00805f9b34fb",
    "0000fea1-0000-1000-8000-00805f9b34fb",
}


def show_value(name, value):
    print(f"\n{name}")
    print(f"  HEX: {value.hex(' ').upper()}")

    decoded = value.decode("utf-8", errors="replace").strip("\x00")
    if decoded and decoded.isprintable():
        print(f"  TEXT: {decoded}")

    if name == "BATTERY" and value:
        print(f"  LEVEL: {value[0]}%")


def notification_handler(sender, data):
    print(
        f"\n[NOTIFICATION] {sender.uuid}\n"
        f"  HEX: {bytes(data).hex(' ').upper()}"
    )


async def main():
    print("Searching for Oraimo Watch 6R...")
    devices = await BleakScanner.discover(timeout=15)

    matches = [
        device for device in devices
        if device.name and "oraimo watch 6r" in device.name.lower()
    ]

    if not matches:
        print("Watch not found. Make sure it is nearby.")
        return

    for i, device in enumerate(matches):
        print(f"[{i}] {device.name} | {device.address}")

    choice = int(input("\nSelect device: "))
    target = matches[choice]

    async with BleakClient(target, timeout=20) as client:
        print("\nConnected:", client.is_connected)

        print("\n========== READ CHARACTERISTICS ==========")

        for service in client.services:
            for char in service.characteristics:
                uuid = char.uuid.lower()

                if uuid not in READ_UUIDS:
                    continue

                if "read" not in char.properties:
                    continue

                name = READ_UUIDS[uuid]

                try:
                    value = await client.read_gatt_char(char)
                    show_value(name, bytes(value))
                except Exception as error:
                    print(f"\n{name}: READ FAILED: {error}")

        print("\n========== SUBSCRIBING TO NOTIFICATIONS ==========")

        subscribed = []

        for service in client.services:
            for char in service.characteristics:
                uuid = char.uuid.lower()

                if uuid not in NOTIFY_UUIDS:
                    continue

                if "notify" not in char.properties:
                    continue

                try:
                    await client.start_notify(
                        char,
                        notification_handler
                    )
                    subscribed.append(char)
                    print(f"Subscribed: {uuid}")
                except Exception as error:
                    print(f"Subscription failed: {uuid}: {error}")

        print("\n========== MONITORING FOR 30 SECONDS ==========")
        print("Walk a little and check your watch's step counter.")
        print("You can also manually open its heart-rate measurement.")
        print("No commands are being sent to the watch.")

        await asyncio.sleep(30)

        for char in subscribed:
            try:
                await client.stop_notify(char)
            except Exception:
                pass

        print("\nFinished.")


if __name__ == "__main__":
    asyncio.run(main())
