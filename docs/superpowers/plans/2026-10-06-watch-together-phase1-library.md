# Watch Together Phase 1 (Protocol Library) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Kodi-free protocol library for Watch Together — a minimal RFC6455 WebSocket client, the syncplay session/drift engine, and the rooms REST model — proven live against the real relay before any Kodi code is written.

**Architecture:** Four-layer split with transport injected into the session layer. `lib/ws.py` (generic WS, no Plex/Kodi) → `lib/syncplay.py` (session FSM + drift math, **no sockets, no Kodi**) → `lib/watchtogether.py` (rooms REST, token injected, no Kodi). The live soak script `scripts/watchtogether_soak.py` wires real sockets + real relay and is the phase exit gate. Kodi integration (player bridge, UI) is Phase 2, in a separate plan written after this one's exit.

**Tech Stack:** Python 3 stdlib (`socket`, `ssl`, `struct`, `json`, `threading`) + `requests` for REST (already an addon dep). No new dependencies. Tests: `uv run pytest` (717 baseline must stay green).

**Spec:** `docs/superpowers/specs/2026-10-06-watch-together-design.md`
**Protocol reference:** `docs/watch-together.md` — section numbers (§5.1 etc.) are cited inline; the doc is authoritative when anything conflicts.

---

## File structure

| File | Responsibility |
|---|---|
| Create: `lib/ws.py` | RFC6455 codec (encode/decode frames), handshake helpers, `WSClient` (one connection, reader thread, ping→pong, send lock) |
| Create: `lib/syncplay.py` | Identity helpers, message builders, `Latency` (§6.1), `sync_action` (§6.2), `Session` (inbound dispatch, self-ignore, `ignoringOnTheFly` mirror, roster, outbound `State`) |
| Create: `lib/watchtogether.py` | `RoomsApi` + `Room` model over `https://together.plex.tv` (§4); `createRoom`/`invite` stubs raising `NotImplementedError("v2")` |
| Create: `tests/test_ws.py` | Codec + handshake + WSClient against an in-process fake relay |
| Create: `tests/test_syncplay.py` | Identity, builders, latency, drift thresholds, session behaviour |
| Create: `tests/test_syncplay_transcript.py` | Conformance run over a recorded relay transcript fixture |
| Create: `tests/test_watchtogether.py` | REST model against an injected fake transport |
| Create: `tests/fixtures/syncplay/transcript.json` | Recorded relay frames (verbatim LIVE frames quoted in `docs/watch-together.md` §5.5/§5.9) with expected outcomes |
| Create: `tests/test_protocol_isolation.py` | Import-hygiene: the three protocol modules must never import `xbmc*` |
| Create: `scripts/watchtogether_soak.py` | Dev-only live validation: relay soak (2 identities, 1 Hz echo, convergence, self-echo, no-stall) + cloud discovery check. **Not in pytest.** |

Conventions: tests are plain `unittest.TestCase` (repo style), imported as `from lib import ws` etc. Every task ends with the full suite (`uv run pytest -q` → all green) and a commit.

---

### Task 1: WS frame codec

**Files:**
- Create: `lib/ws.py`
- Test: `tests/test_ws.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ws.py -q`
Expected: FAIL — `ModuleNotFoundError` / `FileNotFoundError: lib/ws.py`

- [ ] **Step 3: Write the implementation**

```python
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
```

Note: the class contains only the codec so far — `WSClient` arrives in Task 3.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ws.py -q`
Expected: 12 passed

- [ ] **Step 5: Full suite + commit**

Run: `uv run pytest -q` → `717 passed` + new ws tests green (nothing else touched)
```bash
git add lib/ws.py tests/test_ws.py
git commit -m "feat(ws): RFC6455 frame codec with incremental decoder"
```

---

### Task 2: WS handshake

**Files:**
- Modify: `lib/ws.py` (append)
- Test: `tests/test_ws.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ws.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ws.py -q::HandshakeTest 2>/dev/null || uv run pytest tests/test_ws.py -q -k Handshake`
Expected: FAIL — `AttributeError: module 'lib.ws' has no attribute 'handshake_request'`

- [ ] **Step 3: Write the implementation**

Append to `lib/ws.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ws.py -q`
Expected: 17 passed

- [ ] **Step 5: Full suite + commit**

Run: `uv run pytest -q` → all green
```bash
git add lib/ws.py tests/test_ws.py
git commit -m "feat(ws): websocket handshake request/response parsing"
```

---

### Task 3: `WSClient` — one connection with a reader thread

**Files:**
- Modify: `lib/ws.py` (append)
- Test: `tests/test_ws.py` (append)

`WSClient` runs exactly one connection (no auto-reconnect — reconnect policy belongs to the caller; a reconnect is a new session anyway, §5.8: per-user state does not survive a disconnect). Send path is serialised behind one lock: `sendall()` is NOT thread-safe and the heartbeat thread will race the main thread (§5.9, observed 2 failures in 5 before the lock).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ws.py`:

```python
import socket
import threading
import time


class FakeRelay(threading.Thread):
    """In-process plain-TCP relay double for WSClient tests (no TLS)."""

    def __init__(self):
        super(FakeRelay, self).__init__(daemon=True)
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.handshake = b""
        self.texts = []
        self.pongs = []
        self.error = None

    def run(self):
        try:
            conn, _ = self._srv.accept()
        except OSError as exc:
            self.error = exc
            return
        with conn:
            conn.settimeout(5)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            self.handshake = head
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n")
            decoder = ws.FrameDecoder()
            # 1. expect the client's first text frame
            text1 = self._next_text(conn, decoder)
            if text1 is not None:
                self.texts.append(text1)
            # 2. send a text frame and a ping
            conn.sendall(server_frame("ready"))
            conn.sendall(server_frame(b"ping-payload", 0x9))
            # 3. expect the client's pong
            self._next_pong(conn, decoder)
            # 4. send another text, then hang up
            conn.sendall(server_frame("bye"))
            time.sleep(0.2)

    def _next_text(self, conn, decoder, deadline=5.0):
        end = time.time() + deadline
        while time.time() < end:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return None
            for op, payload in decoder.feed(chunk):
                if op == 0x1:
                    return payload.decode()
        return None

    def _next_pong(self, conn, decoder, deadline=5.0):
        end = time.time() + deadline
        while time.time() < end:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return
            for op, payload in decoder.feed(chunk):
                if op == 0xA:
                    self.pongs.append(payload)
                    return


class WSClientTest(unittest.TestCase):
    def test_full_connection_lifecycle(self):
        relay = FakeRelay()
        relay.start()

        opened = []
        messages = []
        closed = []
        client = ws.WSClient(
            "127.0.0.1", relay.port, use_ssl=False,
            on_open=lambda: (opened.append(True), client.send({"hello": 1})),
            on_message=messages.append,
            on_close=lambda reason: closed.append(reason))
        client.start()
        deadline = time.time() + 6
        while time.time() < deadline and (len(messages) < 2 or not relay.pongs):
            time.sleep(0.05)
        client.close()

        self.assertEqual(opened, [True], "on_open must fire after the 101")
        self.assertEqual(messages, ["ready", "bye"], "text frames in order")
        self.assertEqual(relay.pongs, [b"ping-payload"], "ping answered with pong")
        self.assertEqual(len(relay.texts), 1)
        self.assertEqual(json.loads(relay.texts[0]), {"hello": 1})
        self.assertIn(b"Sec-WebSocket-Key:", relay.handshake)
        self.assertEqual(closed[:1], [closed[0]], "on_close fires once")
```

Note: add `import json` to the test imports if not present (Task 1 tests didn't use it).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ws.py -q -k WSClient`
Expected: FAIL — `AttributeError: module 'lib.ws' has no attribute 'WSClient'`

- [ ] **Step 3: Write the implementation**

Append to `lib/ws.py`:

```python
class WSClient(object):
    """One WebSocket connection with a reader thread.

    on_open()          — after the 101, before any frame (send Hello here)
    on_message(text)   — str payload of every text frame (reader thread)
    on_close(reason)   — when the connection is gone (reader thread)

    No auto-reconnect: per-user state does not survive a disconnect (§5.8),
    so a reconnect is a fresh session and the caller decides when to start
    a new WSClient.
    """

    def __init__(self, host, port, on_message, on_open=None, on_close=None,
                 use_ssl=True, timeout=15.0):
        self.host = host
        self.port = port
        self.on_message = on_message
        self.on_open = on_open
        self.on_close = on_close
        self.use_ssl = use_ssl
        self.timeout = timeout
        self._sock = None
        self._wlock = threading.Lock()   # sendall() is NOT thread-safe (§5.9)
        self._decoder = FrameDecoder()
        self._stopped = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        reason = "connect failed"
        try:
            self._connect_once()
            reason = "closed by peer"
        except Exception as exc:
            reason = repr(exc)
        finally:
            sock, self._sock = self._sock, None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self.on_close:
            self.on_close(reason)

    def _connect_once(self):
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.use_ssl:
            # verification stays ON for *.syncplay.plex.services (§10.2)
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        raw.settimeout(1.0)
        raw.sendall(handshake_request(self.host, new_key()))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(4096)
            if not chunk:
                raise HandshakeError("connection closed during handshake")
            head += chunk
        head, rest = head.split(b"\r\n\r\n", 1)
        handshake_response_status(head)
        with self._wlock:
            self._sock = raw
        if self.on_open:
            self.on_open()
        self._read_loop(rest)

    def _read_loop(self, initial=b""):
        for op, payload in self._decoder.feed(initial):
            self._dispatch(op, payload)
        while not self._stopped.is_set():
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                continue
            except ssl.SSLWantReadError:
                continue
            if not chunk:
                raise EOFError("server closed TCP")
            for op, payload in self._decoder.feed(chunk):
                self._dispatch(op, payload)

    def _dispatch(self, op, payload):
        if op == 0x9:                     # ping -> pong, immediately (§10.2)
            self.send_raw(payload, 0xA)
            return
        if op == 0x8:                     # close
            self._stopped.set()
            return
        if op in (0x1, 0x2):
            if self.on_message:
                self.on_message(payload.decode("utf-8", "replace"))

    def send(self, obj):
        """Serialise a dict to JSON and send it. Thread-safe."""
        self.send_raw(json.dumps(obj).encode(), 0x1)

    def send_raw(self, payload, opcode=0x1):
        with self._wlock:
            sock = self._sock
            if sock is None:
                raise OSError("not connected")
            sock.sendall(encode_frame(payload, opcode))

    def close(self):
        self._stopped.set()
        try:
            if self._sock is not None:
                self.send_raw(struct.pack(">H", 1000), 0x8)
        except OSError:
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ws.py -q`
Expected: 18 passed

- [ ] **Step 5: Full suite + commit**

Run: `uv run pytest -q` → all green
```bash
git add lib/ws.py tests/test_ws.py
git commit -m "feat(ws): WSClient with reader thread, ping/pong, locked sends"
```

---

### Task 4: Identity helpers

**Files:**
- Create: `lib/syncplay.py`
- Test: `tests/test_syncplay.py`

The identity is the whole session key (§5.1): double-encoded JSON string, under 150 bytes (the relay blind-truncates at 150), relay echoes it verbatim and appends `_` per collision, and self-recognition — which suppresses your own echo — depends on getting the strip right.

- [ ] **Step 1: Write the failing tests**

```python
# coding=utf-8
"""Tests for lib/syncplay.py — identity, builders, drift math, session.

Protocol references are docs/watch-together.md sections (§5.1 etc.)."""

from __future__ import absolute_import

import json
import unittest

from lib import syncplay


class IdentityTest(unittest.TestCase):
    def test_builds_compact_double_encodable_string(self):
        ident = syncplay.build_identity("dev-1234", "Kodi", 1000002)
        parsed = json.loads(ident)                 # the string itself is JSON
        self.assertEqual(parsed["deviceIdentifier"], "dev-1234")
        self.assertEqual(parsed["deviceName"], "Kodi")
        self.assertEqual(parsed["userID"], "1000002")

    def test_compact_separators(self):
        ident = syncplay.build_identity("d", "K", 1)
        self.assertNotIn(" ", ident, "relay echoes bytes as-is; stay compact (§5.1)")

    def test_numeric_user_id_becomes_string(self):
        ident = syncplay.build_identity("d", "K", 2028816)
        self.assertIn('"userID":"2028816"', ident)

    def test_rejects_identity_at_150_bytes(self):
        # 150+ bytes get blind-truncated mid-string by the relay (§5.1)
        with self.assertRaises(ValueError):
            syncplay.build_identity("device-identifier-1234567890",
                                    "way-too-long-device-name-for-the-150-cap",
                                    1000001)

    def test_short_device_name_fits(self):
        ident = syncplay.build_identity("device-identifier-1234567890", "Kodi", 1000001)
        self.assertLessEqual(len(ident.encode()), 149)

    def test_strip_identity_removes_collision_ladder_underscores(self):
        self.assertEqual(syncplay.strip_identity("abc_"), "abc")
        self.assertEqual(syncplay.strip_identity("abc___"), "abc")
        self.assertEqual(syncplay.strip_identity("abc"), "abc")
        self.assertEqual(syncplay.strip_identity(None), None)

    def test_is_self_matches_own_identity(self):
        ident = syncplay.build_identity("d", "Kodi", 1000001)
        self.assertTrue(syncplay.is_self(ident, ident))

    def test_is_self_survives_relay_appended_underscores(self):
        ident = syncplay.build_identity("d", "Kodi", 1000001)
        self.assertTrue(syncplay.is_self(ident + "__", ident))

    def test_is_self_survives_reordered_or_extended_setby(self):
        # twoclientprobe.py proved subset matching is what keeps self-ignore
        # alive across key reorder / extra fields / int-vs-str ids (§5.5)
        mine = json.dumps({"deviceIdentifier": "d", "deviceName": "Kodi",
                           "userID": "1000001"}, separators=(",", ":"))
        theirs = json.dumps({"userID": 1000001, "deviceIdentifier": "d",
                             "deviceName": "Kodi", "extra": "field"},
                            separators=(",", ":"))
        self.assertTrue(syncplay.is_self(theirs, mine))

    def test_is_self_rejects_other_identity(self):
        mine = syncplay.build_identity("d", "Kodi", 1000001)
        theirs = syncplay.build_identity("d", "Safari", 1000002)
        self.assertFalse(syncplay.is_self(theirs, mine))

    def test_is_self_rejects_none_and_garbage(self):
        ident = syncplay.build_identity("d", "K", 1)
        self.assertFalse(syncplay.is_self(None, ident))
        self.assertFalse(syncplay.is_self("not json", ident))
        self.assertFalse(syncplay.is_self(json.dumps(["list"]), ident))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_syncplay.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.syncplay'`

- [ ] **Step 3: Write the implementation**

```python
# coding=utf-8
"""Plex Watch Together syncplay session — protocol logic.

No Kodi imports and no sockets: transport is injected. Feed parsed JSON
messages to Session.on_message(); pull outbound messages from
Session.outbound_state() on a steady 1 Hz cadence. Protocol reference:
docs/watch-together.md §5–§6.
"""

from __future__ import absolute_import

import json
import time

VERSION = "1.6.4"
MAX_IDENTITY_BYTES = 149   # relay blind-truncates at 150 (§5.1); stay under
SEEK_BEHIND = -1.75        # §6.2 thresholds
SEEK_AHEAD = 4.0
TEMPO_DIFF = 1.5
TEMPO_RATE = 0.95


def build_identity(device_identifier, device_name, user_id):
    """The double-encoded identity string (§5.1). Compact, under 150 bytes."""
    ident = json.dumps(
        {"deviceIdentifier": device_identifier,
         "deviceName": device_name,
         "userID": str(user_id)},
        separators=(",", ":"))
    if len(ident.encode("utf-8")) > MAX_IDENTITY_BYTES:
        raise ValueError(
            "identity is %d bytes; the relay truncates at 150 and the result "
            "is unparseable — shorten deviceName" % len(ident.encode("utf-8")))
    return ident


def strip_identity(raw):
    """Strip the relay's collision-ladder underscores before parsing (§5.1)."""
    if isinstance(raw, str):
        return raw.rstrip("_")
    return raw


def is_self(set_by, identity):
    """True when setBy resolves to the identity WE sent (§5.5).

    The relay echoes our own State back with setBy filled in; applying it
    makes us fight our own playback. Matched as a subset on str() so it
    survives key reordering, extra fields and int-vs-str ids — full dict
    equality silently stops self-ignoring on any of those.
    """
    if not set_by or not identity:
        return False
    try:
        theirs = json.loads(strip_identity(set_by))
        mine = json.loads(strip_identity(identity))
    except (ValueError, TypeError):
        return False
    if not isinstance(theirs, dict) or not isinstance(mine, dict):
        return False
    return all(k in theirs and str(theirs[k]) == str(v) for k, v in mine.items())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_syncplay.py -q`
Expected: 13 passed

- [ ] **Step 5: Full suite + commit**

Run: `uv run pytest -q` → all green
```bash
git add lib/syncplay.py tests/test_syncplay.py
git commit -m "feat(syncplay): identity build/strip/self-match helpers"
```

---

### Task 5: Message builders

**Files:**
- Modify: `lib/syncplay.py` (append)
- Test: `tests/test_syncplay.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_syncplay.py`:

```python
class BuildersTest(unittest.TestCase):
    def test_hello_matches_wire_shape(self):
        ident = syncplay.build_identity("d", "K", 1)
        self.assertEqual(
            syncplay.hello("ca8cfezmke4", ident),
            {"Hello": {"room": {"name": "ca8cfezmke4"},
                       "username": ident,
                       "version": "1.6.4"}})

    def test_list_request(self):
        self.assertEqual(syncplay.list_request(), {"List": {}})

    def test_set_ready(self):
        self.assertEqual(
            syncplay.set_ready(True, manually_initiated=True),
            {"Set": {"ready": {"isReady": True, "manuallyInitiated": True}}})

    def test_set_file_double_encodes_uri(self):
        msg = syncplay.set_file("server://aaaa/com.plexapp.plugins.library/"
                                "library/metadata/227117")
        inner = json.loads(msg["Set"]["file"]["name"])   # name is a JSON string
        self.assertEqual(inner["uri"],
                         "server://aaaa/com.plexapp.plugins.library/"
                         "library/metadata/227117")
        self.assertEqual(inner["ads"], {"playing": False})

    def test_set_file_compact_inner(self):
        msg = syncplay.set_file("x")
        self.assertNotIn(" ", msg["Set"]["file"]["name"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_syncplay.py -q -k Builders`
Expected: FAIL — `AttributeError: module 'lib.syncplay' has no attribute 'hello'`

- [ ] **Step 3: Write the implementation**

Append to `lib/syncplay.py`:

```python
def hello(room, identity, version=VERSION):
    """Hello, sent immediately on connect (§5.2)."""
    return {"Hello": {"room": {"name": room},
                      "username": identity,
                      "version": version}}


def list_request():
    """Roster snapshot request (§5.3)."""
    return {"List": {}}


def set_ready(is_ready, manually_initiated=True):
    """Readiness for the lobby/ready flow — sent only on change (§6.4)."""
    return {"Set": {"ready": {"isReady": bool(is_ready),
                              "manuallyInitiated": bool(manuallyInitiated)}}}


def set_file(uri, playing=False):
    """Announce what is playing (§5.4). name is double-encoded JSON."""
    inner = json.dumps({"ads": {"playing": bool(playing)}, "uri": uri},
                       separators=(",", ":"))
    return {"Set": {"file": {"name": inner}}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_syncplay.py -q`
Expected: 18 passed

- [ ] **Step 5: Full suite + commit**

Run: `uv run pytest -q` → all green
```bash
git add lib/syncplay.py tests/test_syncplay.py
git commit -m "feat(syncplay): Hello/List/Set message builders"
```

---

### Task 6: Latency compensation + drift decision (§6.1–§6.2)

**Files:**
- Modify: `lib/syncplay.py` (append)
- Test: `tests/test_syncplay.py` (append)

The one piece the research doc explicitly flags as deserving a test (§10.3): pure arithmetic that fails silently when wrong.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_syncplay.py`:

```python
class LatencyTest(unittest.TestCase):
    def bootstrap(self, sr, lc, now):
        lat = syncplay.Latency()
        lat.on_state({"serverRtt": sr, "clientLatencyCalculation": lc}, now)
        return lat

    def test_first_sample_seeds_from_server_rtt(self):
        # §6.1: if averageRtt == 0: averageRtt = ping.serverRtt
        lat = self.bootstrap(sr=0.057, lc=100.0, now=100.1)
        expected_avg = 0.85 * 0.057 + 0.15 * 0.1
        self.assertAlmostEqual(lat.avg_rtt, expected_avg, places=6)
        expected = expected_avg / 2 + (0.1 - 0.057)   # sr < clientRtt -> skew add
        self.assertAlmostEqual(lat.forward_delay, expected, places=6)

    def test_no_skew_add_when_server_rtt_is_larger(self):
        lat = self.bootstrap(sr=3600.06, lc=100.0, now=100.1)
        self.assertAlmostEqual(lat.forward_delay, lat.avg_rtt / 2, places=6)

    def test_negative_client_rtt_sample_is_skipped(self):
        lat = syncplay.Latency()
        lat.on_state({"serverRtt": 0.05, "clientLatencyCalculation": 200.0}, 100.0)
        self.assertEqual(lat.client_rtt, 0.0, "clock ahead of sample: skip")
        self.assertEqual(lat.avg_rtt, 0.0)

    def test_negative_server_rtt_sample_is_skipped(self):
        lat = syncplay.Latency()
        lat.on_state({"serverRtt": -3599.94, "clientLatencyCalculation": 100.0},
                     100.1)
        self.assertEqual(lat.client_rtt, 0.0)

    def test_server_rtt_stored_for_outbound_echo_even_when_sample_skipped(self):
        lat = syncplay.Latency()
        lat.on_state({"serverRtt": -3599.94, "clientLatencyCalculation": 100.0},
                     100.1)
        self.assertEqual(lat.server_rtt, -3599.94)

    def test_missing_client_latency_calculation_updates_nothing(self):
        lat = syncplay.Latency()
        lat.on_state({"serverRtt": 0.2}, 100.0)
        self.assertEqual(lat.client_rtt, 0.0)
        self.assertEqual(lat.server_rtt, 0.2, "serverRtt is echoed back out (§5.5)")

    def test_ema_walk(self):
        lat = syncplay.Latency()
        lat.avg_rtt = 1.0
        lat.on_state({"serverRtt": 1.0, "clientLatencyCalculation": 0.0}, 2.0)
        self.assertAlmostEqual(lat.avg_rtt, 0.85 * 1.0 + 0.15 * 2.0, places=6)


class SyncActionTest(unittest.TestCase):
    def test_seeks_when_ahead_by_4_or_more(self):
        action = syncplay.sync_action(local_position=104.0,
                                      remote_position=100.0, paused=True,
                                      forward_delay=0.0)
        self.assertEqual(action, ("seek", 100.0))

    def test_seeks_when_behind_by_more_than_1_75(self):
        action = syncplay.sync_action(local_position=98.0,
                                      remote_position=100.0, paused=True,
                                      forward_delay=0.0)
        self.assertEqual(action, ("seek", 100.0))

    def test_boundary_1_75_exactly_does_not_seek(self):
        action = syncplay.sync_action(local_position=98.25,
                                      remote_position=100.0, paused=True,
                                      forward_delay=0.0)
        self.assertIsNone(action)

    def test_tempo_between_1_5_and_4(self):
        action = syncplay.sync_action(local_position=102.0,
                                      remote_position=100.0, paused=True,
                                      forward_delay=0.0)
        self.assertEqual(action, ("tempo", 0.95))

    def test_no_action_inside_drift_band(self):
        for local in (98.3, 99.0, 100.0, 101.4, 101.5):
            self.assertIsNone(
                syncplay.sync_action(local_position=local, remote_position=100.0,
                                     paused=True, forward_delay=0.0),
                "local=%r should be inside the band" % local)

    def test_playing_target_includes_forward_delay(self):
        # target = position + lastForwardDelay when not paused (§6.2)
        action = syncplay.sync_action(local_position=100.0,
                                      remote_position=100.0, paused=False,
                                      forward_delay=3.0)
        self.assertIsNone(action, "local - (remote+delay) = -3 is inside band")
        action = syncplay.sync_action(local_position=98.0,
                                      remote_position=100.0, paused=False,
                                      forward_delay=3.0)
        self.assertEqual(action, ("seek", 103.0),
                         "local - 103 = -5 -> seek to target 103")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_syncplay.py -q -k "Latency or SyncAction"`
Expected: FAIL — `AttributeError: module 'lib.syncplay' has no attribute 'Latency'`

- [ ] **Step 3: Write the implementation**

Append to `lib/syncplay.py`:

```python
class Latency(object):
    """§6.1 latency compensation + forward-delay estimation.

    serverRtt is our own clock skew, not the relay's (§5.5): it is
    relayNow − the epoch latencyCalculation WE sent, so a fresh epoch stamp
    every tick is what keeps it meaningful.
    """

    def __init__(self):
        self.avg_rtt = 0.0
        self.forward_delay = 0.0
        self.client_rtt = 0.0     # last accepted sample (echoed outbound)
        self.server_rtt = 0.0     # last value the relay reported (echoed back)

    def on_state(self, ping, now_mono):
        sr = ping.get("serverRtt")
        if isinstance(sr, (int, float)):
            self.server_rtt = sr
        lc = ping.get("clientLatencyCalculation")
        if not isinstance(lc, (int, float)):
            return
        client_rtt = now_mono - lc
        if client_rtt < 0 or (isinstance(sr, (int, float)) and sr < 0):
            return                      # §6.1: skip negative samples
        self.client_rtt = client_rtt
        if self.avg_rtt == 0:
            self.avg_rtt = sr if isinstance(sr, (int, float)) and sr >= 0 else 0.0
        self.avg_rtt = 0.85 * self.avg_rtt + 0.15 * client_rtt
        self.forward_delay = self.avg_rtt / 2.0
        if isinstance(sr, (int, float)) and sr < client_rtt:
            self.forward_delay += client_rtt - sr   # clock-skew correction


def sync_action(local_position, remote_position, paused, forward_delay):
    """§6.2: decide what the player must do to converge. Pure arithmetic.

    Returns None (stay), ("seek", target) or ("tempo", 0.95). The 0.95 is a
    tempo change (Kodi 21+ only, feature-detected in Phase 2) — callers may
    degrade it to a seek on older Kodi (§9).
    """
    target = remote_position + (0 if paused else forward_delay)
    diff = local_position - target
    if diff >= SEEK_AHEAD or diff <= SEEK_BEHIND:
        return ("seek", target)
    if diff > TEMPO_DIFF:
        return ("tempo", TEMPO_RATE)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_syncplay.py -q`
Expected: 30 passed

- [ ] **Step 5: Full suite + commit**

Run: `uv run pytest -q` → all green
```bash
git add lib/syncplay.py tests/test_syncplay.py
git commit -m "feat(syncplay): latency compensation and drift decision math"
```

---

### Task 7: `Session` — inbound dispatch, self-ignore, 1 Hz echo

**Files:**
- Modify: `lib/syncplay.py` (append)
- Test: `tests/test_syncplay.py` (append)

The conformance core (§10.2 "must be right" list): mirror `ignoringOnTheFly.server` back verbatim, drop own `setBy` echo, parse inbound position as float / send int, `setBy: null` outbound, one `State` per tick, roster keyed on the raw identity.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_syncplay.py`:

```python
class SessionTest(unittest.TestCase):
    def make(self):
        ident = syncplay.build_identity("dev-a", "Kodi", 1000001)
        self.events = []
        self.states = []
        self.rosters = []
        sess = syncplay.Session(
            room="ca8cfezmke4", identity=ident,
            on_state=self.states.append,
            on_roster=self.rosters.append,
            on_event=lambda kind, key: self.events.append((kind, key)))
        return sess, ident

    def test_outbound_state_shape(self):
        sess, _ = self.make()
        msg = sess.outbound_state({"position": 1804, "paused": False,
                                   "doSeek": True},
                                  now_mono=2755.552, now_epoch=1791143688.11)
        state = msg["State"]
        self.assertEqual(state["playstate"],
                         {"doSeek": True, "paused": False,
                          "position": 1804, "setBy": None})
        self.assertEqual(state["ping"]["clientLatencyCalculation"], 2755.552)
        self.assertEqual(state["ping"]["latencyCalculation"], 1791143688.11)
        self.assertEqual(state["ignoringOnTheFly"], {"client": 0, "server": 0})

    def test_outbound_position_is_int_even_for_float_local(self):
        sess, _ = self.make()
        msg = sess.outbound_state({"position": 1806.0028, "paused": False},
                                  now_mono=1.0, now_epoch=2.0)
        pos = msg["State"]["playstate"]["position"]
        self.assertIsInstance(pos, int)
        self.assertEqual(pos, 1806)

    def test_inbound_state_mirrors_relay_ignore_counter(self):
        # §5.9 — the one field that decides whether the relay keeps relaying
        sess, _ = self.make()
        sess.on_message({"State": {
            "ping": {"latencyCalculation": 1.0, "serverRtt": 0.16},
            "playstate": {"position": 100.0, "paused": False,
                          "doSeek": False, "setBy": None},
            "ignoringOnTheFly": {"server": 1}}}, now_mono=10.0)
        self.assertEqual(sess.relay_ignore, 1)
        outbound = sess.outbound_state({"position": 100},
                                       now_mono=11.0, now_epoch=2.0)
        self.assertEqual(outbound["State"]["ignoringOnTheFly"]["server"], 1)

    def test_inbound_state_without_counter_leaves_relay_ignore_alone(self):
        sess, _ = self.make()
        sess.relay_ignore = 1
        sess.on_message({"State": {
            "ping": {"latencyCalculation": 1.0, "serverRtt": 0.16},
            "playstate": {"position": 5.0, "paused": True,
                          "doSeek": False, "setBy": None}}}, now_mono=10.0)
        self.assertEqual(sess.relay_ignore, 1, "pure relay tick has no counter")

    def test_own_echo_is_counted_and_never_applied(self):
        # §5.5: applying your own echo pins you at a stale position
        sess, ident = self.make()
        sess.on_message({"State": {
            "ping": {"latencyCalculation": 1.0, "serverRtt": 0.1},
            "playstate": {"position": 42.0, "paused": False, "doSeek": False,
                          "setBy": ident + "_"}}}, now_mono=5.0)
        self.assertEqual(sess.self_echo, 1)
        self.assertEqual(sess.remote["position"], 0.0)
        self.assertEqual(self.states, [])

    def test_remote_state_applied_as_float(self):
        sess, _ = self.make()
        sess.on_message({"State": {
            "ping": {"latencyCalculation": 1.0, "serverRtt": 0.16},
            "playstate": {"position": 1806.0028346305862, "paused": False,
                          "doSeek": False,
                          "setBy": '{"deviceIdentifier":"other"}'}}},
            now_mono=5.0)
        self.assertEqual(sess.remote["position"], 1806.0028346305862)
        self.assertIsInstance(sess.remote["position"], float)
        self.assertEqual(len(self.states), 1)

    def test_doseek_frame_is_reported_to_consumer(self):
        sess, _ = self.make()
        sess.on_message({"State": {
            "ping": {"latencyCalculation": 1.0, "serverRtt": 0.1},
            "playstate": {"position": 900.0, "paused": False, "doSeek": True,
                          "setBy": '{"deviceIdentifier":"other"}'}}},
            now_mono=5.0)
        self.assertTrue(sess.remote["doSeek"])
        self.assertTrue(self.states[0]["doSeek"])

    def test_latency_uses_wall_now_when_not_given(self):
        sess, _ = self.make()
        sess.on_message(json.dumps({"State": {
            "ping": {"serverRtt": 0.2},
            "playstate": {"position": 0.0, "paused": True,
                          "doSeek": False, "setBy": None}}}))
        self.assertEqual(sess.latency.server_rtt, 0.2,
                         "str messages must parse (ws delivers str)")

    def test_list_populates_roster_keyed_by_room(self):
        sess, _ = self.make()
        sess.on_message({"List": {"ca8cfezmke4": {
            '{"deviceIdentifier":"d","deviceName":"K","userID":"1"}': {
                "position": 0, "file": {}, "controller": False,
                "isReady": None}}}})
        self.assertEqual(len(sess.roster), 1)
        self.assertEqual(self.rosters[0], sess.roster)

    def test_list_for_other_room_ignores(self):
        sess, _ = self.make()
        sess.on_message({"List": {"otherroom": {"x": {}}}})
        self.assertEqual(sess.roster, {})

    def test_set_ready_updates_roster_entry(self):
        sess, ident = self.make()
        sess.roster[ident] = {"isReady": None}
        sess.on_message({"Set": {"ready": {"username": ident,
                                           "isReady": True,
                                           "manuallyInitiated": True}}})
        self.assertIs(sess.roster[ident]["isReady"], True)

    def test_set_user_event_left_fires_and_drops_roster_entry(self):
        # §5.8 — transport death reaches peers in ~0.2 s as this event
        sess, ident = self.make()
        sess.roster[ident] = {"isReady": True}
        sess.on_message({"Set": {"user": {ident: {
            "room": {"name": "ca8cfezmke4"}, "event": {"left": True}}}}})
        self.assertEqual(self.events, [("left", ident)])
        self.assertNotIn(ident, sess.roster)

    def test_set_user_event_joined_fires(self):
        sess, _ = self.make()
        sess.on_message({"Set": {"user": {"someone-else": {
            "room": {"name": "ca8cfezmke4"}, "event": {"joined": True}}}}})
        self.assertEqual(self.events, [("joined", "someone-else")])

    def test_set_user_file_arrives_double_encoded(self):
        # §10.2: a file change arrives as Set.user, not as Set.file
        sess, _ = self.make()
        inner = json.dumps({"ads": {"playing": False},
                            "uri": "server://x/metadata/1"}, separators=(",", ":"))
        sess.on_message({"Set": {"user": {"me": {"file": {"name": inner}}}}})
        self.assertEqual(sess.file["uri"], "server://x/metadata/1")

    def test_set_file_echo_also_updates(self):
        sess, _ = self.make()
        inner = json.dumps({"ads": {"playing": False}, "uri": "u"},
                           separators=(",", ":"))
        sess.on_message({"Set": {"file": {"name": inner}}})
        self.assertEqual(sess.file["uri"], "u")

    def test_hello_response_stored(self):
        sess, _ = self.make()
        sess.on_message({"Hello": {"version": "1.6.4", "realversion": "1.6.5",
                                   "features": {"readiness": True}}})
        self.assertEqual(sess.relay_hello["realversion"], "1.6.5")

    def test_garbage_messages_are_ignored(self):
        sess, _ = self.make()
        sess.on_message("not json at all")
        sess.on_message([1, 2, 3])
        sess.on_message({"unknown": {}})
        self.assertEqual(sess.remote["position"], 0.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_syncplay.py -q -k Session`
Expected: FAIL — `AttributeError: module 'lib.syncplay' has no attribute 'Session'`

- [ ] **Step 3: Write the implementation**

Append to `lib/syncplay.py`:

```python
class Session(object):
    """Protocol session for one connection. Transport-agnostic (no sockets).

    on_state(remote)  — a remote (non-self) State was applied, per frame
    on_roster(roster) — a List snapshot arrived
    on_event(kind, key) — "left"/"joined" for a roster identity (§5.8)

    Drive outbound_state() at a steady 1 Hz: your echo cadence is every
    peer's smoothness (§5.5).
    """

    def __init__(self, room, identity, on_state=None, on_roster=None,
                 on_event=None):
        self.room = room
        self.identity = identity
        self.on_state = on_state
        self.on_roster = on_roster
        self.on_event = on_event
        self.relay_ignore = 0       # ignoringOnTheFly.server, live from wire (§5.9)
        self.local_ignore = 0       # 1 = local-only correction, don't rebroadcast (§5.7)
        self.latency = Latency()
        self.remote = {"position": 0.0, "paused": True, "doSeek": False,
                       "setBy": None}
        self.roster = {}            # raw identity -> List entry
        self.relay_hello = None
        self.file = None            # parsed {ads, uri} of what's playing
        self.self_echo = 0

    def on_message(self, msg, now_mono=None):
        if isinstance(msg, str):
            try:
                msg = json.loads(msg)
            except ValueError:
                return
        if not isinstance(msg, dict):
            return
        if "Hello" in msg:
            self.relay_hello = msg["Hello"]
        elif "List" in msg:
            self.roster = dict(msg["List"].get(self.room) or {})
            if self.on_roster:
                self.on_roster(dict(self.roster))
        elif "Set" in msg:
            self._on_set(msg["Set"])
        elif "State" in msg:
            self._on_state(msg["State"], now_mono)

    def _on_set(self, sub):
        if not isinstance(sub, dict):
            return
        ready = sub.get("ready")
        if isinstance(ready, dict):
            key = ready.get("username")
            if key:
                entry = self.roster.setdefault(key, {})
                entry["isReady"] = ready.get("isReady")
        user = sub.get("user")
        if isinstance(user, dict):
            for key, entry in user.items():
                if not isinstance(entry, dict):
                    continue
                event = entry.get("event") or {}
                if "left" in event or "joined" in event:
                    kind = "left" if event.get("left") else "joined"
                    if kind == "left":
                        self.roster.pop(key, None)
                    if self.on_event:
                        self.on_event(kind, key)
                self._take_file(entry.get("file"))
        self._take_file(sub.get("file"))

    def _take_file(self, file_obj):
        if isinstance(file_obj, dict) and isinstance(file_obj.get("name"), str):
            try:
                self.file = json.loads(file_obj["name"])
            except ValueError:
                self.file = None

    def _on_state(self, state, now_mono=None):
        ig = state.get("ignoringOnTheFly") or {}
        if "server" in ig:
            try:
                self.relay_ignore = int(ig["server"])
            except (TypeError, ValueError):
                pass
        ping = state.get("ping") or {}
        self.latency.on_state(ping,
                              time.monotonic() if now_mono is None else now_mono)
        ps = state.get("playstate") or {}
        if is_self(ps.get("setBy"), self.identity):
            self.self_echo += 1     # §5.5: our own echo — count, never apply
            return
        for key in ("position", "paused", "doSeek"):
            if ps.get(key) is not None:
                value = ps[key]
                if key == "position":
                    value = float(value)   # §5.5: never truncate the relay's float
                self.remote[key] = value
        self.remote["setBy"] = ps.get("setBy")
        if self.on_state:
            self.on_state(dict(self.remote))

    def outbound_state(self, local, now_mono=None, now_epoch=None):
        """One heartbeat frame (§5.5). Caller drives this at 1 Hz."""
        mono = time.monotonic() if now_mono is None else now_mono
        epoch = time.time() if now_epoch is None else now_epoch
        return {"State": {
            "ping": {"clientLatencyCalculation": mono,
                     "clientRtt": self.latency.client_rtt,
                     "serverRtt": self.latency.server_rtt,
                     "latencyCalculation": epoch},
            "playstate": {"doSeek": bool(local.get("doSeek", False)),
                          "paused": bool(local.get("paused", True)),
                          "position": int(local.get("position", 0)),
                          "setBy": None},
            "ignoringOnTheFly": {"client": self.local_ignore,
                                 "server": self.relay_ignore}}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_syncplay.py -q`
Expected: 48 passed

- [ ] **Step 5: Full suite + commit**

Run: `uv run pytest -q` → all green
```bash
git add lib/syncplay.py tests/test_syncplay.py
git commit -m "feat(syncplay): protocol session with self-ignore and relay counter mirror"
```

---

### Task 8: Recorded relay transcript conformance test

**Files:**
- Create: `tests/fixtures/syncplay/transcript.json`
- Create: `tests/test_syncplay_transcript.py`

§10.3 of the research doc: the drift/session behaviour deserves a test against a *recorded relay transcript*. The fixture frames below are the verbatim LIVE frames quoted in `docs/watch-together.md` §5.5 (real relay broadcasts, an own-echo frame with `ignoringOnTheFly.server: 1`, a remote driver's seek), with `<IDENTITY>` as the placeholder the test substitutes.

- [ ] **Step 1: Create the fixture**

`tests/fixtures/syncplay/transcript.json`:

```json
[
  {
    "note": "relay's own idle tick (§5.5) — apply as room truth",
    "in": {"State": {"ping": {"latencyCalculation": 1791143689.1127045, "serverRtt": 0.16222858428955078, "clientLatencyCalculation": 2756.3902396698}, "playstate": {"position": 1806.0028346305862, "paused": false, "doSeek": false, "setBy": null}}},
    "expect": {"remote_position": 1806.0028346305862, "relay_ignore": 0, "self_echo": 0}
  },
  {
    "note": "our own State echoed back with setBy=us and relay apply-ack (§5.5/§5.9) — count, drop, mirror counter",
    "in": {"State": {"ping": {"latencyCalculation": 1791143698.2830253, "serverRtt": 0.1694636344909668, "clientLatencyCalculation": 2765.5562961158753}, "playstate": {"position": 1806.0, "paused": false, "doSeek": false, "setBy": "<IDENTITY>"}, "ignoringOnTheFly": {"server": 1}}},
    "expect": {"remote_position": 1806.0028346305862, "relay_ignore": 1, "self_echo": 1}
  },
  {
    "note": "remote driver's state — apply position + setBy (§5.5)",
    "in": {"State": {"ping": {"latencyCalculation": 1791143700.11, "serverRtt": 0.158, "clientLatencyCalculation": 2767.39}, "playstate": {"position": 1810.0, "paused": false, "doSeek": false, "setBy": "{\"deviceIdentifier\":\"zzzzzzzzzzzzzzzzzzzzzz\",\"deviceName\":\"Safari\",\"userID\":\"1000001\"}"}}},
    "expect": {"remote_position": 1810.0, "relay_ignore": 1, "self_echo": 1, "set_by_name": "Safari"}
  },
  {
    "note": "remote driver seeks — doSeek lives one tick, position is the target (§5.6)",
    "in": {"State": {"ping": {"latencyCalculation": 1791143701.12, "serverRtt": 0.16}, "playstate": {"position": 1753.0, "paused": false, "doSeek": true, "setBy": "{\"deviceIdentifier\":\"zzzzzzzzzzzzzzzzzzzzzz\",\"deviceName\":\"Safari\",\"userID\":\"1000001\"}"}}},
    "expect": {"remote_position": 1753.0, "remote_do_seek": true}
  },
  {
    "note": "next tick: doSeek back to false (§5.6)",
    "in": {"State": {"ping": {"latencyCalculation": 1791143702.13, "serverRtt": 0.16}, "playstate": {"position": 1754.0, "paused": false, "doSeek": false, "setBy": "{\"deviceIdentifier\":\"zzzzzzzzzzzzzzzzzzzzzz\",\"deviceName\":\"Safari\",\"userID\":\"1000001\"}"}}},
    "expect": {"remote_position": 1754.0, "remote_do_seek": false}
  },
  {
    "note": "paused room: position stays an int on the wire (§5.5)",
    "in": {"State": {"ping": {"latencyCalculation": 1791143710.14, "serverRtt": 0.16}, "playstate": {"position": 1804, "paused": true, "doSeek": false, "setBy": null}}},
    "expect": {"remote_position": 1804.0, "remote_paused": true}
  },
  {
    "note": "roster snapshot keyed by room then identity (§5.3)",
    "in": {"List": {"<ROOM>": {"<IDENTITY>": {"position": 0, "file": {}, "controller": false, "isReady": null}}}},
    "expect": {"roster_len": 1}
  },
  {
    "note": "peer's transport death as Set.user event (§5.8)",
    "in": {"Set": {"user": {"{\"deviceIdentifier\":\"zzzzzzzzzzzzzzzzzzzzzz\",\"deviceName\":\"Safari\",\"userID\":\"1000001\"}": {"room": {"name": "<ROOM>"}, "event": {"left": true}}}}},
    "expect": {"roster_len": 0, "last_event": ["left", "Safari"]}
  }
]
```

(`roster_len` counts `Session.roster`; the `left` event expectation strips identity to `deviceName` — the test's `last_event` assertion checks kind + that the key parses to a dict with `deviceName == "Safari"`.)

- [ ] **Step 2: Write the failing test**

`tests/test_syncplay_transcript.py`:

```python
# coding=utf-8
"""Run the Session over a recorded relay transcript (spec §testing strategy 1)."""

from __future__ import absolute_import

import json
import os
import unittest

from lib import syncplay

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "syncplay",
                       "transcript.json")


class TranscriptTest(unittest.TestCase):
    def test_session_conforms_to_recorded_relay_transcript(self):
        with open(FIXTURE, "r", encoding="utf-8") as fp:
            steps = json.load(fp)
        ident = syncplay.build_identity("zzzzzzzzzzzzzzzzzzzzzz", "Kodi", 1000001)
        room = "ca8cfezmke4"
        events = []
        sess = syncplay.Session(room, ident,
                                on_event=lambda kind, key: events.append((kind, key)))
        now = 100.0
        for i, step in enumerate(steps):
            now += 1.0
            raw = json.dumps(step["in"])
            raw = raw.replace("<IDENTITY>", ident).replace("<ROOM>", room)
            sess.on_message(raw, now_mono=now)
            expect = step["expect"]
            if "remote_position" in expect:
                self.assertEqual(sess.remote["position"], expect["remote_position"],
                                 "step %d (%s): position" % (i, step["note"]))
            if "remote_paused" in expect:
                self.assertEqual(sess.remote["paused"], expect["remote_paused"],
                                 "step %d: paused" % i)
            if "remote_do_seek" in expect:
                self.assertEqual(sess.remote["doSeek"], expect["remote_do_seek"],
                                 "step %d: doSeek" % i)
            if "relay_ignore" in expect:
                self.assertEqual(sess.relay_ignore, expect["relay_ignore"],
                                 "step %d (%s): relay_ignore" % (i, step["note"]))
            if "self_echo" in expect:
                self.assertEqual(sess.self_echo, expect["self_echo"],
                                 "step %d: self_echo" % i)
            if "roster_len" in expect:
                self.assertEqual(len(sess.roster), expect["roster_len"],
                                 "step %d: roster" % i)
            if "set_by_name" in expect:
                set_by = json.loads(syncplay.strip_identity(sess.remote["setBy"]))
                self.assertEqual(set_by["deviceName"], expect["set_by_name"])
            if "last_event" in expect:
                kind, key = events[-1]
                self.assertEqual(kind, expect["last_event"][0])
                parsed = json.loads(syncplay.strip_identity(key))
                self.assertEqual(parsed["deviceName"], expect["last_event"][1])

    def test_outbound_after_transcript_mirrors_counter(self):
        # §5.9: the stall killer is a client that keeps sending server: 0
        with open(FIXTURE, "r", encoding="utf-8") as fp:
            steps = json.load(fp)
        ident = syncplay.build_identity("dev", "K", 1)
        sess = syncplay.Session("ca8cfezmke4", ident)
        for step in steps:
            now = 100.0
            sess.on_message(json.dumps(step["in"]).replace("<IDENTITY>", ident)
                            .replace("<ROOM>", "ca8cfezmke4"), now_mono=now)
        outbound = sess.outbound_state({"position": 100, "paused": True},
                                       now_mono=1.0, now_epoch=2.0)
        # last frame in the transcript sets no counter; the apply-ack step did
        self.assertEqual(outbound["State"]["ignoringOnTheFly"]["server"], 1)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_syncplay_transcript.py -q`
Expected: FAIL — `FileNotFoundError` (fixture not yet written → but Step 1 created it; expected PASS if Step 1 done first; if run in order, this validates the fixture lands on Session correctly). If red: the Session/fixture disagree — fix before proceeding.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_syncplay_transcript.py -q`
Expected: 2 passed

- [ ] **Step 5: Full suite + commit**

Run: `uv run pytest -q` → all green
```bash
git add tests/fixtures/syncplay/transcript.json tests/test_syncplay_transcript.py
git commit -m "test(syncplay): conformance over recorded relay transcript"
```

---

### Task 9: REST model — `RoomsApi` / `Room`

**Files:**
- Create: `lib/watchtogether.py`
- Test: `tests/test_watchtogether.py`

Token is injected (no Kodi account plumbing here — that's Phase 2's bridge). Transport is injectable for tests. Rule from §4: **never read or log a 401 body** — it leaks internal cluster URLs.

- [ ] **Step 1: Write the failing tests**

```python
# coding=utf-8
"""Tests for lib/watchtogether.py — rooms REST model (docs/watch-together.md §4)."""

from __future__ import absolute_import

import unittest

from lib import watchtogether

ROOM_JSON = {
    "id": "ca8cfezmke4",
    "title": "A Fazenda – S18 • E20 – Episode 20",
    "type": "watch",
    "sourceUri": "server://aaaa0000bbbb1111cccc2222dddd3333eeee4444/"
                 "com.plexapp.plugins.library/library/metadata/227117",
    "source": "server://aaaa0000bbbb1111cccc2222dddd3333eeee4444/"
              "com.plexapp.plugins.library/library/metadata/227117",
    "createdBy": 1000003,
    "startsAt": 1791128438,
    "updatedAt": 1791128438,
    "endsAt": 1791139238,
    "syncplayHost": "pop-fra00.syncplay.plex.services",
    "syncplayPort": 7776,
    "users": [
        {"id": 1000003, "username": "otherviewer", "title": "Other Viewer",
         "uuid": "aaaa000000000001", "thumb": "https://plex.tv/users/x/avatar"},
        {"id": 1000001, "username": "serverowner", "title": "Server Owner",
         "uuid": "aaaa000000000002", "thumb": "https://api.plex.tv/users/y/avatar"},
        {"id": 1000003, "username": "otherviewer", "title": "Other Viewer",
         "uuid": "aaaa000000000001", "thumb": "https://plex.tv/users/x/avatar"},
    ],
}


class FakeTransport(object):
    """Stands in for _http_request: (method, path, body, token) -> (status, obj)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, body=None, token=None):
        self.calls.append((method, path, body, token))
        return self.responses.pop(0)


class RoomModelTest(unittest.TestCase):
    def test_parses_room_fields(self):
        room = watchtogether.Room(ROOM_JSON)
        self.assertEqual(room.id, "ca8cfezmke4")
        self.assertEqual(room.syncplay_host, "pop-fra00.syncplay.plex.services")
        self.assertEqual(room.syncplay_port, 7776)
        self.assertEqual(room.created_by, 1000003)
        self.assertEqual(room.title, ROOM_JSON["title"])

    def test_source_uri_falls_back_to_source(self):
        data = dict(ROOM_JSON)
        del data["sourceUri"]
        room = watchtogether.Room(data)
        self.assertIn("metadata/227117", room.source_uri)

    def test_users_are_a_set_even_when_inviter_duplicated(self):
        # §4: users[] can contain the same id twice — treat as a set
        room = watchtogether.Room(ROOM_JSON)
        self.assertEqual(room.user_ids, {1000003, 1000001})

    def test_participants_dedup(self):
        room = watchtogether.Room(ROOM_JSON)
        self.assertEqual(len(room.participants), 2)

    def test_ended_uses_ends_at(self):
        room = watchtogether.Room(ROOM_JSON)
        self.assertFalse(room.ended(now=ROOM_JSON["endsAt"] - 1))
        self.assertTrue(room.ended(now=ROOM_JSON["endsAt"]))


class RoomsApiTest(unittest.TestCase):
    def api(self, *responses):
        transport = FakeTransport(list(responses))
        return watchtogether.RoomsApi(token="tok", transport=transport), transport

    def test_rooms_parses_list(self):
        api, transport = self.api((200, {"rooms": [ROOM_JSON]}))
        rooms = api.rooms()
        self.assertEqual([r.id for r in rooms], ["ca8cfezmke4"])
        self.assertEqual(transport.calls[0][:2], ("GET", "/rooms"))
        self.assertEqual(transport.calls[0][3], "tok")

    def test_rooms_empty_is_empty_list_not_error(self):
        # §4: GET /rooms with no active rooms -> 200 {"rooms":[]}
        api, _ = self.api((200, {"rooms": []}))
        self.assertEqual(api.rooms(), [])

    def test_room_fetch(self):
        api, transport = self.api((200, ROOM_JSON))
        room = api.room("ca8cfezmke4")
        self.assertEqual(room.id, "ca8cfezmke4")
        self.assertEqual(transport.calls[0][:2], ("GET", "/rooms/ca8cfezmke4"))

    def test_403_maps_to_not_member(self):
        api, _ = self.api((403, "you do not have access to that room"))
        with self.assertRaises(watchtogether.NotMember):
            api.room("ca8cfezmke4")

    def test_404_maps_to_room_gone(self):
        api, _ = self.api((404, "room not found not found!"))
        with self.assertRaises(watchtogether.RoomGone):
            api.room("ca8cfezmke4")

    def test_401_maps_to_auth_error_and_never_carries_the_body(self):
        # §4: never log/read a 401 body — the first one leaks an internal URL
        api, _ = self.api((401, "Request failed with status code 401 "
                                "(Unauthorized): GET http://my-plex-api"
                                ".plex-tv.svc.cluster.local:8080/..."))
        with self.assertRaises(watchtogether.AuthError) as ctx:
            api.rooms()
        self.assertNotIn("cluster.local", str(ctx.exception))
        self.assertNotIn("http", str(ctx.exception))

    def test_leave_sends_delete(self):
        api, transport = self.api((204, None))
        api.leave("ca8cfezmke4")
        self.assertEqual(transport.calls[0][:2], ("DELETE", "/rooms/ca8cfezmke4"))

    def test_leave_swallows_404(self):
        # second DELETE / leave of an expired room -> 404; still "gone = done"
        api, _ = self.api((404, "room not found not found!"))
        api.leave("ca8cfezmke4")

    def test_create_and_invite_are_v2_stubs(self):
        api, _ = self.api()
        with self.assertRaises(NotImplementedError):
            api.create(source_uri="x", title="t")
        with self.assertRaises(NotImplementedError):
            api.invite("ca8cfezmke4", [1000002])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_watchtogether.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.watchtogether'`

- [ ] **Step 3: Write the implementation**

```python
# coding=utf-8
"""Watch Together rooms REST model — https://together.plex.tv (§4).

No Kodi: the plex.tv account token is injected by the caller. Transport is
injectable for tests. Rules baked in from §4: DELETE means "leave" (never
"destroy"); a room you left reads 403 while an expired one reads 404;
never read or log a 401 body — it leaks internal service URLs.
"""

from __future__ import absolute_import

import requests

BASE = "https://together.plex.tv"


class WatchTogetherError(Exception):
    pass


class AuthError(WatchTogetherError):
    """401 — message carries method+path only, never the response body."""


class NotMember(WatchTogetherError):
    """403 on GET — you are not (or no longer) in this room."""


class RoomGone(WatchTogetherError):
    """404 — expired (endsAt passed) or never existed. Never retry."""


class Room(object):
    def __init__(self, data):
        self.raw = data
        self.id = data.get("id")
        self.title = data.get("title")
        self.source_uri = data.get("sourceUri") or data.get("source")
        self.created_by = data.get("createdBy")
        self.starts_at = data.get("startsAt", 0)
        self.ends_at = data.get("endsAt", 0)
        self.syncplay_host = data.get("syncplayHost")
        self.syncplay_port = data.get("syncplayPort")
        self.users = data.get("users") or []

    @property
    def user_ids(self):
        # users[] can contain the inviter twice — treat as a set (§4)
        return set(u.get("id") for u in self.users if isinstance(u, dict))

    @property
    def participants(self):
        seen = {}
        for user in self.users:
            if isinstance(user, dict) and user.get("id") is not None:
                seen.setdefault(user["id"], user)
        return list(seen.values())

    def ended(self, now=None):
        import time as _time
        return (now if now is not None else _time.time()) >= self.ends_at


def _http_request(method, path, body=None, token=None):
    headers = {"Accept": "application/json", "X-Plex-Token": token or ""}
    kwargs = {"headers": headers, "timeout": 15}
    if body is not None:
        kwargs["json"] = body
    resp = requests.request(method, BASE + path, **kwargs)
    try:
        payload = resp.json() if resp.content else None
    except ValueError:
        payload = None
    return resp.status_code, payload


class RoomsApi(object):
    """The §4 surface used by v1: list, fetch, leave. create/invite = v2."""

    def __init__(self, token, transport=None):
        self.token = token
        self._transport = transport or _http_request

    def _req(self, method, path, body=None):
        status, payload = self._transport(method, path, body, self.token)
        if status == 401:
            # §4: body is untrusted and the first 401 leaks an internal URL
            raise AuthError("%s %s -> 401" % (method, path))
        if status == 403:
            raise NotMember("%s %s -> 403" % (method, path))
        if status == 404:
            raise RoomGone("%s %s -> 404" % (method, path))
        if status >= 400:
            raise WatchTogetherError("%s %s -> HTTP %d" % (method, path, status))
        return payload

    def rooms(self):
        payload = self._req("GET", "/rooms") or {"rooms": []}
        return [Room(r) for r in payload.get("rooms") or []]

    def room(self, room_id):
        return Room(self._req("GET", "/rooms/%s" % room_id))

    def leave(self, room_id):
        """DELETE is per-participant leave (§4); 404 means already gone."""
        try:
            self._req("DELETE", "/rooms/%s" % room_id)
        except RoomGone:
            pass

    def create(self, source_uri, title, users=None):
        raise NotImplementedError("v2: host capability from Kodi")

    def invite(self, room_id, user_ids):
        raise NotImplementedError("v2: host capability from Kodi")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_watchtogether.py -q`
Expected: 17 passed

- [ ] **Step 5: Full suite + commit**

Run: `uv run pytest -q` → all green
```bash
git add lib/watchtogether.py tests/test_watchtogether.py
git commit -m "feat(watchtogether): rooms REST model with injectable transport"
```

---

### Task 10: Import-hygiene guard

**Files:**
- Create: `tests/test_protocol_isolation.py`

The spec's testability rule: `lib/ws.py`, `lib/syncplay.py`, `lib/watchtogether.py` never import Kodi. `kodistubs` sits on `sys.path` during tests, so a normal import test would happily resolve `import xbmc` — this guard reads the source instead.

- [ ] **Step 1: Write the failing test** (it should pass immediately if Tasks 1–9 kept the rule — that's the point: it fails later if someone breaks it)

```python
# coding=utf-8
"""The protocol library must stay Kodi-free (spec: Architecture)."""

from __future__ import absolute_import

import os
import re
import unittest

from . import REPO_ROOT

MODULES = ("ws.py", "syncplay.py", "watchtogether.py")
KODI_IMPORT = re.compile(r"^\s*(?:import|from)\s+xbmc", re.MULTILINE)


class IsolationTest(unittest.TestCase):
    def test_protocol_modules_never_import_kodi(self):
        for name in MODULES:
            with open(os.path.join(REPO_ROOT, "lib", name), "r",
                      encoding="utf-8") as fp:
                source = fp.read()
            self.assertIsNone(
                KODI_IMPORT.search(source),
                "%s must not import xbmc — it is the testability boundary "
                "that keeps the protocol layer deletable (spec: risks)" % name)
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_protocol_isolation.py -q`
Expected: 1 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_protocol_isolation.py
git commit -m "test: guard the no-Kodi boundary of the protocol library"
```

---

### Task 11: Live soak script (phase exit gate)

**Files:**
- Create: `scripts/watchtogether_soak.py`

Dev-only, **not in pytest**. Two subcommands:

- `relay` (default): two `WSClient` + `Session` instances against a real relay in a throwaway room (the `twoclientprobe.py` pattern — cloud never told, no cleanup needed), 1 Hz echo each, one client drives position; asserts no-stall, roster, convergence, self-echo handling. Relay needs no token (§11).
- `cloud`: read-only `RoomsApi.rooms()` + `room()` live check against a real token: asserts relay endpoint present. Optional `--leave` for cleanup checks.

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""Live Watch Together validation for the PM4K protocol library.

Dev-only — NOT part of pytest. Uses the real lib/ws.py + lib/syncplay.py +
lib/watchtogether.py against the real services.

  python3 scripts/watchtogether_soak.py relay [--minutes 2] [--room NAME]
      Two identities in a throwaway relay room (the cloud is never told, so
      there is nothing to clean up). Asserts:
        * no stall: relay States keep arriving for the whole soak (§5.9)
        * roster: both identities appear in List
        * convergence: follower's remote position tracks the driver
        * self-echo: driver drops its own echoed States (§5.5)
      Exit 0 = PASS, 1 = FAIL.

  python3 scripts/watchtogether_soak.py cloud [--token-file PATH] [--leave]
      Read-only RoomsApi check: GET /rooms, then GET /rooms/{id} on the first
      room; asserts syncplayHost/syncplayPort present. With --leave also
      DELETEs (you leave the room — nothing is destroyed, §4).

Exit code 0 = PASS, 1 = FAIL.
"""

from __future__ import absolute_import, print_function

import argparse
import os
import sys
import threading
import time
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from lib.syncplay import Session, build_identity, hello, list_request  # noqa: E402
from lib.watchtogether import RoomsApi, RoomGone  # noqa: E402
from lib.ws import WSClient  # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print("  %-52s %s%s" % (name, "PASS" if ok else "FAIL",
                            (" — " + detail) if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


class Peer(object):
    """One WSClient + Session + 1 Hz echo loop."""

    def __init__(self, label, user_id, device, room, host, port):
        self.label = label
        self.identity = build_identity(device, label, user_id)
        self.session = Session(room, self.identity)
        self.local = {"position": 0.0, "paused": True, "doSeek": False}
        self.inbound_at = []
        self._lock = threading.Lock()
        self.client = WSClient(
            host, port,
            on_open=self._on_open,
            on_message=self._on_message,
            on_close=self._on_close)
        self._stop = threading.Event()

    def _on_open(self):
        self.client.send(hello(self.session.room, self.identity))
        self.client.send(list_request())

    def _on_message(self, text):
        with self._lock:
            self.inbound_at.append(time.monotonic())
        self.session.on_message(text)

    def _on_close(self, reason):
        self._stop.set()

    def start(self):
        self.client.start()
        threading.Thread(target=self._echo_loop, daemon=True).start()

    def _echo_loop(self):
        # steady 1 Hz: our cadence is everyone else's smoothness (§5.5)
        while not self._stop.is_set():
            msg = self.session.outbound_state(self.local)
            try:
                self.client.send(msg)
            except OSError:
                self._stop.set()
                return
            self._stop.wait(1.0)

    def stop(self):
        self._stop.set()
        self.client.close()


def soak_relay(args):
    host, port_s = args.relay.rsplit(":", 1)
    port = int(port_s)
    room = args.room or ("wt" + uuid.uuid4().hex[:8])
    print("relay  : %s" % args.relay)
    print("room   : %s  (throwaway — cloud never told)" % room)

    driver = Peer("driver", 1000001, "soak-driver", room, host, port)
    follower = Peer("follower", 1000002, "soak-follower", room, host, port)
    driver.start()
    follower.start()

    # let the handshake + List land
    time.sleep(3.0)
    start = time.monotonic()

    # driver plays: position advances 1 per tick; follower must converge
    driver.local["paused"] = False
    deadline = start + args.minutes * 60
    settled = False
    while time.monotonic() < deadline:
        driver.local["position"] += 1.0
        time.sleep(1.0)
        if not settled and time.monotonic() > start + 10:
            gap = abs(follower.session.remote["position"]
                      - driver.local["position"])
            settled = True
            check("follower converges on driver (drift < 6s)", gap < 6.0,
                  "gap=%.2f" % gap)

    elapsed = time.monotonic() - start
    driver.stop()
    follower.stop()
    time.sleep(1.0)

    for peer in (driver, follower):
        inbound = [t for t in peer.inbound_at if t >= start]
        expected = elapsed * 0.9        # 1 Hz, allow 10% jitter
        check("%s: no stall — relay kept relaying for %.0fs" % (peer.label, elapsed),
              len(inbound) >= expected,
              "%d frames in %.0fs (want >= %.0f)" % (len(inbound), elapsed, expected))
        if len(inbound) > 2:
            worst_gap = max(b - a for a, b in zip(inbound, inbound[1:]))
            check("%s: worst inter-frame gap < 3s" % peer.label, worst_gap < 3.0,
                  "gap=%.2fs" % worst_gap)

    check("roster contains both identities", len(driver.session.roster) == 2,
          "roster=%d" % len(driver.session.roster))
    check("driver self-echoed and dropped own States (§5.5)",
          driver.session.self_echo > 0, "self_echo=%d" % driver.session.self_echo)
    check("follower never self-echoed", follower.session.self_echo == 0)
    check("follower's remote setBy names the driver",
          follower.session.remote.get("setBy") is not None)
    return not FAILURES


def cloud(args):
    with open(os.path.expanduser(args.token_file), "r") as fp:
        token = fp.read().strip()
    api = RoomsApi(token)
    rooms = api.rooms()
    print("  GET /rooms -> %d room(s)" % len(rooms))
    if not rooms:
        print("  (no rooms you are a member of — create one on plex.tv and "
              "invite this account to test discovery)")
        check("cloud: GET /rooms reachable with valid token", True)
        return not FAILURES
    room = rooms[0]
    fetched = api.room(room.id)
    check("cloud: room fetched and has a relay endpoint",
          bool(fetched.syncplay_host and fetched.syncplay_port),
          "host=%r port=%r" % (fetched.syncplay_host, fetched.syncplay_port))
    check("cloud: room has sourceUri", bool(fetched.source_uri))
    if args.leave:
        api.leave(room.id)
        try:
            api.room(room.id)
            check("cloud: room reads 403/404 after leave", False, "still readable")
        except RoomGone:
            check("cloud: room gone for us after leave", True)
        except Exception as exc:      # NotMember == 403 == left (§4)
            check("cloud: left the room (%s)" % type(exc).__name__, True)
    return not FAILURES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode")
    sub.required = True
    p_relay = sub.add_parser("relay", help="relay soak, throwaway room")
    p_relay.add_argument("--relay", default="pop-atl01.syncplay.plex.services:7777",
                         help="host:port of a syncplay PoP")
    p_relay.add_argument("--room", default=None, help="throwaway room name")
    p_relay.add_argument("--minutes", type=float, default=2.0)
    p_cloud = sub.add_parser("cloud", help="read-only RoomsApi check")
    p_cloud.add_argument("--token-file", default="~/.plex-probe-token-host")
    p_cloud.add_argument("--leave", action="store_true")
    args = parser.parse_args()

    ok = soak_relay(args) if args.mode == "relay" else cloud(args)
    print("\n%s" % ("PASS" if ok and not FAILURES else "FAIL: %s" % FAILURES))
    return 0 if ok and not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Offline sanity — script parses and help works**

Run: `python3 scripts/watchtogether_soak.py --help`
Expected: usage text, exit 0
Run: `uv run pytest -q` → all green (script not collected; it's under `scripts/`)

- [ ] **Step 3: Live relay soak (network, ~2.5 min)**

Run: `python3 scripts/watchtogether_soak.py relay --minutes 2`
Expected: every check `PASS`, final line `PASS`, exit 0.
Failure modes to watch: any `no stall` FAIL → the §5.9 counter mirror is broken in `Session.outbound_state`; `convergence` FAIL → drift math or self-ignore wiring.

- [ ] **Step 4: Live cloud check (network, read-only)**

Run: `python3 scripts/watchtogether_soak.py cloud --token-file ~/.plex-probe-token-host`
With an active room present (create one on plex.tv with the other account and invite this one — optional): Expected `PASS` lines for room fetch + relay endpoint. Without rooms: Expected `PASS` on the reachable check with the informational note.

- [ ] **Step 5: Commit**

```bash
git add scripts/watchtogether_soak.py
git commit -m "chore: live soak script — phase 1 exit gate"
```

---

### Task 12: Phase 1 exit verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite**

Run: `uv run pytest -q`
Expected: **717 + all new tests** green (717 existing never regress — spec baseline guard).

- [ ] **Step 2: Isolation guard**

Run: `uv run pytest tests/test_protocol_isolation.py -q`
Expected: 1 passed.

- [ ] **Step 3: Live gates re-run**

Run: `python3 scripts/watchtogether_soak.py relay --minutes 2` → PASS
Run: `python3 scripts/watchtogether_soak.py cloud` → PASS

- [ ] **Step 4: Confirm no secrets staged**

Run: `git status --short && git grep -l "plex-probe-token" -- '*.py' ':!scripts/*' ':!docs/*' || echo clean`
Expected: clean working tree; no token values anywhere (`git grep -E "[a-f0-9]{20,}" -- lib/ tests/` returns nothing token-shaped).

- [ ] **Step 5: Record phase completion**

```bash
git log --oneline -12   # tasks 1-11 as separate commits
```
Phase 1 exit = all of the above green. Phase 2 plan (player bridge + UI, P4–P5) is written **after** this exit, once the live soak has validated the real APIs.

---

## Self-review (writing-plans checklist)

1. **Spec coverage:** P1 = Tasks 1–3 (`lib/ws.py`) ✓; P2 = Tasks 4–8 (identity, builders, math, session, transcript — spec's `tests/test_syncplay.py` + `tests/fixtures/syncplay/` ✓); P3 = Tasks 9–11 (REST model with v2 stubs ✓, live soak ✓); isolation guard covers the spec's "import-clean of xbmc" requirement ✓. Spec's `tests/test_watchtogether.py` ✓. Baseline guard in every task's full-suite step ✓.
2. **Placeholders:** none — every step carries complete code, exact commands, expected outputs.
3. **Type consistency:** `RoomsApi(token, transport=)` vs `FakeTransport.__call__(method, path, body, token)` match; `Session(room, identity, on_state, on_roster, on_event)` matches all call sites; `build_identity(device_identifier, device_name, user_id)` matches all callers; `WSClient(host, port, on_message, on_open, on_close, use_ssl, timeout)` matches test + soak script; `sync_action(local_position, remote_position, paused, forward_delay)` matches §6.2 tests.
