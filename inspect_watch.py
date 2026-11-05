
import asyncio
from bleak import BleakScanner, BleakClient

async def main():
    print("Scanning for Bluetooth LE devices...")
    devices = await BleakScanner.discover(timeout=20)

    if not devices:
        print("No BLE devices found.")
        return

    for index, device in enumerate(devices):
        print(f"[{index}] {device.name or 'Unknown'} | {device.address}")

    choice = input("\nSelect your watch's device number: ")

    try:
        target = devices[int(choice)]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return

    print(f"\nConnecting to {target.name}...")

    async with BleakClient(target, timeout=20) as client:
        print(f"Connected: {client.is_connected}")
        print("\nGATT SERVICES AND CHARACTERISTICS\n")

        for service in client.services:
            print(f"SERVICE: {service.uuid}")

            for characteristic in service.characteristics:
                properties = ", ".join(characteristic.properties)

                print(f"  CHARACTERISTIC: {characteristic.uuid}")
                print(f"  HANDLE: {characteristic.handle}")
                print(f"  PROPERTIES: {properties}")

            print()

if __name__ == "__main__":
    asyncio.run(main())
