# coding=utf-8
"""Minimal RFC6455 WebSocket client — generic, no Plex and no Kodi.

Just enough of the spec for the Syncplay relay (docs/watch-together.md §5):
text frames, ping/pong, close, client-side masking. No fragmentation, no
extensions, no compression — the relay uses none of them.
"""

from __future__ import absolute_import

import base64
import json
import os
import socket
import ssl
import struct
import threading


class HandshakeError(Exception):
    pass


def encode_frame(payload, opcode=0x1):
    """Build a client->server frame (always masked, as the RFC requires)."""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    n = len(data)
    if n < 126:
        header = bytes([0x80 | opcode, 0x80 | n])
    elif n < 1 << 16:
        header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", n)
    else:
        header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    return header + mask + bytes(c ^ k for c, k in zip(data, mask * n))


class FrameDecoder(object):
    """Incremental frame decoder: feed() bytes, take (opcode, payload) tuples.

    Handles masked and unmasked frames — we always mask, the relay never does.
    """

    def __init__(self):
        self._buf = b""

    def feed(self, data):
        self._buf += data
        out = []
        while True:
            frame = self._one()
            if frame is None:
                return out
            out.append(frame)

    def _one(self):
        buf = self._buf
        if len(buf) < 2:
            return None
        opcode = buf[0] & 0x0F
        masked = bool(buf[1] & 0x80)
        n = buf[1] & 0x7F
        off = 2
        if n == 126:
            if len(buf) < 4:
                return None
            n = struct.unpack(">H", buf[2:4])[0]
            off = 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n = struct.unpack(">Q", buf[2:10])[0]
            off = 10
        if masked:
            if len(buf) < off + 4:
                return None
            mask = buf[off:off + 4]
            off += 4
        if len(buf) < off + n:
            return None
        data = bytearray(buf[off:off + n])
        if masked:
            for i in range(n):
                data[i] ^= mask[i % 4]
        self._buf = buf[off + n:]
        return opcode, bytes(data)


def new_key():
    return base64.b64encode(os.urandom(16)).decode()


def handshake_request(host, key, origin="https://app.plex.tv"):
    """The exact request wsprobe.py sent — live-verified against the relay."""
    lines = [
        "GET /ws HTTP/1.1",
        "Host: %s" % host,
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: %s" % key,
        "Sec-WebSocket-Version: 13",
        "Origin: %s" % origin,
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def handshake_response_status(head_bytes):
    """Parse the response head; return the status, raise unless it is 101."""
    first = head_bytes.split(b"\r\n", 1)[0].decode("latin-1")
    parts = first.split()
    if len(parts) < 2 or not parts[1].isdigit():
        raise HandshakeError("unparseable handshake response: %r" % first)
    status = int(parts[1])
    if status != 101:
        raise HandshakeError("handshake rejected: %s" % first)
    return status
