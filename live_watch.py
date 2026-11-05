
import asyncio
from bleak import BleakClient, BleakScanner

HR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

STEPS_UUID = "0000fee1-0000-1000-8000-00805f9b34fb"
EXTRA_UUID = "0000fea1-0000-1000-8000-00805f9b34fb"


def uint24(data, offset):
    return int.from_bytes(data[offset:offset + 3], "little")


def handle_packet(uuid, data):
    uuid = uuid.lower()
    data = bytes(data)

    if not data:
        return

    if uuid == BATTERY_UUID:
        print(f"[BATTERY] {data[0]}%")

    elif uuid == HR_UUID:
        if len(data) < 2:
            return

        flags = data[0]

        if flags & 0x01:
            if len(data) < 3:
                return
            bpm = int.from_bytes(data[1:3], "little")
        else:
            bpm = data[1]

        print(f"[HEART RATE] {bpm} BPM")

    elif uuid == STEPS_UUID:
        if len(data) < 9:
            print(f"[STEPS RAW] {data.hex(' ').upper()}")
            return

        steps = uint24(data, 0)
        distance = uint24(data, 3)
        calories = uint24(data, 6)

        extra = uint24(data, 9) if len(data) >= 12 else None

        print(
            f"[ACTIVITY] Steps: {steps:,} | "
            f"Distance: {distance / 1000:.3f} km | "
            f"Calories: {calories} kcal | "
            f"Extra: {extra}"
        )

    elif uuid == EXTRA_UUID:
        if len(data) == 4 and data[0] == 0x01:
            steps = int.from_bytes(data[1:4], "little")
            print(f"[ACTIVITY EVENT] Steps: {steps:,}")
        else:
            print(f"[EXTRA RAW] {data.hex(' ').upper()}")


def handle_notification(sender, data):
    handle_packet(sender.uuid, data)


async def main():
    print("Searching for Oraimo Watch 6R...")

    devices = await BleakScanner.discover(timeout=15)

    watch = next(
        (
            device for device in devices
            if device.name
            and device.name.lower().startswith("oraimo watch 6r")
        ),
        None
    )

    if watch is None:
        print("Watch not found.")
        return

    print(f"Found: {watch.name} ({watch.address})")

    async with BleakClient(watch, timeout=20) as client:
        print("Connected:", client.is_connected)

        characteristics = [
            BATTERY_UUID,
            STEPS_UUID,
            HR_UUID,
            EXTRA_UUID,
        ]

        print("\n========== SUBSCRIPTIONS ==========")

        for uuid in characteristics:
            try:
                await client.start_notify(
                    uuid,
                    handle_notification
                )
                print(f"Subscribed: {uuid}")
            except Exception as error:
                print(f"Subscription failed: {uuid}: {error}")

        print("\n========== INITIAL READINGS ==========")

        for uuid in [BATTERY_UUID, STEPS_UUID]:
            try:
                data = await client.read_gatt_char(uuid)
                handle_packet(uuid, data)
            except Exception as error:
                print(f"Read failed ({uuid}): {error}")

        print("\nMonitoring. Press Ctrl+C to disconnect.")

        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDisconnected.")
