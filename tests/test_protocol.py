import unittest

from oraimo_watch6r.protocol import PacketReassembler, build_packet, packet_length, parse_packet


class ProtocolTests(unittest.TestCase):
    def test_packet_uses_mtu_20_header(self):
        packet = build_packet(0x29, mtu=20)
        self.assertEqual(packet, bytes.fromhex("FE EA 10 05 29"))

    def test_packet_uses_v2_header(self):
        packet = build_packet(0x29, b"\x01")
        self.assertEqual(packet, bytes.fromhex("FE EA 20 06 29 01"))
        self.assertEqual(packet_length(packet), 6)

    def test_parse_packet(self):
        packet = build_packet(0x35, b"\x02\x3C\x40")
        parsed = parse_packet(packet)
        self.assertEqual(parsed.command, 0x35)
        self.assertEqual(parsed.payload, b"\x02\x3C\x40")
        self.assertEqual(parsed.raw, packet)

    def test_reassembles_split_packet(self):
        packet = build_packet(0x29, b"\x07")
        reassembler = PacketReassembler()
        self.assertEqual(reassembler.feed(packet[:3]), [])
        result = reassembler.feed(packet[3:])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].command, 0x29)
        self.assertEqual(result[0].payload, b"\x07")

    def test_reassembles_multiple_packets_in_one_fragment(self):
        first = build_packet(0xF9, b"\x02\x00")
        second = build_packet(0x29, b"\x07")
        result = PacketReassembler().feed(first + second)
        self.assertEqual([packet.command for packet in result], [0xF9, 0x29])

    def test_resynchronizes_after_noise(self):
        packet = build_packet(0x29, b"\x07")
        result = PacketReassembler().feed(b"noise" + packet)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].payload, b"\x07")


if __name__ == "__main__":
    unittest.main()
