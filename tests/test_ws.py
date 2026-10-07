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


class HandshakeTest(unittest.TestCase):
    def test_request_matches_what_the_relay_accepts(self):
        # wsprobe.py handshake, live-verified (docs/watch-together.md §12)
        req = ws.handshake_request("pop-fra00.syncplay.plex.services",
                                   "dGhlIHNhbXBsZSBub25jZQ==")
        text = req.decode()
        self.assertTrue(text.startswith("GET /ws HTTP/1.1\r\n"))
        self.assertIn("Host: pop-fra00.syncplay.plex.services\r\n", text)
        self.assertIn("Upgrade: websocket\r\n", text)
        self.assertIn("Connection: Upgrade\r\n", text)
        self.assertIn("Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n", text)
        self.assertIn("Sec-WebSocket-Version: 13\r\n", text)
        self.assertIn("Origin: https://app.plex.tv\r\n", text)
        self.assertTrue(text.endswith("\r\n\r\n"))

    def test_new_key_is_valid_base64_of_16_bytes(self):
        import base64
        key = ws.new_key()
        self.assertEqual(len(base64.b64decode(key)), 16)

    def test_accepts_101(self):
        head = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
        self.assertEqual(ws.handshake_response_status(head), 101)

    def test_rejects_non_101(self):
        head = b"HTTP/1.1 400 Bad Request\r\n"
        with self.assertRaises(ws.HandshakeError) as ctx:
            ws.handshake_response_status(head)
        self.assertIn("400", str(ctx.exception))

    def test_rejects_garbage(self):
        with self.assertRaises(ws.HandshakeError):
            ws.handshake_response_status(b"not http at all")


if __name__ == "__main__":
    unittest.main()
