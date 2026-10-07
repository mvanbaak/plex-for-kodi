# coding=utf-8
"""Tests for lib/ws.py — RFC6455 codec (docs/watch-together.md §5)."""

from __future__ import absolute_import

import unittest

from lib import ws


def server_frame(payload, opcode=0x1):
    """Build a server->client frame (never masked, per RFC6455)."""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    n = len(data)
    if n < 126:
        header = bytes([0x80 | opcode, n])
    elif n < 1 << 16:
        header = bytes([0x80 | opcode, 126]) + __import__("struct").pack(">H", n)
    else:
        header = bytes([0x80 | opcode, 127]) + __import__("struct").pack(">Q", n)
    return header + data


class CodecTest(unittest.TestCase):
    def roundtrip(self, payload, opcode=0x1):
        decoder = ws.FrameDecoder()
        out = decoder.feed(ws.encode_frame(payload, opcode))
        self.assertEqual(out, [(opcode, payload.encode() if isinstance(payload, str) else payload)])

    def test_roundtrip_small_payload(self):
        self.roundtrip('{"Hello":{}}')

    def test_roundtrip_125_bytes(self):
        self.roundtrip("x" * 125)

    def test_roundtrip_126_bytes_needs_extended_length(self):
        self.roundtrip("x" * 126)

    def test_roundtrip_64k_boundary_needs_8byte_length(self):
        self.roundtrip("y" * 65536)

    def test_roundtrip_binary_payload(self):
        decoder = ws.FrameDecoder()
        blob = bytes(range(256)) * 4
        out = decoder.feed(ws.encode_frame(blob, 0x2))
        self.assertEqual(out, [(0x2, blob)])

    def test_partial_feeds_reassemble(self):
        frame = ws.encode_frame('{"List":{}}')
        decoder = ws.FrameDecoder()
        collected = []
        for i in range(len(frame)):
            collected.extend(decoder.feed(frame[i:i + 1]))
        self.assertEqual(collected, [(0x1, b'{"List":{}}')])

    def test_unmasked_server_frame(self):
        decoder = ws.FrameDecoder()
        out = decoder.feed(server_frame('{"State":{}}'))
        self.assertEqual(out, [(0x1, b'{"State":{}}')])

    def test_ping_and_close_opcodes(self):
        decoder = ws.FrameDecoder()
        out = decoder.feed(server_frame(b"pong-me", 0x9))
        out += decoder.feed(server_frame(b"\x03\xe8", 0x8))
        self.assertEqual(out, [(0x9, b"pong-me"), (0x8, b"\x03\xe8")])

    def test_two_frames_in_one_feed(self):
        decoder = ws.FrameDecoder()
        out = decoder.feed(server_frame("one") + server_frame("two"))
        self.assertEqual(out, [(0x1, b"one"), (0x1, b"two")])

    def test_incomplete_frame_yields_nothing_and_stores_it(self):
        decoder = ws.FrameDecoder()
        frame = ws.encode_frame("hello world this is longer than six bytes")
        self.assertEqual(decoder.feed(frame[:3]), [])
        out = decoder.feed(frame[3:])
        self.assertEqual(out, [(0x1, b"hello world this is longer than six bytes")])

    def test_client_frames_are_masked(self):
        raw = ws.encode_frame("abc")
        self.assertTrue(raw[1] & 0x80, "client frames MUST be masked")

    def test_large_length_frames_encode_mask_correctly(self):
        payload = "z" * 70000
        decoder = ws.FrameDecoder()
        out = decoder.feed(ws.encode_frame(payload))
        self.assertEqual(out, [(0x1, payload.encode())])


if __name__ == "__main__":
    unittest.main()
