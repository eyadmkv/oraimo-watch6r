
import asyncio

from bleak import BleakClient, BleakScanner

FEE2 = "0000fee2-0000-1000-8000-00805f9b34fb"
FEE3 = "0000fee3-0000-1000-8000-00805f9b34fb"

HR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


def make_packet(command, payload):
    size = 5 + len(payload)

    return bytes([
        0xFE,
        0xEA,
        0x20 + (size >> 8),
        size & 0xFF,
        command
    ]) + payload


def on_response(sender, data):
    packet = bytes(data)

    print(f"\n[RAW] {packet.hex(' ').upper()}")

    if len(packet) < 5 or packet[:2] != b"\xFE\xEA":
        print("[UNKNOWN] Invalid MOYOUNG packet")
        return

    declared_size = ((packet[2] - 0x20) << 8) | packet[3]

    print(f"[PACKET SIZE] Declared: {declared_size}, Received: {len(packet)}")

    if declared_size < 5 or len(packet) < declared_size:
        print("[WARNING] Incomplete packet")
        return

    command = packet[4]
    payload = packet[5:declared_size]

    print(f"[COMMAND] 0x{command:02X}")
    print(f"[PAYLOAD] {payload.hex(' ').upper()}")

    if command == 0x69:
        if len(payload) >= 3:
            unknown, systolic, diastolic = payload[:3]

            print("\n========== BLOOD PRESSURE ==========")
            print(f"Systolic:  {systolic} mmHg")
            print(f"Diastolic: {diastolic} mmHg")
            print(f"Extra:     {unknown}")
            print("====================================")
        else:
            print("[BP] Incomplete measurement response")

    elif command == 0xB9:
        if payload:
            subcommand = payload[0]

            print("\n========== ADVANCED COMMAND ==========")
            print(f"Subcommand: 0x{subcommand:02X}")
            print(f"Data: {payload[1:].hex(' ').upper()}")
            print("======================================")
        else:
            print("[ADVANCED] Empty payload")

    else:
        print(f"[UNRECOGNIZED COMMAND] 0x{command:02X}")


def on_heart_rate(sender, data):
    data = bytes(data)

    if len(data) < 2:
        return

    bpm = (
        int.from_bytes(data[1:3], "little")
        if data[0] & 0x01 and len(data) >= 3
        else data[1]
    )

    print(f"[HEART RATE] {bpm} BPM")


async def main():
    print("Searching for Oraimo Watch 6R...")

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

    print(f"Found: {watch.name} ({watch.address})")

    async with BleakClient(watch, timeout=20) as client:
        print("Connected:", client.is_connected)

        await client.start_notify(FEE3, on_response)
        await client.start_notify(HR_UUID, on_heart_rate)

        await asyncio.sleep(1)

        start_packet = make_packet(
            0x69,
            bytes([0x00, 0x00, 0x00])
        )

        print("\nStarting blood-pressure measurement...")
        print("[TX]", start_packet.hex(" ").upper())

        await client.write_gatt_char(
            FEE2,
            start_packet,
            response=False
        )

        print("\nListening for 60 seconds...")

        try:
            await asyncio.sleep(60)

        finally:
            stop_packet = make_packet(
                0x69,
                bytes([0xFF, 0xFF, 0xFF])
            )

            try:
                await client.write_gatt_char(
                    FEE2,
                    stop_packet,
                    response=False
                )

                print("\n[TX] Measurement stop request sent.")

            except Exception as error:
                print("[ERROR] Stop request:", error)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDisconnected.")
