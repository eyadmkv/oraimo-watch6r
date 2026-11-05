
import asyncio
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from datetime import datetime, timedelta, timezone

FEE2 = "0000fee2-0000-1000-8000-00805f9b34fb"
FEE3 = "0000fee3-0000-1000-8000-00805f9b34fb"

BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
MANUFACTURER = "00002a29-0000-1000-8000-00805f9b34fb"
FIRMWARE = "00002a26-0000-1000-8000-00805f9b34fb"
SOFTWARE = "00002a28-0000-1000-8000-00805f9b34fb"

QUERY_TIMEOUT = 2.5


@dataclass
class Query:
    name: str
    command: int
    payload: bytes = b""
    timeout: float = QUERY_TIMEOUT


QUERIES = [
    Query("Active watch face", 41),
    Query("Watch face layout", 57, timeout=4),
    Query("Time format", 39),
    Query("Measurement units", 42),
    Query("Device language", 43),
    Query("Device version", 46),
    Query("Daily step goal", 38),
    Query("Alarm configuration", 33, timeout=4),
    Query("Dominant hand", 36),
    Query("Quick view", 40),
    Query("Sedentary reminder", 45),
    Query("Do not disturb schedule", 129),
    Query("Quick view schedule", 130),
    Query("Move reminder configuration", 131),
    Query("Power saving", 164),
    Query("Breathing light", 136),
    Query("Today's sleep", 50, timeout=4),
    Query("Yesterday's sleep", 51, b"\x03", timeout=4),
    Query("Day-before-yesterday sleep", 51, b"\x04", timeout=4),
    Query("Today's hourly steps", 89, b"\x00", timeout=4),
    Query("Yesterday's steps", 51, b"\x01", timeout=4),
    Query("Day-before-yesterday steps", 51, b"\x02", timeout=4),
]
QUERIES.extend([
    Query(
        "One-minute HR history block 0",
        54,
        b"\x00",
        timeout=4
    ),
    Query(
        "Recent workout summaries",
        55,
        timeout=5
    ),
])
for index in range(8):
    QUERIES.append(
        Query(
            f"Heart-rate history block {index}",
            53,
            bytes([index]),
            timeout=3
        )
    )


def build_packet(command, payload=b""):
    total = 5 + len(payload)

    return bytes([
        0xFE,
        0xEA,
        0x20 + (total >> 8),
        total & 0xFF,
        command
    ]) + payload


def packet_length(data):
    if len(data) < 4 or data[:2] != b"\xFE\xEA":
        return -1

    if data[2] == 0x10:
        high = 0
    elif data[2] >= 0x20:
        high = data[2] - 0x20
    else:
        return -1

    return (high << 8) | data[3]


class Reassembler:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, fragment):
        self.buffer.extend(fragment)
        packets = []

        while len(self.buffer) >= 4:
            if self.buffer[:2] != b"\xFE\xEA":
                del self.buffer[0]
                continue

            total = packet_length(self.buffer)

            if total < 5:
                del self.buffer[0]
                continue

            if len(self.buffer) < total:
                break

            raw = bytes(self.buffer[:total])
            del self.buffer[:total]

            packets.append({
                "command": raw[4],
                "payload": raw[5:],
                "raw": raw
            })

        return packets


WATCH_TZ = timezone(timedelta(hours=8))

WORKOUT_TYPES = {
    0: "walk",
    1: "run",
    2: "biking",
    3: "rope",
    4: "badminton",
    5: "basketball",
    6: "football",
    7: "swim",
    8: "mountaineering",
    9: "tennis",
    10: "rugby",
    11: "golf",
    12: "yoga",
    13: "fitness",
    14: "dancing",
    15: "baseball",
    16: "elliptical",
    17: "indoor cycling",
    18: "free exercise",
    19: "rowing machine",
}


def format_watch_time(epoch):
    return datetime.fromtimestamp(
        epoch,
        WATCH_TZ
    ).strftime("%Y-%m-%d %H:%M:%S")


def matches_query(query, response):
    if response["command"] != query.command:
        return False

    # These responses echo the requested history index.
    if query.command in (51, 53, 89) and query.payload:
        payload = response["payload"]

        return (
            bool(payload)
            and payload[0] == query.payload[0]
        )

    return True

def decode_response(query, payload):
    command = query.command

    # Today's and historical sleep
    if command == 50 or (
        command == 51 and query.payload in (b"\x03", b"\x04")
    ):
        if command == 51:
            if not payload:
                print("  Empty sleep response.")
                return

            payload = payload[1:]

        if not payload:
            print("  No recorded sleep stages.")
            return

        if len(payload) % 3:
            print(f"  Unexpected sleep payload: {payload.hex(' ')}")
            return

        stages = {
            0: "Awake",
            1: "Light",
            2: "Deep",
            3: "REM"
        }

        print("  Sleep stages:")

        for i in range(0, len(payload), 3):
            stage, hour, minute = payload[i:i + 3]

            print(
                f"    {stages.get(stage, 'Unknown')} "
                f"at {hour:02}:{minute:02}"
            )

    # Yesterday's and day-before-yesterday's steps
    elif command == 51 and query.payload in (b"\x01", b"\x02"):
        if len(payload) < 10:
            print(f"  Incomplete historical data: {payload.hex(' ')}")
            return

        index = payload[0]
        data = payload[1:10]

        distance = int.from_bytes(data[0:3], "little")
        steps = int.from_bytes(data[3:6], "little")
        calories = int.from_bytes(data[6:9], "little")

        expected_index = query.payload[0]

        if index != expected_index:
            print(
                f"  Warning: expected index {expected_index}, "
                f"received {index}"
            )

        print(f"  Day index: {index}")
        print(f"  Steps: {steps:,}")
        print(f"  Distance: {distance:,} m")
        print(f"  Calories: {calories} kcal")

    # Historical heart rate
    elif command == 53:
        if not payload:
            print("  Empty HR history.")
            return

        index = payload[0]
        samples = list(payload[1:])
        valid = [value for value in samples if value != 0]

        print(f"  History index: {index}")
        print(f"  Samples received: {len(samples)}")
        print(f"  Valid samples: {len(valid)}")

        if valid:
            print(f"  Minimum BPM: {min(valid)}")
            print(f"  Maximum BPM: {max(valid)}")
            print(f"  Average BPM: {sum(valid) / len(valid):.1f}")

        print(f"  Raw samples: {samples}")

    # Hourly activity
    elif command == 89:
        if not payload:
            print("  Empty hourly-step response.")
            return

        index = payload[0]
        body = payload[1:]

        buckets = [
            int.from_bytes(body[i:i + 2], "little")
            for i in range(0, len(body) - 1, 2)
        ]

        print(f"  Day index: {index}")
        print(f"  Hourly buckets: {buckets}")

    # Active watch face
    elif command == 41:
        if payload:
            print(f"  Active watch-face index: {payload[0]}")
        # Recent workout summaries
    elif command == 55:
        record_count = len(payload) // 24
        trailing = len(payload) % 24
        found = 0

        for i in range(record_count):
            entry = payload[i * 24:(i + 1) * 24]

            if not any(entry):
                continue

            found += 1

            start = int.from_bytes(entry[0:4], "little")
            end = int.from_bytes(entry[4:8], "little")

            valid_time = int.from_bytes(entry[8:10], "little")
            entry_number = entry[10]

            workout_type = entry[11]
            workout_name = WORKOUT_TYPES.get(
                workout_type,
                f"Unknown ({workout_type})"
            )

            steps = int.from_bytes(entry[12:16], "little")
            distance = int.from_bytes(entry[16:20], "little")
            calories = int.from_bytes(entry[20:22], "little")

            print(f"  Workout {entry_number}: {workout_name}")
            print(f"    Start: {format_watch_time(start)}")
            print(f"    End: {format_watch_time(end)}")
            print(f"    Valid time: {valid_time:,} seconds")
            print(f"    Steps: {steps:,}")
            print(f"    Distance: {distance:,} m")
            print(f"    Calories: {calories} kcal")

        print(f"  Workout records found: {found}")

        if trailing:
            print(
                f"  Unparsed trailing bytes ({trailing}): "
                f"{payload[-trailing:].hex(' ').upper()}"
            )
    # Other historical data
    elif command == 51:
        print(f"  Historical data: {payload.hex(' ').upper()}")

    else:
        print(f"  Raw response: {payload.hex(' ').upper()}")


async def main():
    print("Searching for Oraimo Watch 6R...")

    devices = await BleakScanner.discover(timeout=15)

    watches = [
        d for d in devices
        if d.name and "oraimo watch 6r" in d.name.lower()
    ]

    if not watches:
        print("Watch not found.")
        return

    for i, watch in enumerate(watches):
        print(f"[{i}] {watch.name} | {watch.address}")

    try:
        index = int(input("\nSelect watch: "))
        watch = watches[index]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return

    notifications = asyncio.Queue()
    reassembler = Reassembler()

    def on_notification(sender, data):
        notifications.put_nowait(bytes(data))

    print("\nConnecting...")

    async with BleakClient(watch, timeout=20) as client:
        print("Connected:", client.is_connected)

        for uuid, label in [
            (MANUFACTURER, "Manufacturer"),
            (FIRMWARE, "Firmware"),
            (SOFTWARE, "Software"),
            (BATTERY, "Battery")
        ]:
            try:
                value = bytes(await client.read_gatt_char(uuid))

                if label == "Battery":
                    print(f"{label}: {value[0]}%")
                else:
                    print(f"{label}: {value.decode(errors='replace')}")
            except Exception as error:
                print(f"{label}: {error}")

        manufacturer = bytes(
            await client.read_gatt_char(MANUFACTURER)
        ).decode(errors="replace").strip("\x00")

        if manufacturer != "MOYOUNG-V2":
            print("Unexpected protocol version. Aborting.")
            return

        await client.start_notify(FEE3, on_notification)

        print("\nMOYOUNG-V2 verified.")
        print(f"Starting {len(QUERIES)} read-only queries.\n")

        for number, query in enumerate(QUERIES, 1):


            packet = build_packet(query.command, query.payload)

            print("=" * 65)
            print(f"[{number}/{len(QUERIES)}] {query.name}")
            print(f"[TX] {packet.hex(' ').upper()}")

            try:
                await client.write_gatt_char(
                    FEE2,
                    packet,
                    response=False
                )
            except Exception as error:
                print(f"[WRITE ERROR] {error}")
                continue

            deadline = asyncio.get_running_loop().time() + query.timeout
            matched = False

            while True:
                remaining = deadline - asyncio.get_running_loop().time()

                if remaining <= 0:
                    break

                try:
                    fragment = await asyncio.wait_for(
                        notifications.get(),
                        timeout=remaining
                    )
                except asyncio.TimeoutError:
                    break

                for response in reassembler.feed(fragment):
                    command = response["command"]
                    payload = response["payload"]

                    print(
                        f"[RX] CMD=0x{command:02X} "
                        f"DATA={payload.hex(' ').upper()}"
                    )

                    if matches_query(query, response):
                        matched = True
                        decode_response(query, payload)
                    else:
                        print("  Unsolicited/late response.")

            if not matched:
                print("[NO MATCHING RESPONSE]")

            await asyncio.sleep(0.3)

        print("\n" + "=" * 65)
        print("Diagnostic finished.")
        print("Disconnecting...")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
