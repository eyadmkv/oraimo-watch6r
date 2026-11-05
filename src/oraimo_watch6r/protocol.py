from __future__ import annotations

from dataclasses import dataclass

from .exceptions import ProtocolError

PACKET_PREFIX = b"\xFE\xEA"
HEADER_SIZE = 5
DEFAULT_MAX_PACKET_SIZE = 4096


@dataclass(frozen=True, slots=True)
class ProtocolPacket:
    command: int
    payload: bytes
    raw: bytes


def build_packet(command: int, payload: bytes = b"", *, mtu: int = 247) -> bytes:
    if not 0 <= command <= 0xFF:
        raise ValueError("command must be between 0 and 255")
    if mtu < 20:
        raise ValueError("mtu must be at least 20")

    payload = bytes(payload)
    total = HEADER_SIZE + len(payload)
    if total > DEFAULT_MAX_PACKET_SIZE:
        raise ValueError(f"packet exceeds maximum size ({DEFAULT_MAX_PACKET_SIZE})")

    high = 0x10 if mtu == 20 else (0x20 + (total >> 8)) & 0xFF
    return PACKET_PREFIX + bytes((high, total & 0xFF, command)) + payload


def packet_length(data: bytes | bytearray | memoryview) -> int:
    view = bytes(data)
    if len(view) < 4 or view[:2] != PACKET_PREFIX:
        return -1

    high_byte = view[2]
    if high_byte == 0x10:
        high = 0
    elif high_byte >= 0x20:
        high = high_byte - 0x20
    else:
        return -1

    return (high << 8) | view[3]


def parse_packet(data: bytes) -> ProtocolPacket:
    total = packet_length(data)
    if total < HEADER_SIZE or total != len(data):
        raise ProtocolError("invalid or incomplete MOYOUNG-V2 packet")
    return ProtocolPacket(command=data[4], payload=data[5:], raw=bytes(data))


class PacketReassembler:
    def __init__(self, *, max_packet_size: int = DEFAULT_MAX_PACKET_SIZE) -> None:
        if max_packet_size < HEADER_SIZE:
            raise ValueError("max_packet_size must be at least 5")
        self._buffer = bytearray()
        self.max_packet_size = max_packet_size

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, fragment: bytes | bytearray | memoryview) -> list[ProtocolPacket]:
        self._buffer.extend(fragment)
        packets: list[ProtocolPacket] = []

        while len(self._buffer) >= 4:
            if self._buffer[:2] != PACKET_PREFIX:
                del self._buffer[0]
                continue

            total = packet_length(self._buffer)
            if total < HEADER_SIZE or total > self.max_packet_size:
                del self._buffer[0]
                continue

            if len(self._buffer) < total:
                break

            raw = bytes(self._buffer[:total])
            del self._buffer[:total]
            packets.append(parse_packet(raw))

        if len(self._buffer) > self.max_packet_size:
            self.reset()
            raise ProtocolError("reassembly buffer exceeded maximum packet size")

        return packets
