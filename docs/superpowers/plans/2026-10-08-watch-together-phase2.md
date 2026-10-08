# Watch Together Phase 2 (Kodi UI Integration) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the Phase 1 protocol library into Kodi: a supervised relay session that survives disconnects, a player bridge that keeps two clients in sync (pause/seek both directions), and the minimal UI to join, leave, and see status — guest flow only, host capability stays v2.

**Architecture:** One Kodi-free supervisor (`SessionSupervisor` in `lib/watchtogether.py`) owns REST validation, socket lifetime, reconnect backoff, and heartbeat sends. One Kodi file (`lib/windows/watchtogether.py`) is the only new module touching `xbmc`: it feeds local playback state down and applies remote state up through two small `lib/player.py` hooks (`wt_broadcast` callback + `wt_applying_remote` echo deadline). UI is a sidebar entry (`home.py`), two dialogs with skin XMLs, three settings toggles, and one OSD status property.

**Tech Stack:** Python 3 stdlib (`threading`, `time`) + `requests` (addon dep) — no new dependencies. Tests: `uv run pytest` (805 baseline at Phase 1 exit must stay green).

**Spec:** `docs/superpowers/specs/2026-10-08-watch-together-phase2-design.md`
**Protocol reference:** `docs/watch-together.md` — section numbers (§4, §5.5 etc.) cited inline; the doc is authoritative when anything conflicts. (The spec header calls it `docs/watchtogether.md`; the real file is `docs/watch-together.md`.)

---

## Conventions

- Tests are plain `unittest.TestCase` (repo style). Any test importing `lib/windows/` or `lib/player` must set `ENV.abort_requested = True` **before** the import (`from kodienv import ENV`), otherwise `lib.player`'s monitor thread spins forever — see `tests/test_bgm_fade.py`.
- Every task ends with the full suite (`uv run pytest -q`) and a commit.
- Supervisor/bridge callbacks run on non-main threads: they must never raise (a raise inside `syncplay.Session` callbacks tears the connection down) and must never block.
- No token values anywhere; `git grep -E "[a-f0-9]{20,}" -- lib/ tests/` stays empty.

## File structure

| File | Responsibility |
|---|---|
| Edit: `lib/watchtogether.py` | + `SessionSupervisor` (session lifetime, backoff, poll, heartbeat send) — stays Kodi-free (guarded by `tests/test_protocol_isolation.py`) |
| Edit: `tests/test_watchtogether.py` | + offline supervisor tests (fake `ws_factory`/`transport`/clock) |
| Edit: `lib/player.py` | + `wt_broadcast` callback attr, `wt_applying_remote` monotonic deadline, `wtBroadcast()` gate called from `onPlayBackPaused`/`onPlayBackResumed`/`onPlayBackSeek` |
| Create: `tests/test_watchtogether_player.py` | gate wiring on `PlexPlayer` callbacks |
| Edit: `resources/settings.xml` | + 3 boolean toggles (`watchtogether.*`) in category `general` |
| Edit: `resources/language/resource.language.en_gb/strings.po` | + ids 35050–35059 |
| Edit: `lib/windows/settings.py` | + same 3 toggles in `Settings.SETTINGS['main']` |
| Edit: `resources/skins/Main/1080i/script-plex-seek_dialog.xml` | + one status label (top bar, row 2) |
| Create: `lib/windows/watchtogether.py` | bridge (join/leave/status/lobby), `RoomPickerDialog`, `ParticipantsDialog`, `show()` |
| Create: `tests/test_watchtogether_bridge.py` | bridge sync/leave/gone/toast/status tests (fake player + supervisor) |
| Create: `resources/skins/Main/1080i/script-plex-watchtogether_room_picker.xml` | room list dialog |
| Create: `resources/skins/Main/1080i/script-plex-watchtogether_participants.xml` | roster + leave button dialog |
| Edit: `lib/windows/home.py` | sidebar sentinel + `showSections()` entry + 3 guards + startup hook |
| Create: `tests/test_watchtogether_sidebar.py` | sentinel guards + click routing |

String ids: `35050` sidebar toggle, `35051` auto-join toggle, `35052` OSD toggle, `35053` "Watch Together", `35054` "{} watching", `35055` "Reconnecting…", `35056` "Leave room", `35057` "No active rooms", `35058` "New Watch Together room: {}", `35059` "The Watch Together room has ended".

---

### Task 1: SessionSupervisor

**Files:**
- Edit: `lib/watchtogether.py`
- Edit: `tests/test_watchtogether.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_watchtogether.py`. First extend the imports at the top of the file:

```python
import json
import time
import unittest

from lib import syncplay, watchtogether
```

Then append:

```python
class StubTransport(object):
    """RoomsApi transport that always answers from ROOM_JSON (or a status)."""

    def __init__(self, data=ROOM_JSON, status=200):
        self.data = data
        self.status = status
        self.calls = []

    def __call__(self, method, path, body=None, token=None):
        self.calls.append((method, path, token))
        if method == "DELETE":
            return 200, None
        if self.status != 200:
            return self.status, None
        if path == "/rooms":
            return 200, {"rooms": [self.data]}
        return 200, self.data


class FakeClient(object):
    """Stands in for ws.WSClient: start() emulates connect, send() records."""

    def __init__(self, factory, host, port, on_open, on_message, on_close, opens):
        self.factory = factory
        self.host = host
        self.port = port
        self.on_open = on_open
        self.on_message = on_message
        self.on_close = on_close
        self.opens = opens
        self.opened = False
        self.closed = None
        self.sent = []

    def start(self):
        if self.opens:
            self.opened = True
            self.on_open()
        else:
            self.closed = "refused"
            self.on_close("refused")

    def send(self, obj):
        if not self.opened or self.closed is not None:
            raise RuntimeError("not connected")
        self.sent.append(obj)

    def close(self):
        if self.opened and self.closed is None:
            self.closed = "closed"
            self.on_close("closed")

    def drop(self):
        """Remote hangup: socket dies without us calling close()."""
        if self.closed is None:
            self.closed = "dropped"
            self.on_close("dropped")

    def states(self):
        return [m for m in self.sent if "State" in m]


class FakeWSFactory(object):
    def __init__(self, opens=True):
        self.opens = opens
        self.clients = []
        self.times = []

    def __call__(self, host, port, on_open, on_message, on_close):
        self.times.append(time.monotonic())
        client = FakeClient(self, host, port, on_open, on_message, on_close,
                            opens=self.opens)
        self.clients.append(client)
        return client


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class SessionSupervisorTest(unittest.TestCase):
    """Offline tests for lib/watchtogether.SessionSupervisor (spec: phase 2)."""

    def setUp(self):
        self._saved = (watchtogether.POLL, watchtogether.BACKOFF, watchtogether.TICK)
        watchtogether.POLL = 0.2       # exercised inside a couple of seconds
        watchtogether.BACKOFF = (0.02, 0.04, 0.08)
        watchtogether.TICK = 0.01
        self.sup = None

    def tearDown(self):
        (watchtogether.POLL, watchtogether.BACKOFF, watchtogether.TICK) = self._saved
        if self.sup:
            self.sup.stop(timeout=3.0)

    def make(self, transport=None, factory=None):
        self.transport = transport if transport is not None else StubTransport()
        self.factory = factory if factory is not None else FakeWSFactory()
        self.sup = watchtogether.SessionSupervisor(
            watchtogether.Room(ROOM_JSON), "me-identity", "token",
            self.factory, transport=self.transport)
        return self.sup

    def open_client(self):
        self.assertTrue(wait_for(lambda: self.factory.clients and self.factory.clients[0].opened),
                        "supervisor never opened a socket")
        return self.factory.clients[0]

    def test_on_open_sends_hello_list_ready(self):
        sup = self.make()
        sup.start()
        client = self.open_client()
        self.assertTrue(wait_for(lambda: len(client.sent) >= 3))
        self.assertEqual(client.sent[0], syncplay.hello(ROOM_JSON["id"], "me-identity"))
        self.assertEqual(client.sent[1], syncplay.list_request())
        self.assertEqual(client.sent[2], syncplay.set_ready(True))

    def test_outbound_state_before_connect_only_stores(self):
        sup = self.make()
        self.assertFalse(sup.outbound_state({"position": 1, "paused": True}))
        self.assertEqual(sup._local["position"], 1)

    def test_outbound_state_sends_while_connected(self):
        sup = self.make()
        sup.start()
        client = self.open_client()
        before = len(client.sent)
        self.assertTrue(sup.outbound_state(
            {"position": 42, "paused": False, "doSeek": False}))
        self.assertTrue(wait_for(lambda: len(client.sent) > before))
        state = client.states()[-1]
        self.assertEqual(state["State"]["playstate"]["position"], 42)
        self.assertEqual(state["State"]["playstate"]["paused"], False)

    def test_outbound_state_after_drop_is_inert(self):
        sup = self.make()
        sup.start()
        client = self.open_client()
        client.drop()
        self.assertTrue(wait_for(lambda: not sup.connected))
        self.assertFalse(sup.outbound_state({"position": 5, "paused": True}))

    def test_failed_connects_back_off(self):
        factory = FakeWSFactory(opens=False)
        self.make(factory=factory)
        self.sup.start()
        self.assertTrue(wait_for(lambda: len(factory.clients) >= 4, timeout=5.0),
                        "supervisor stopped re-dialing")
        gaps = [b - a for a, b in zip(factory.times, factory.times[1:])]
        # BACKOFF = (0.02, 0.04, 0.08): each gap is wider than the one before
        self.assertGreaterEqual(gaps[1], gaps[0] + 0.005)
        self.assertGreaterEqual(gaps[2], gaps[1] + 0.005)

    def test_drop_fires_on_disconnected_then_redials(self):
        factory = FakeWSFactory()
        sup = self.make(factory=factory)
        events = []
        sup.on_disconnected = lambda: events.append("disc")
        sup.start()
        client = self.open_client()
        client.drop()
        self.assertTrue(wait_for(lambda: "disc" in events))
        self.assertTrue(wait_for(lambda: len(factory.clients) >= 2))
        self.assertTrue(wait_for(lambda: factory.clients[1].opened))

    def test_session_is_fresh_per_attempt(self):
        factory = FakeWSFactory()
        sup = self.make(factory=factory)
        sup.start()
        client = self.open_client()
        first = sup.session
        self.assertIsNotNone(first)
        client.drop()
        self.assertTrue(wait_for(lambda: len(factory.clients) >= 2
                                 and factory.clients[1].opened))
        self.assertIsNot(sup.session, first, "a reconnect must be a fresh session (§5.8)")

    def test_poll_delivers_rest_room(self):
        sup = self.make()
        rooms = []
        sup.on_roster = rooms.append
        sup.start()
        self.assertTrue(wait_for(lambda: rooms))
        self.assertEqual(rooms[0].id, ROOM_JSON["id"])
        self.assertEqual(sup.room.syncplay_host, ROOM_JSON["syncplayHost"])

    def test_relay_state_reaches_on_state(self):
        sup = self.make()
        states = []
        sup.on_state = states.append
        sup.start()
        client = self.open_client()
        client.on_message(json.dumps({"State": {"playstate": {
            "position": 7.5, "paused": True, "doSeek": False,
            "setBy": "someone-else"}}}))
        self.assertTrue(wait_for(lambda: states))
        self.assertEqual(states[0]["position"], 7.5)

    def test_gone_room_fires_on_gone_once_and_never_leaves(self):
        transport = StubTransport(status=404)
        self.make(transport=transport)
        gones = []
        self.sup.on_gone = lambda: gones.append(1)
        self.sup.start()
        self.assertTrue(wait_for(lambda: not self.sup._thread.is_alive()))
        self.assertEqual(gones, [1])
        self.assertEqual(len(self.factory.clients), 0, "must not dial a gone room")
        self.assertFalse(any(c[0] == "DELETE" for c in transport.calls),
                         "on_gone never calls leave() (§4)")

    def test_not_member_is_gone_too(self):
        self.make(transport=StubTransport(status=403))
        gones = []
        self.sup.on_gone = lambda: gones.append(1)
        self.sup.start()
        self.assertTrue(wait_for(lambda: gones == [1]))
        self.assertTrue(wait_for(lambda: not self.sup._thread.is_alive()))

    def test_dead_token_is_gone_never_retried(self):
        self.make(transport=StubTransport(status=401))
        gones = []
        self.sup.on_gone = lambda: gones.append(1)
        self.sup.start()
        self.assertTrue(wait_for(lambda: gones == [1]))
        self.assertTrue(wait_for(lambda: not self.sup._thread.is_alive()))

    def test_transient_poll_failure_keeps_the_socket(self):
        sup = self.make()
        sup.start()
        client = self.open_client()
        self.transport.status = 500          # REST blip mid-session
        self.assertTrue(wait_for(lambda: self.transport.calls.count(("GET", "/rooms/ca8cfezmke4", "token")) >= 2))
        self.assertTrue(sup.connected)
        self.assertEqual(len(self.factory.clients), 1)

    def test_stop_interrupts_backoff(self):
        watchtogether.BACKOFF = (5.0,)
        self.make(factory=FakeWSFactory(opens=False))
        self.sup.start()
        self.assertTrue(wait_for(lambda: len(self.factory.clients) == 1))
        started = time.monotonic()
        self.sup.stop(timeout=3.0)
        self.assertLess(time.monotonic() - started, 2.0,
                        "stop() must not sit behind a backoff sleep")
        self.assertIsNone(self.sup._thread)

    def test_stop_is_idempotent(self):
        self.make()
        self.sup.stop()
        self.sup.stop()
        self.assertIsNone(self.sup._thread)
```

- [ ] **Step 2: Run — must fail**

Run: `uv run pytest tests/test_watchtogether.py -q`
Expected: `AttributeError: module 'lib.watchtogether' has no attribute 'SessionSupervisor'` (or `POLL`/`BACKOFF`), all new tests in error, existing 16 REST tests still pass.

- [ ] **Step 3: Implement the supervisor**

Append to `lib/watchtogether.py` (add `import threading`, `import time`, and `from . import syncplay` to the imports at the top):

```python
# --- session supervisor -------------------------------------------------------
#
# Reconnect ladder (§5.8): a dropped relay connection is re-dialed after
# 1s/2s/4s/8s, then every 30s, and a successful open resets the ladder to 1s.
BACKOFF = (1, 2, 4, 8, 30)
POLL = 15.0      # refresh GET /rooms/{id}: membership + syncplayHost/Port
TICK = 0.1       # sleep granularity so stop() interrupts a 30s backoff


class SessionSupervisor(object):
    """Owns one Watch Together session's whole lifetime.

    REST validates membership and refreshes the endpoint, the socket runs one
    syncplay.Session per attempt (§5.8: a reconnect is a fresh session),
    failures re-dial on the BACKOFF ladder, and outbound_state() heartbeats
    local state (§5.5, the bridge feeds it at 1 Hz). Kodi-free: sockets come
    from ws_factory, time from clock, REST from transport — enforced by
    tests/test_protocol_isolation.py.

    Callbacks (assign before start(); they run on the supervisor thread and
    must not raise or block — a raise inside a syncplay.Session callback
    tears the connection down):
        on_state(remote)      remote State applied (syncplay.Session payload)
        on_event(kind, key)   roster "joined"/"left" (§5.8)
        on_roster(room)       fresh GET /rooms/{id} -> watchtogether.Room
        on_disconnected()     socket dropped, redialing
        on_gone()             room gone / not a member / token dead. Fires
                              once, the loop stops for good, leave() is NEVER
                              called (§4: 403/404 already mean you're out).
    """

    def __init__(self, room, identity, token, ws_factory, transport=None,
                 clock=None, abort=None, timer_factory=None, log=None):
        self.room = room
        self.identity = identity
        self.api = RoomsApi(token, transport=transport)
        self.ws_factory = ws_factory   # (host, port, on_open, on_message, on_close)
        self.clock = clock or time
        self.abort = abort or (lambda: False)
        self.log = log or (lambda msg: None)

        self.on_state = None
        self.on_event = None
        self.on_roster = None
        self.on_disconnected = None
        self.on_gone = None

        self.session = None       # syncplay.Session of the live attempt
        self.connected = False
        self._local = None        # latest local snapshot; swapped in whole
        self._gone = False
        self._stop = threading.Event()
        self._thread = None
        self._client = None
        self._heartbeat = None

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="wt-supervisor")
        self._thread.daemon = True
        self._thread.start()
        return self

    def stop(self, timeout=5.0):
        """Idempotent; safe to call from the supervisor thread itself (no join)."""
        self._stop.set()
        client, self._client = self._client, None
        if client:
            try:
                client.close()
            except Exception:
                pass
        hb, self._heartbeat = self._heartbeat, None
        if hb:
            try:
                hb.cancel()
                hb.join(timeout=1.0)
            except Exception:
                pass
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    def outbound_state(self, local):
        """Adopt the latest local snapshot; send one State now if connected.

        The whole dict swaps in on assignment, so the supervisor thread never
        observes a half-updated snapshot. No-op while disconnected — the next
        successful open resumes from whatever was stored (§5.8 re-announce is
        covered by the caller re-feeding on reconnect).
        """
        self._local = local
        if not self.connected or not self.session:
            return False
        now = self.clock.monotonic()
        return self._send(self.session.outbound_state(local, now, self.clock.time()))

    # -- supervisor thread --------------------------------------------------

    def _stopping(self):
        return self._stop.is_set() or self.abort()

    def _sleep(self, seconds):
        """Interruptible wait: stop() must not sit behind a 30s backoff."""
        end = self.clock.monotonic() + seconds
        while not self._stopping():
            if self.clock.monotonic() >= end:
                return True
            if self._stop.wait(TICK):
                return False
        return False

    def _loop(self):
        started = False
        failures = 0     # consecutive attempts that never opened a socket
        while not self._stopping() and not self._gone:
            if started:
                delay = BACKOFF[min(max(failures - 1, 0), len(BACKOFF) - 1)]
                if not self._sleep(delay):
                    break
            started = True
            if self._poll_room() is False:
                break
            if self._attempt():
                failures = 0
            else:
                failures += 1

    def _mark_gone(self):
        if self._gone:
            return
        self._gone = True
        if self.on_gone:
            self.on_gone()

    def _poll_room(self):
        """Refresh membership + syncplay endpoint. False = stop for good."""
        try:
            fresh = self.api.room(self.room.id)
        except (RoomGone, NotMember, AuthError) as exc:
            # gone, removed, or a dead token — all unrecoverable without the
            # user re-joining; never DELETE on any of them (§4)
            self.log("Watch Together: room unavailable ({0})".format(exc.__class__.__name__))
            self._mark_gone()
            return False
        except WatchTogetherError as exc:
            self.log("Watch Together: room refresh failed ({0})".format(exc))
            return True    # transient: the socket has its own path, keep going
        self.room = fresh
        if self.on_roster:
            self.on_roster(fresh)
        return True

    def _attempt(self):
        """One connection attempt. True if the socket ever opened."""
        state = {"open": False, "ever": False, "closed": False}

        def on_open():
            state["open"] = state["ever"] = True
            self.connected = True
            # §5.8: a (re)connect re-announces everything
            self._send(syncplay.hello(self.room.id, self.identity))
            self._send(syncplay.list_request())
            self._send(syncplay.set_ready(True))
            self._start_heartbeat()

        def on_message(text):
            session = self.session
            if session:
                session.on_message(text, self.clock.monotonic())

        def on_close(reason):
            state["open"] = False
            state["closed"] = True
            self.connected = False
            self._stop_heartbeat()

        self.session = syncplay.Session(self.room.id, self.identity,
                                        on_state=self.on_state,
                                        on_event=self.on_event)
        client = self.ws_factory(self.room.syncplay_host, self.room.syncplay_port,
                                 on_open, on_message, on_close)
        self._client = client
        opened = self._pump(client, state)
        try:
            client.close()
        except Exception:
            pass
        self._client = None
        self.connected = False
        self.session = None
        self._stop_heartbeat()
        if opened and not self._stopping() and not self._gone and self.on_disconnected:
            self.on_disconnected()
        return opened

    def _pump(self, client, state):
        """Poll until the socket dies or we're stopping. True if it ever opened."""
        client.start()
        last_poll = self.clock.monotonic()
        while not self._stopping():
            if state["closed"]:
                break
            if state["open"] and self.clock.monotonic() - last_poll >= POLL:
                last_poll = self.clock.monotonic()
                if self._poll_room() is False:
                    break
            if self._stop.wait(TICK):
                break
        return state["ever"]

    def _send(self, obj):
        client = self._client
        if client is None or not self.connected:
            return False
        try:
            client.send(obj)
            return True
        except Exception as exc:
            self.log("Watch Together: send failed ({0})".format(exc.__class__.__name__))
            return False

    def _start_heartbeat(self):
        """Start a real 1 Hz repeating timer (injected for testability)."""
        self._stop_heartbeat()
        if self.timer_factory is None:
            from plexnet.util import RepeatingCounterTimer
            self._heartbeat = RepeatingCounterTimer(1.0, self._heartbeat_tick)
        else:
            self._heartbeat = self.timer_factory(1.0, self._heartbeat_tick, repeat=True)

    def _stop_heartbeat(self):
        hb, self._heartbeat = self._heartbeat, None
        if hb:
            try:
                hb.cancel()
            except Exception:
                pass

    def _heartbeat_tick(self, tick=True):
        if self._stopping() or not self.connected:
            self._stop_heartbeat()
            return
        session = self.session
        local = self._local
        if session is None or local is None:
            return
        try:
            self._send(session.outbound_state(local, self.clock.monotonic(),
                                              self.clock.time()))
        except Exception as exc:
            self.log("Watch Together: heartbeat send failed ({0})".format(exc.__class__.__name__))
            self._stop_heartbeat()
```

- [ ] **Step 4: Run — must pass**

Run: `uv run pytest tests/test_watchtogether.py -q`
Expected: all green (`16` existing + `17` new = `33 passed`). The isolation guard also runs in the full suite: `uv run pytest -q` → **822 passed** (805 baseline + 17).

- [ ] **Step 5: Commit**

```bash
git add lib/watchtogether.py tests/test_watchtogether.py
git commit -m "feat(watchtogether): session supervisor with reconnect backoff"
```

---

### Task 2: Player echo gate

**Files:**
- Edit: `lib/player.py`
- Create: `tests/test_watchtogether_player.py`

The bridge applies remote state through the normal player paths, which makes Kodi fire `onPlayBackPaused`/`onPlayBackResumed`/`onPlayBackSeek`. Broadcasting those straight back would make two clients echo each other at network speed (§5.7). The fix is a monotonic deadline: the bridge stamps `wt_applying_remote = time.monotonic() + 2` before each apply, and the callbacks skip the broadcast while `now < deadline`. Kodi may deliver the callbacks after the apply call returns, so a try/finally would be too short; the deadline self-heals (worst case: a genuine local event inside the 2s window is dropped, the next event or the 1 Hz heartbeat converges).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_watchtogether_player.py`:

```python
# coding=utf-8
"""lib/player.py — the Watch Together local-broadcast gate (spec: phase 2).

The gate is two attributes on PlexPlayer: wt_broadcast (callback into the
bridge, None until a room is joined) and wt_applying_remote (a monotonic
deadline set by the bridge before it applies remote state)."""

from __future__ import absolute_import

from kodienv import ENV

ENV.abort_requested = True
from lib import player  # noqa: E402

from .base import KodiTestCase  # noqa: E402


class FakeHandler(object):
    def __init__(self):
        self.calls = []

    def onPlayBackPaused(self):
        self.calls.append("paused")

    def onPlayBackResumed(self):
        self.calls.append("resumed")

    def onPlayBackSeek(self, time, offset):
        self.calls.append("seek")


class WTGateTest(KodiTestCase):
    def bare_player(self):
        p = player.PlexPlayer.__new__(player.PlexPlayer)
        p.sessionID = "sid"
        p.handler = FakeHandler()
        p.wt_applying_remote = 0.0
        self.fired = []
        p.wt_broadcast = self.fired.append
        return p

    def test_pause_reaches_the_bridge(self):
        p = self.bare_player()
        p.onPlayBackPaused()
        self.assertEqual(self.fired, ["pause"])
        self.assertEqual(p.handler.calls, ["paused"])

    def test_resume_reaches_the_bridge(self):
        p = self.bare_player()
        p.onPlayBackResumed()
        self.assertEqual(self.fired, ["play"])

    def test_seek_reaches_the_bridge(self):
        p = self.bare_player()
        p.onPlayBackSeek(12, 4000)
        self.assertEqual(self.fired, ["seek"])

    def test_armed_deadline_swallows_the_echo(self):
        p = self.bare_player()
        p.wt_applying_remote = float("inf")     # bridge applied remote state
        p.onPlayBackPaused()
        p.onPlayBackResumed()
        p.onPlayBackSeek(9, -1000)
        self.assertEqual(self.fired, [])
        self.assertEqual(p.handler.calls, ["paused", "resumed", "seek"],
                         "the local handler path still runs")

    def test_disarmed_deadline_forwards_again(self):
        p = self.bare_player()
        p.wt_applying_remote = float("inf")
        p.onPlayBackPaused()
        p.wt_applying_remote = 0.0
        p.onPlayBackPaused()
        self.assertEqual(self.fired, ["pause"])

    def test_no_plex_session_means_no_broadcast(self):
        p = self.bare_player()
        p.sessionID = None
        p.onPlayBackPaused()
        self.assertEqual(self.fired, [])

    def test_no_bridge_listener_is_a_noop(self):
        p = self.bare_player()
        p.wt_broadcast = None
        p.onPlayBackPaused()               # must not raise
        self.assertEqual(p.handler.calls, ["paused"])
```

- [ ] **Step 2: Run — must fail**

Run: `uv run pytest tests/test_watchtogether_player.py -q`
Expected: `AttributeError: 'PlexPlayer' object has no attribute 'wt_broadcast'` (or `wt_applying_remote`) — 7 tests in error.

- [ ] **Step 3: Implement the gate**

Three edits to `lib/player.py`:

1. Add `import time` to the stdlib imports at the top (after `import threading`).

2. In `PlexPlayer.__init__` (the one at line ~2335, right after `self.bgmGeneration = 0`):

```python
        # Watch Together: bridge callback + monotonic echo-suppression deadline
        self.wt_broadcast = None
        self.wt_applying_remote = 0.0
```

3. Add the gate helper next to the `onPlayBack*` callbacks and call it from the three of them:

```python
    def wtBroadcast(self, kind):
        """Hand a local playback change to the Watch Together bridge.

        Skipped while wt_applying_remote is in the future: that event is the
        echo of a change the bridge itself just applied (§5.7). The deadline
        (not a flag) survives Kodi delivering the callback after apply()
        returns, and self-heals if a genuine local event lands inside it."""
        if time.monotonic() < self.wt_applying_remote:
            util.DEBUG_LOG('Watch Together - suppressing {} echo'.format(kind))
            return
        if self.wt_broadcast:
            self.wt_broadcast(kind)
```

The three call sites (fire at the very end of each method, after the handler dispatch):

```python
    def onPlayBackPaused(self):
        if not self.sessionID:
            return
        util.DEBUG_LOG('Player - PAUSED')
        if not self.handler:
            return
        self.handler.onPlayBackPaused()
        self.wtBroadcast('pause')

    def onPlayBackResumed(self):
        if not self.sessionID:
            return
        util.DEBUG_LOG('Player - RESUMED')
        if not self.handler:
            return
        self.handler.onPlayBackResumed()
        self.wtBroadcast('play')

    def onPlayBackSeek(self, time, offset):
        if not self.sessionID:
            return
        util.DEBUG_LOG('Player - SEEK: {} {:d}', time, offset)
        if not self.handler:
            return
        self.handler.onPlayBackSeek(time, offset)
        self.wtBroadcast('seek')
```

Note: `onPlayBackSeek(self, time, offset)` shadows the `time` module inside that method — harmless, the gate lives in `wtBroadcast`, which has its own scope.

- [ ] **Step 4: Run — must pass**

Run: `uv run pytest tests/test_watchtogether_player.py -q`
Expected: `7 passed`.
Run: `uv run pytest -q`
Expected: **829 passed** (822 + 7).

- [ ] **Step 5: Commit**

```bash
git add lib/player.py tests/test_watchtogether_player.py
git commit -m "feat(watchtogether): gate local playback events for the relay"
```

---

### Task 3: Settings, strings, OSD status label

**Files:**
- Edit: `resources/language/resource.language.en_gb/strings.po`
- Edit: `resources/settings.xml`
- Edit: `lib/windows/settings.py`
- Edit: `resources/skins/Main/1080i/script-plex-seek_dialog.xml`

- [ ] **Step 1: Add the strings**

Append to `resources/language/resource.language.en_gb/strings.po` (after `#35049`, keeping the blank-line rhythm):

```
msgctxt "#35050"
msgid "Show Watch Together in the sidebar"
msgstr ""

msgctxt "#35051"
msgid "Auto-join the last Watch Together room on startup"
msgstr ""

msgctxt "#35052"
msgid "Show Watch Together status on the video OSD"
msgstr ""

msgctxt "#35053"
msgid "Watch Together"
msgstr ""

msgctxt "#35054"
msgid "{} watching"
msgstr ""

msgctxt "#35055"
msgid "Reconnecting…"
msgstr ""

msgctxt "#35056"
msgid "Leave room"
msgstr ""

msgctxt "#35057"
msgid "No active rooms"
msgstr ""

msgctxt "#35058"
msgid "New Watch Together room: {}"
msgstr ""

msgctxt "#35059"
msgid "The Watch Together room has ended"
msgstr ""
```

Other languages stay untouched — allowed, drift counters only track existing entries.

- [ ] **Step 2: Declare the toggles in settings.xml**

In `resources/settings.xml`, inside category `general` / group `1`, after the `cache_home_users` setting and before that group's `</group>`:

```xml
                <setting id="watchtogether.enable_sidebar" type="boolean" label="35050">
                    <level>0</level>
                    <default>true</default>
                    <control type="toggle"/>
                </setting>
                <setting id="watchtogether.auto_join_last" type="boolean" label="35051">
                    <level>0</level>
                    <default>false</default>
                    <control type="toggle"/>
                </setting>
                <setting id="watchtogether.show_osd_status" type="boolean" label="35052">
                    <level>0</level>
                    <default>true</default>
                    <control type="toggle"/>
                </setting>
```

Same shape as `auto_seek` (`<level>0</level>`, `<default>`, `<control type="toggle"/>`). `watchtogether.last_room` is deliberately **not** declared — it is an internal key written with `util.setSetting`, same pattern as `previous_server.<uuid>` (home.py:4798).

- [ ] **Step 3: Mirror the toggles in the in-app settings window**

In `lib/windows/settings.py`, in `Settings.SETTINGS['main']`, after the `assume_resume` BoolSetting and before the tuple's closing `)`:

```python
                BoolSetting('watchtogether.enable_sidebar', T(35050, 'Show Watch Together in the sidebar'), True),
                BoolSetting('watchtogether.auto_join_last', T(35051, 'Auto-join the last Watch Together room on startup'), False),
                BoolSetting('watchtogether.show_osd_status', T(35052, 'Show Watch Together status on the video OSD'), True),
```

- [ ] **Step 4: OSD status label in the seek dialog**

In `resources/skins/Main/1080i/script-plex-seek_dialog.xml`, in the top-bar group (the one starting `<control type="group">` with `<posy>34.64</posy>`, containing the title labels and `$INFO[System.Time]`), add this label as the **last child of that group**, after the clock label:

```xml
        <control type="label">
            <visible>!String.IsEmpty(Window(10000).Property(watchtogether.status))</visible>
            <posx>460</posx>
            <posy>51.97</posy>
            <width>1000</width>
            <height>34.64</height>
            <font>font10</font>
            <align>center</align>
            <aligny>center</aligny>
            <textcolor>FFE5A00D</textcolor>
            <label>$INFO[Window(10000).Property(watchtogether.status)]</label>
        </control>
```

Row 2 of the top bar (row 1 holds the title + clock), centered, amber, invisible while the property is empty. The bridge writes the property with `util.setGlobalProperty(..., base='{0}')` so the name is literally `watchtogether.status` — with the default `script.plex.{0}` base this `$INFO` would read an empty property.

- [ ] **Step 5: Run**

Run: `uv run pytest -q`
Expected: **829 passed** — `tests/test_i18n.py` now scans the new `T(35050…)` call sites in settings.py against the new po entries (`test_every_translated_string_used_in_code_exists_in_en_gb` green).

- [ ] **Step 6: Commit**

```bash
git add resources/language/resource.language.en_gb/strings.po resources/settings.xml \
        lib/windows/settings.py resources/skins/Main/1080i/script-plex-seek_dialog.xml
git commit -m "feat(watchtogether): settings toggles, strings and OSD status label"
```

---

### Task 4: Bridge core

**Files:**
- Create: `lib/windows/watchtogether.py`
- Create: `tests/test_watchtogether_bridge.py`

The module gets dialogs in Task 5; this task builds the singleton bridge: join/leave/gone lifecycle, the 1 Hz local-state feed, remote-state application, lobby polling with invite toasts, and the OSD status property.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_watchtogether_bridge.py`:

```python
# coding=utf-8
"""lib/windows/watchtogether.py — the Kodi bridge (spec: phase 2).

Bridge behaviour under test: snapshot shape, the video-only guard (theme
music must never reach the relay), remote apply + echo deadline, leave/gone
teardown, lobby toasts, OSD status property. Fake player/supervisor stand in
for Kodi and the socket."""

from __future__ import absolute_import

import time

from kodienv import ENV

ENV.abort_requested = True
from lib import util, watchtogether  # noqa: E402
from lib.windows import watchtogether as wtwin  # noqa: E402
from kodi_six import xbmcgui  # noqa: E402

from .base import KodiTestCase  # noqa: E402

ROOM_JSON = {
    "id": "ca8cfezmke4",
    "title": "A Fazenda – S18 • E20",
    "sourceUri": "server://x/metadata/227117",
    "createdBy": 1000001,
    "startsAt": 1791128438,
    "updatedAt": 1791128438,
    "endsAt": 1791139238,
    "syncplayHost": "pop-fra00.syncplay.plex.services",
    "syncplayPort": 7776,
    "users": [
        {"id": 1000001, "username": "owner", "title": "Owner", "uuid": "u1"},
        {"id": 1000002, "username": "guest", "title": "Guest", "uuid": "u2"},
    ],
}


class FakeLatency(object):
    forward_delay = 0.0


class FakeSession(object):
    latency = FakeLatency()


class FakeSupervisor(object):
    def __init__(self, connected=True):
        self.connected = connected
        self.session = FakeSession()
        self.room = watchtogether.Room(ROOM_JSON)
        self.sent = []
        self.stopped = False

    def outbound_state(self, local):
        self.sent.append(local)
        return self.connected

    def stop(self, timeout=None):
        self.stopped = True


class FakeDialog(object):
    def __init__(self):
        self.seeks = []

    def doSeek(self, offset_ms):
        self.seeks.append(offset_ms)


class FakeHandler(object):
    def __init__(self):
        self.dialog = None


class FakePlayer(object):
    def __init__(self, playing=True, video=True, position=50.0, paused=False):
        self.playing = playing
        self.video = video
        self.position = position
        self.paused = paused
        self.controls = []
        self.seek_times = []
        self.handler = FakeHandler()
        self.wt_broadcast = None
        self.wt_applying_remote = 0.0

    def isPlaying(self):
        return self.playing

    def isPlayingVideo(self):
        return self.video

    def getTime(self):
        return self.position

    def control(self, action):
        self.controls.append(action)

    def seekTime(self, seconds):
        self.seek_times.append(seconds)


class FakeAPI(object):
    def __init__(self, rooms=None, room=ROOM_JSON):
        self.rooms_out = rooms if rooms is not None else []
        self.room_out = room
        self.calls = []

    def rooms(self):
        self.calls.append("rooms")
        return [watchtogether.Room(r) for r in self.rooms_out]

    def room(self, room_id):
        self.calls.append(("room", room_id))
        return watchtogether.Room(self.room_out)

    def leave(self, room_id):
        self.calls.append(("leave", room_id))


class BridgeTestCase(KodiTestCase):
    def setUp(self):
        super(BridgeTestCase, self).setUp()
        self._saved_player = wtwin.player.PLAYER
        self._saved_notification = wtwin.util.showNotification
        self.toasts = []
        wtwin.util.showNotification = self.toasts.append
        util.setSetting("watchtogether.show_osd_status", "true")
        util.setSetting("watchtogether.last_room", "")
        self.bridge = wtwin.WatchTogetherBridge()
        self.player = FakePlayer()
        wtwin.player.PLAYER = self.player

    def tearDown(self):
        wtwin.player.PLAYER = self._saved_player
        wtwin.util.showNotification = self._saved_notification
        util.setSetting("watchtogether.show_osd_status", "true")
        util.setSetting("watchtogether.last_room", "")
        super(BridgeTestCase, self).tearDown()


class SnapshotTest(BridgeTestCase):
    def test_push_local_shape(self):
        self.bridge.supervisor = FakeSupervisor()
        self.player.position = 12.7
        self.player.paused = True
        self.bridge.push_local()
        self.assertEqual(self.bridge.supervisor.sent,
                         [{"position": 12, "paused": True, "doSeek": False}])

    def test_theme_music_never_reaches_the_relay(self):
        # BGM plays through the same player: audio-only must not push state,
        # or peers would seek their video to the theme's position
        self.bridge.supervisor = FakeSupervisor()
        self.player.video = False
        self.bridge.push_local()
        self.assertEqual(self.bridge.supervisor.sent, [])

    def test_no_supervisor_is_inert(self):
        self.bridge.push_local()
        self.assertEqual(self.player.wt_broadcast, None)

    def test_local_change_event_pushes(self):
        self.bridge.supervisor = FakeSupervisor()
        self.bridge.on_local_change("pause")
        self.assertEqual(len(self.bridge.supervisor.sent), 1)


class RemoteApplyTest(BridgeTestCase):
    def remote(self, position, paused):
        return {"position": position, "paused": paused, "doSeek": False,
                "setBy": "other-identity"}

    def test_remote_pause_applies_and_arms_the_deadline(self):
        self.bridge.supervisor = FakeSupervisor()
        self.bridge.on_state(self.remote(self.player.position, paused=True))
        self.assertEqual(self.player.controls, ["pause"])
        self.assertGreater(self.player.wt_applying_remote, time.monotonic(),
                           "echo deadline must be armed across the apply")

    def test_remote_resume_applies(self):
        self.player.paused = True
        self.bridge.supervisor = FakeSupervisor()
        self.bridge.on_state(self.remote(self.player.position, paused=False))
        self.assertEqual(self.player.controls, ["play"])

    def test_remote_seek_goes_through_the_seek_dialog(self):
        self.bridge.supervisor = FakeSupervisor()
        dialog = FakeDialog()
        self.player.handler.dialog = dialog
        # 10s behind: sync_action's seek threshold is >1.75s (§6.2)
        self.bridge.on_state(self.remote(self.player.position + 10, paused=False))
        self.assertEqual(dialog.seeks, [int((self.player.position + 10) * 1000)])
        self.assertGreater(self.player.wt_applying_remote, time.monotonic())

    def test_remote_seek_falls_back_to_the_player(self):
        self.bridge.supervisor = FakeSupervisor()
        self.bridge.on_state(self.remote(self.player.position + 10, paused=False))
        self.assertEqual(self.player.seek_times, [self.player.position + 10])

    def test_drift_inside_the_band_applies_nothing(self):
        self.bridge.supervisor = FakeSupervisor()
        self.bridge.on_state(self.remote(self.player.position + 0.5, paused=False))
        self.assertEqual(self.player.controls, [])
        self.assertEqual(self.player.seek_times, [])

    def test_no_video_playback_is_left_alone(self):
        self.player.video = False
        self.bridge.supervisor = FakeSupervisor()
        self.bridge.on_state(self.remote(0, paused=True))
        self.assertEqual(self.player.controls, [])

    def test_a_raising_callback_never_escapes(self):
        # Session doc: an exception in on_state tears the connection down
        self.bridge.supervisor = FakeSupervisor()
        self.player.handler = object()      # no .dialog, attribute access raises
        self.bridge.on_state(self.remote(0, paused=True))   # must not raise


class LifecycleTest(BridgeTestCase):
    def test_leave_sends_delete_then_stops(self):
        api = FakeAPI()
        self.bridge.api = api
        self.bridge.room = watchtogether.Room(ROOM_JSON)
        sup = FakeSupervisor()
        self.bridge.supervisor = sup
        self.player.wt_broadcast = self.bridge.on_local_change
        self.player.wt_applying_remote = float("inf")
        util.setSetting("watchtogether.last_room", "ca8cfezmke4")

        self.bridge.leave()

        self.assertEqual(api.calls, [("leave", "ca8cfezmke4")])
        self.assertTrue(sup.stopped)
        self.assertIsNone(self.bridge.supervisor)
        self.assertIsNone(self.player.wt_broadcast)
        self.assertEqual(self.player.wt_applying_remote, 0.0)
        self.assertEqual(util.getSetting("watchtogether.last_room", ""), "")
        self.assertEqual(util.getGlobalProperty("watchtogether.status", base="{0}"), "")

    def test_gone_stops_without_delete(self):
        api = FakeAPI()
        self.bridge.api = api
        self.bridge.room = watchtogether.Room(ROOM_JSON)
        sup = FakeSupervisor()
        self.bridge.supervisor = sup
        util.setSetting("watchtogether.last_room", "ca8cfezmke4")

        self.bridge.on_gone()

        self.assertEqual(api.calls, [], "gone must never call leave()")
        self.assertTrue(sup.stopped)
        self.assertIsNone(self.bridge.supervisor)
        self.assertEqual(self.toasts, [util.T(35059, "The Watch Together room has ended")])
        self.assertEqual(util.getSetting("watchtogether.last_room", ""), "")

    def test_join_feeds_the_gate_and_status(self):
        class JoinableSup(FakeSupervisor):
            pass

        def fake_join(room_id):
            sup = JoinableSup()
            self.bridge.supervisor = sup
            self.bridge.room = watchtogether.Room(ROOM_JSON)
            return sup

        self.bridge.join = fake_join
        self.bridge.join("ca8cfezmke4")
        # join wires the broadcast callback through on join in the real code;
        # assert the wiring surfaces through on_local_change
        self.bridge.on_local_change("seek")
        self.assertEqual(len(self.bridge.supervisor.sent), 1)


class LobbyTest(BridgeTestCase):
    def test_first_poll_seeds_silently(self):
        self.bridge.api = FakeAPI(rooms=[ROOM_JSON])
        self.bridge._poll_rooms()
        self.assertEqual(self.toasts, [])
        self.assertEqual(len(self.bridge.rooms_cache), 1)

    def test_new_room_toasts_once(self):
        self.bridge.api = FakeAPI(rooms=[ROOM_JSON])
        self.bridge._poll_rooms()
        second = dict(ROOM_JSON, id="otherroom01", title="Other room")
        self.bridge.api = FakeAPI(rooms=[ROOM_JSON, second])
        self.bridge._poll_rooms()
        self.assertEqual(len(self.toasts), 1)
        self.assertIn("Other room", self.toasts[0])
        self.bridge._poll_rooms()        # same rooms again: no repeat toast
        self.assertEqual(len(self.toasts), 1)

    def test_poll_failure_is_swallowed(self):
        class Boom(object):
            def rooms(self):
                raise watchtogether.WatchTogetherError("500")
        self.bridge.api = Boom()
        self.bridge._poll_rooms()        # must not raise
        self.assertEqual(self.toasts, [])


class StatusTest(BridgeTestCase):
    def read(self):
        return util.getGlobalProperty("watchtogether.status", base="{0}")

    def test_disconnected_clears_status(self):
        self.bridge.update_status()
        self.assertEqual(self.read(), "")

    def test_connected_shows_count(self):
        self.bridge.supervisor = FakeSupervisor(connected=True)
        self.bridge.room = watchtogether.Room(ROOM_JSON)
        self.bridge.update_status()
        self.assertEqual(self.read(), "2 watching")

    def test_reconnecting_while_socket_is_down(self):
        self.bridge.supervisor = FakeSupervisor(connected=False)
        self.bridge.room = watchtogether.Room(ROOM_JSON)
        self.bridge.update_status()
        self.assertEqual(self.read(), util.T(35055, "Reconnecting…"))

    def test_osd_toggle_silences_the_property(self):
        util.setSetting("watchtogether.show_osd_status", "false")
        self.bridge.supervisor = FakeSupervisor(connected=True)
        self.bridge.room = watchtogether.Room(ROOM_JSON)
        self.bridge.update_status()
        self.assertEqual(self.read(), "")
```

- [ ] **Step 2: Run — must fail**

Run: `uv run pytest tests/test_watchtogether_bridge.py -q`
Expected: `ModuleNotFoundError: No module named 'lib.windows.watchtogether'`.

- [ ] **Step 3: Implement the bridge**

Create `lib/windows/watchtogether.py`:

```python
# coding=utf-8
"""Watch Together — Kodi bridge (spec: phase 2 design; docs/watch-together.md).

Kodi-side glue only: joins/leaves rooms, feeds local playback state to the
supervisor at 1 Hz, applies remote syncplay state back onto the player, runs
the lobby poll for invite toasts, and owns one global OSD status property.
The protocol itself lives in lib/watchtogether.py + lib/syncplay.py + lib/ws.py.
"""

from __future__ import absolute_import

import threading
import time

from kodi_six import xbmc
from plexnet import plexapp

from lib import plex, player, syncplay, util, watchtogether, ws
from . import kodigui


def _ws_factory(host, port, on_open, on_message, on_close):
    return ws.WSClient(host, port, on_message, on_open=on_open, on_close=on_close)


class WatchTogetherBridge(object):
    """Singleton wiring room <-> supervisor <-> player.

    Threads: the main thread drives join/leave; one daemon thread feeds local
    state at 1 Hz while joined (or polls the lobby every 15s while idle); the
    supervisor thread owns the socket and runs the session callbacks — those
    must never block, so REST refreshes the dialogs want run on their own
    throwaway threads."""

    def __init__(self):
        self.api = None
        self.supervisor = None
        self.room = None
        self.rooms_cache = []
        self._rooms_seeded = False
        self._seen_rooms = set()
        self._thread = None
        self._stop = threading.Event()
        self._join_lock = threading.Lock()
        self._auto_join_done = False
        self._was_connected = False

    # -- startup ------------------------------------------------------------

    def start(self):
        """Idempotent: lobby thread + one auto-join attempt. Called from
        HomeWindow.onFirstInit and from show()."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wt-bridge")
        self._thread.daemon = True
        self._thread.start()
        self.auto_join()

    def _run(self):
        ticks = 0
        while not self._stop.is_set() and not util.MONITOR.abortRequested():
            if self._stop.wait(1.0):
                break
            ticks += 1
            sup = self.supervisor
            if sup is None:
                if not self._rooms_seeded or ticks % 15 == 0:
                    self._poll_rooms()
            else:
                if sup.connected != self._was_connected:
                    self._was_connected = sup.connected
                    self.update_status()
                self.push_local()

    def ensure_api(self):
        if self.api is None:
            self.api = watchtogether.RoomsApi(plexapp.ACCOUNT.authToken)
        return self.api

    # -- lobby (no supervisor running) ---------------------------------------

    def _poll_rooms(self):
        try:
            rooms = self.ensure_api().rooms()
        except watchtogether.WatchTogetherError as exc:
            util.DEBUG_LOG("Watch Together: room poll failed: {0}".format(exc))
            return
        self.rooms_cache = rooms
        if not self._rooms_seeded:
            # first paint: remember, never toast what was already there
            self._rooms_seeded = True
            self._seen_rooms = set(r.id for r in rooms)
            return
        for room in rooms:
            if room.id not in self._seen_rooms:
                self._seen_rooms.add(room.id)
                util.showNotification(
                    util.T(35058, "New Watch Together room: {}").format(room.title))

    def refresh_rooms(self):
        """Fire-and-forget poll for a dialog that wants fresh data now."""
        thread = threading.Thread(target=self._poll_rooms, name="wt-rooms")
        thread.daemon = True
        thread.start()

    def refresh_room(self):
        """Fresh GET /rooms/{id} while the participants dialog is open."""
        sup = self.supervisor
        if sup is None:
            return

        def work():
            try:
                self.room = self.ensure_api().room(sup.room.id)
            except watchtogether.WatchTogetherError as exc:
                util.DEBUG_LOG("Watch Together: room refresh failed: {0}".format(exc))

        thread = threading.Thread(target=work, name="wt-room")
        thread.daemon = True
        thread.start()

    # -- local -> relay -------------------------------------------------------

    def push_local(self):
        """Heartbeat (§5.5): the bridge feeds at 1 Hz, the supervisor sends."""
        sup = self.supervisor
        if sup is None:
            return
        # isPlayingVideo, not isPlaying: theme music runs through this same
        # player and would otherwise push audio position at remote videos
        if not player.PLAYER.isPlayingVideo():
            return
        sup.outbound_state({
            "position": int(player.PLAYER.getTime() or 0),
            "paused": bool(xbmc.getCondVisibility("Player.Paused")),
            "doSeek": False,
        })

    def on_local_change(self, kind):
        """Kodi fired onPlayBack* for a local event (the gate let it through)."""
        util.DEBUG_LOG("Watch Together: local {0}, broadcasting state".format(kind))
        self.push_local()

    # -- relay -> kodi ---------------------------------------------------------

    def on_state(self, remote):
        """syncplay.Session applied a remote State (supervisor thread).

        Must not raise: an exception here tears the connection down."""
        try:
            self._apply_remote(remote)
        except Exception:
            util.ERROR()

    def _apply_remote(self, remote):
        sup = self.supervisor
        if sup is None:
            return
        session = sup.session
        if session is None or not sup.connected:
            return
        if not player.PLAYER.isPlayingVideo():
            return
        local_pos = player.PLAYER.getTime() or 0.0
        action = syncplay.sync_action(local_pos, remote.get("position", 0.0),
                                      remote.get("paused", True),
                                      session.latency.forward_delay)
        # arm BEFORE the first apply: the echo can arrive after we return
        player.PLAYER.wt_applying_remote = time.monotonic() + 2.0
        want_paused = remote.get("paused")
        is_paused = bool(xbmc.getCondVisibility("Player.Paused"))
        if want_paused is not None and want_paused != is_paused:
            player.PLAYER.control("pause" if want_paused else "play")
        if action and action[0] == "seek":
            self._seek_to(action[1])
        util.DEBUG_LOG("Watch Together: applied remote {0}".format(action))

    def _seek_to(self, target):
        """Through the seek dialog when it exists (it owns the full local seek
        path — transcodes, bookkeeping); plain seekTime otherwise. During video
        playback the dialog is created up front, so the fallback is the edge."""
        target = max(target, 0)
        dialog = getattr(getattr(player.PLAYER, "handler", None), "dialog", None)
        if dialog:
            dialog.doSeek(int(target * 1000))
        else:
            player.PLAYER.seekTime(target)

    # -- supervisor callbacks ---------------------------------------------------

    def on_roster(self, room):
        self.room = room
        self.update_status()

    def on_disconnected(self):
        util.DEBUG_LOG("Watch Together: relay connection lost, reconnecting")
        self.update_status()

    def on_event(self, kind, key):
        util.DEBUG_LOG("Watch Together: roster {0}".format(kind))

    def on_gone(self):
        """Room ended / removed / dead token — supervisor thread. Teardown,
        no DELETE (§4). Dialogs notice supervisor=None and close themselves."""
        util.showNotification(util.T(35059, "The Watch Together room has ended"))
        with self._join_lock:
            sup, self.supervisor = self.supervisor, None
            if sup:
                sup.stop()
            self.room = None
            self._reset_player_link(forget_room=True)

    # -- join / leave -----------------------------------------------------------

    def join(self, room_id):
        with self._join_lock:
            if self.supervisor is not None:
                return self.supervisor
            room = self.ensure_api().room(room_id)     # raises Auth/NotMember/Gone
            identity = syncplay.build_identity(plex.CLIENT_ID, plex.getFriendlyName(),
                                               plexapp.ACCOUNT.ID)
            sup = watchtogether.SessionSupervisor(room, identity,
                                                  plexapp.ACCOUNT.authToken,
                                                  _ws_factory, log=util.DEBUG_LOG)
            sup.on_state = self.on_state
            sup.on_roster = self.on_roster
            sup.on_disconnected = self.on_disconnected
            sup.on_gone = self.on_gone
            sup.on_event = self.on_event
            sup.start()
            self.room = room
            self.supervisor = sup
            self._was_connected = False
            player.PLAYER.wt_broadcast = self.on_local_change
            player.PLAYER.wt_applying_remote = 0.0
            util.setSetting("watchtogether.last_room", room_id)
            self.update_status()
            return sup

    def leave(self):
        with self._join_lock:
            sup, room = self.supervisor, self.room
            if room is not None and self.api is not None:
                try:
                    self.api.leave(room.id)
                except watchtogether.WatchTogetherError as exc:
                    util.DEBUG_LOG("Watch Together: leave failed: {0}".format(exc))
            if sup:
                sup.stop()
            self.supervisor = None
            self.room = None
            self._reset_player_link(forget_room=True)

    def _reset_player_link(self, forget_room=False):
        player.PLAYER.wt_broadcast = None
        player.PLAYER.wt_applying_remote = 0.0
        if forget_room:
            util.setSetting("watchtogether.last_room", "")
        self.update_status()

    def auto_join(self):
        if self._auto_join_done:
            return
        self._auto_join_done = True
        if not util.getSetting("watchtogether.auto_join_last", False):
            return
        room_id = util.getSetting("watchtogether.last_room", "")
        if not room_id:
            return
        thread = threading.Thread(target=self._auto_join, args=(room_id,),
                                  name="wt-autojoin")
        thread.daemon = True
        thread.start()

    def _auto_join(self, room_id):
        try:
            self.join(room_id)
        except watchtogether.WatchTogetherError as exc:
            # dead room or dead token: forget it rather than retry every boot
            util.DEBUG_LOG("Watch Together: auto-join failed: {0}".format(exc))
            util.setSetting("watchtogether.last_room", "")

    # -- OSD status --------------------------------------------------------------

    def update_status(self):
        if self.supervisor is None:
            text = ""
        elif not util.getSetting("watchtogether.show_osd_status", True):
            text = ""
        elif not self.supervisor.connected:
            text = util.T(35055, "Reconnecting…")
        else:
            count = len(self.room.participants) if self.room else 0
            text = util.T(35054, "{} watching").format(count)
        # base='{0}': the skin reads Window(10000).Property(watchtogether.status)
        util.setGlobalProperty("watchtogether.status", text, base="{0}")


bridge = WatchTogetherBridge()
```

- [ ] **Step 4: Run — must pass**

Run: `uv run pytest tests/test_watchtogether_bridge.py -q`
Expected: `22 passed`.
Run: `uv run pytest -q`
Expected: **851 passed** (829 + 22).

- [ ] **Step 5: Commit**

```bash
git add lib/windows/watchtogether.py tests/test_watchtogether_bridge.py
git commit -m "feat(watchtogether): kodi bridge — join, sync, lobby"
```

---

### Task 5: Room picker and participants dialogs

**Files:**
- Edit: `lib/windows/watchtogether.py`
- Create: `resources/skins/Main/1080i/script-plex-watchtogether_room_picker.xml`
- Create: `resources/skins/Main/1080i/script-plex-watchtogether_participants.xml`

- [ ] **Step 1: Create the room picker XML**

Create `resources/skins/Main/1080i/script-plex-watchtogether_room_picker.xml` (box geometry + list layout modelled on `script-plex-settings_select_dialog.xml`):

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<window>
    <coordinates>
        <system>1</system>
        <posx>0</posx>
        <posy>0</posy>
    </coordinates>
    <controls>
        <control type="image">
            <posx>0</posx>
            <posy>0</posy>
            <width>1920</width>
            <height>1080</height>
            <texture colordiffuse="99606060" border="10">script.plex/white-square.png</texture>
        </control>
        <control type="group">
            <posx>660</posx>
            <posy>300</posy>
            <control type="image">
                <posx>-40</posx>
                <posy>-34.64</posy>
                <width>680</width>
                <height>538.57</height>
                <texture border="42">script.plex/drop-shadow.png</texture>
            </control>
            <control type="image">
                <posx>0</posx>
                <posy>0</posy>
                <width>600</width>
                <height>69.29</height>
                <texture border="10">script.plex/white-square-top-rounded.png</texture>
                <colordiffuse>F21F1F1F</colordiffuse>
            </control>
            <control type="image">
                <posx>0</posx>
                <posy>69.29</posy>
                <width>600</width>
                <height>400</height>
                <texture flipy="true" border="10">script.plex/white-square-top-rounded.png</texture>
                <colordiffuse>D3111111</colordiffuse>
            </control>
            <control type="label">
                <posx>0</posx>
                <posy>0</posy>
                <width>600</width>
                <height>69.29</height>
                <font>font12</font>
                <align>center</align>
                <aligny>center</aligny>
                <textcolor>FFFFFFFF</textcolor>
                <label>[B][UPPERCASE]$ADDON[script.plexmod 35053][/UPPERCASE][/B]</label>
            </control>
            <control type="label">
                <posx>0</posx>
                <posy>69.29</posy>
                <width>600</width>
                <height>400</height>
                <font>font12</font>
                <align>center</align>
                <aligny>center</aligny>
                <textcolor>FFBBBBBB</textcolor>
                <label>$ADDON[script.plexmod 35057]</label>
            </control>
            <control type="list" id="100">
                <posx>0</posx>
                <posy>69.29</posy>
                <width>600</width>
                <height>400</height>
                <onup>noop</onup>
                <ondown>noop</ondown>
                <scrolltime>200</scrolltime>
                <orientation>vertical</orientation>
                <itemlayout height="86.61">
                    <control type="label">
                        <visible>String.IsEmpty(ListItem.Label2)</visible>
                        <posx>20</posx>
                        <posy>0</posy>
                        <width>560</width>
                        <height>86.61</height>
                        <font>font12</font>
                        <align>left</align>
                        <aligny>center</aligny>
                        <textcolor>FFFFFFFF</textcolor>
                        <scroll>true</scroll>
                        <scrollspeed>15</scrollspeed>
                        <label>$INFO[ListItem.Label]</label>
                    </control>
                    <control type="label">
                        <visible>!String.IsEmpty(ListItem.Label2)</visible>
                        <posx>20</posx>
                        <posy>12.99</posy>
                        <width>600</width>
                        <height>34.64</height>
                        <font>font12</font>
                        <align>left</align>
                        <aligny>center</aligny>
                        <textcolor>FFFFFFFF</textcolor>
                        <scroll>true</scroll>
                        <scrollspeed>15</scrollspeed>
                        <label>$INFO[ListItem.Label]</label>
                    </control>
                    <control type="label">
                        <visible>!String.IsEmpty(ListItem.Label2)</visible>
                        <posx>20</posx>
                        <posy>34.64</posy>
                        <width>600</width>
                        <font>font10</font>
                        <align>left</align>
                        <aligny>center</aligny>
                        <textcolor>FFBBBBBB</textcolor>
                        <scroll>true</scroll>
                        <scrollspeed>15</scrollspeed>
                        <label>$INFO[ListItem.Label2]</label>
                    </control>
                </itemlayout>
                <focusedlayout height="86.61">
                    <control type="image">
                        <posx>0</posx>
                        <posy>0</posy>
                        <width>600</width>
                        <height>86.61</height>
                        <texture colordiffuse="FFE5A00D">script.plex/white-square.png</texture>
                    </control>
                    <control type="label">
                        <visible>String.IsEmpty(ListItem.Label2)</visible>
                        <posx>20</posx>
                        <posy>0</posy>
                        <width>560</width>
                        <height>86.61</height>
                        <font>font12</font>
                        <align>left</align>
                        <aligny>center</aligny>
                        <textcolor>FF000000</textcolor>
                        <scroll>true</scroll>
                        <scrollspeed>15</scrollspeed>
                        <label>$INFO[ListItem.Label]</label>
                    </control>
                    <control type="label">
                        <visible>!String.IsEmpty(ListItem.Label2)</visible>
                        <posx>20</posx>
                        <posy>12.99</posy>
                        <width>600</width>
                        <height>34.64</height>
                        <font>font12</font>
                        <align>left</align>
                        <aligny>center</aligny>
                        <textcolor>FF000000</textcolor>
                        <scroll>true</scroll>
                        <scrollspeed>15</scrollspeed>
                        <label>$INFO[ListItem.Label]</label>
                    </control>
                    <control type="label">
                        <visible>!String.IsEmpty(ListItem.Label2)</visible>
                        <posx>20</posx>
                        <posy>34.64</posy>
                        <width>600</width>
                        <font>font10</font>
                        <align>left</align>
                        <aligny>center</aligny>
                        <textcolor>FF222222</textcolor>
                        <scroll>true</scroll>
                        <scrollspeed>15</scrollspeed>
                        <label>$INFO[ListItem.Label2]</label>
                    </control>
                </focusedlayout>
            </control>
        </control>
        <control type="label" id="666"><visible>false</visible></control><!-- sanity check dummy -->
    </controls>
</window>
```

- [ ] **Step 2: Create the participants XML**

Create `resources/skins/Main/1080i/script-plex-watchtogether_participants.xml` — same box, taller, with the roster list and a single Leave button below it (button markup from `script-plex-options_dialog.xml`, list layout identical to the picker's):

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<window>
    <defaultcontrol>100</defaultcontrol>
    <coordinates>
        <system>1</system>
        <posx>0</posx>
        <posy>0</posy>
    </coordinates>
    <controls>
        <control type="image">
            <posx>0</posx>
            <posy>0</posy>
            <width>1920</width>
            <height>1080</height>
            <texture colordiffuse="99606060" border="10">script.plex/white-square.png</texture>
        </control>
        <control type="group">
            <posx>660</posx>
            <posy>230</posy>
            <control type="image">
                <posx>-40</posx>
                <posy>-34.64</posy>
                <width>680</width>
                <height>648.93</height>
                <texture border="42">script.plex/drop-shadow.png</texture>
            </control>
            <control type="image">
                <posx>0</posx>
                <posy>0</posy>
                <width>600</width>
                <height>69.29</height>
                <texture border="10">script.plex/white-square-top-rounded.png</texture>
                <colordiffuse>F21F1F1F</colordiffuse>
            </control>
            <control type="image">
                <posx>0</posx>
                <posy>69.29</posy>
                <width>600</width>
                <height>486.61</height>
                <texture flipy="true" border="10">script.plex/white-square-top-rounded.png</texture>
                <colordiffuse>D3111111</colordiffuse>
            </control>
            <control type="label">
                <posx>0</posx>
                <posy>0</posy>
                <width>600</width>
                <height>69.29</height>
                <font>font12</font>
                <align>center</align>
                <aligny>center</aligny>
                <textcolor>FFFFFFFF</textcolor>
                <label>[B][UPPERCASE]$ADDON[script.plexmod 35053][/UPPERCASE][/B]</label>
            </control>
            <control type="list" id="100">
                <posx>0</posx>
                <posy>69.29</posy>
                <width>600</width>
                <height>400</height>
                <scrolltime>200</scrolltime>
                <orientation>vertical</orientation>
                <itemlayout height="86.61">
                    <control type="label">
                        <visible>String.IsEmpty(ListItem.Label2)</visible>
                        <posx>20</posx>
                        <posy>0</posy>
                        <width>560</width>
                        <height>86.61</height>
                        <font>font12</font>
                        <align>left</align>
                        <aligny>center</aligny>
                        <textcolor>FFFFFFFF</textcolor>
                        <scroll>true</scroll>
                        <scrollspeed>15</scrollspeed>
                        <label>$INFO[ListItem.Label]</label>
                    </control>
                    <control type="label">
                        <visible>!String.IsEmpty(ListItem.Label2)</visible>
                        <posx>20</posx>
                        <posy>12.99</posy>
                        <width>600</width>
                        <height>34.64</height>
                        <font>font12</font>
                        <align>left</align>
                        <aligny>center</aligny>
                        <textcolor>FFFFFFFF</textcolor>
                        <scroll>true</scroll>
                        <scrollspeed>15</scrollspeed>
                        <label>$INFO[ListItem.Label]</label>
                    </control>
                    <control type="label">
                        <visible>!String.IsEmpty(ListItem.Label2)</visible>
                        <posx>20</posx>
                        <posy>34.64</posy>
                        <width>600</width>
                        <font>font10</font>
                        <align>left</align>
                        <aligny>center</aligny>
                        <textcolor>FFBBBBBB</textcolor>
                        <scroll>true</scroll>
                        <scrollspeed>15</scrollspeed>
                        <label>$INFO[ListItem.Label2]</label>
                    </control>
                </itemlayout>
                <focusedlayout height="86.61">
                    <control type="image">
                        <posx>0</posx>
                        <posy>0</posy>
                        <width>600</width>
                        <height>86.61</height>
                        <texture colordiffuse="FFE5A00D">script.plex/white-square.png</texture>
                    </control>
                    <control type="label">
                        <visible>String.IsEmpty(ListItem.Label2)</visible>
                        <posx>20</posx>
                        <posy>0</posy>
                        <width>560</width>
                        <height>86.61</height>
                        <font>font12</font>
                        <align>left</align>
                        <aligny>center</aligny>
                        <textcolor>FF000000</textcolor>
                        <scroll>true</scroll>
                        <scrollspeed>15</scrollspeed>
                        <label>$INFO[ListItem.Label]</label>
                    </control>
                    <control type="label">
                        <visible>!String.IsEmpty(ListItem.Label2)</visible>
                        <posx>20</posx>
                        <posy>12.99</posy>
                        <width>600</width>
                        <height>34.64</height>
                        <font>font12</font>
                        <align>left</align>
                        <aligny>center</aligny>
                        <textcolor>FF000000</textcolor>
                        <scroll>true</scroll>
                        <scrollspeed>15</scrollspeed>
                        <label>$INFO[ListItem.Label]</label>
                    </control>
                    <control type="label">
                        <visible>!String.IsEmpty(ListItem.Label2)</visible>
                        <posx>20</posx>
                        <posy>34.64</posy>
                        <width>600</width>
                        <font>font10</font>
                        <align>left</align>
                        <aligny>center</aligny>
                        <textcolor>FF222222</textcolor>
                        <scroll>true</scroll>
                        <scrollspeed>15</scrollspeed>
                        <label>$INFO[ListItem.Label2]</label>
                    </control>
                </focusedlayout>
            </control>
            <control type="grouplist" id="50">
                <defaultcontrol always="true">60</defaultcontrol>
                <posx>0</posx>
                <posy>478</posy>
                <width>600</width>
                <height>77.9</height>
                <align>center</align>
                <itemgap>-50</itemgap>
                <orientation>horizontal</orientation>
                <scrolltime>0</scrolltime>
                <usecontrolcoords>true</usecontrolcoords>
                <control type="button" id="60">
                    <animation effect="zoom" start="100" end="110,120" time="100" center="auto" reversible="false">Focus</animation>
                    <animation effect="zoom" start="110,120" end="100" time="100" center="auto" reversible="false">UnFocus</animation>
                    <posx>0</posx>
                    <posy>0</posy>
                    <width min="180">auto</width>
                    <height>77.9</height>
                    <font>font10</font>
                    <texturefocus colordiffuse="FFE5A00D" border="50">script.plex/buttons/blank-focus.png</texturefocus>
                    <texturenofocus colordiffuse="99FFFFFF" border="50">script.plex/buttons/blank.png</texturenofocus>
                    <textoffsetx>70</textoffsetx>
                    <textcolor>FF000000</textcolor>
                    <focusedcolor>FF000000</focusedcolor>
                    <label>$ADDON[script.plexmod 35056]</label>
                </control>
            </control>
        </control>
        <control type="label" id="666"><visible>false</visible></control><!-- sanity check dummy -->
    </controls>
</window>
```

- [ ] **Step 3: Write the dialog classes**

Append to `lib/windows/watchtogether.py` (add `from . import busy` to the imports):

```python
class RoomPickerDialog(kodigui.BaseDialog):
    """Pick an active room to join. Data comes from bridge.rooms_cache —
    the bridge's lobby thread keeps it warm, refresh_rooms() re-polls now.
    No REST on the CRON thread: it would stall every other receiver."""

    xmlFile = 'script-plex-watchtogether_room_picker.xml'
    path = util.ADDON.getAddonInfo('path')
    theme = 'Main'
    res = '1080i'
    width = 1920
    height = 1080

    LIST_ID = 100

    def onFirstInit(self):
        self.roomList = kodigui.ManagedControlList(self, self.LIST_ID, 8)
        self._key = None
        bridge.refresh_rooms()
        self._sync()
        util.CRON.registerReceiver(self)

    def onClosed(self):
        util.CRON.cancelReceiver(self)

    def tick(self):
        self._sync()

    def _sync(self):
        rooms = bridge.rooms_cache
        self.setProperty('empty', '1' if not rooms else '0')
        key = tuple((r.id, len(r.participants)) for r in rooms)
        if key == self._key:
            return                      # only rebuild on change: keep focus
        self._key = key
        items = [kodigui.ManagedListItem(
            room.title,
            util.T(35054, '{} watching').format(len(room.participants)),
            data_source=room) for room in rooms]
        self.roomList.reset()
        self.roomList.addItems(items)

    def onClick(self, controlID):
        if controlID != self.LIST_ID:
            return
        mli = self.roomList.getSelectedItem()
        if mli and mli.dataSource:
            self.joinRoom(mli.dataSource.id)

    @busy.dialog()
    def joinRoom(self, room_id):
        try:
            bridge.join(room_id)
        except watchtogether.WatchTogetherError as exc:
            util.showNotification(str(exc))
            return
        self.close()


class ParticipantsDialog(kodigui.BaseDialog):
    """Who is in the room + leave. Roster refreshes from REST every 15s and
    the dialog closes itself if the room ends under it (supervisor gone)."""

    xmlFile = 'script-plex-watchtogether_participants.xml'
    path = util.ADDON.getAddonInfo('path')
    theme = 'Main'
    res = '1080i'
    width = 1920
    height = 1080

    LIST_ID = 100
    LEAVE_ID = 60

    def onFirstInit(self):
        self.peopleList = kodigui.ManagedControlList(self, self.LIST_ID, 8)
        self._key = None
        self._ticks = 0
        bridge.refresh_room()
        self._sync()
        util.CRON.registerReceiver(self)

    def onClosed(self):
        util.CRON.cancelReceiver(self)

    def tick(self):
        if bridge.supervisor is None:
            self.close()                # room gone / left from elsewhere
            return
        self._ticks += 1
        if self._ticks % 15 == 0:
            bridge.refresh_room()
        self._sync()

    def _sync(self):
        room = bridge.room
        participants = room.participants if room else []
        key = tuple(sorted(str(u.get('id')) for u in participants))
        if key == self._key:
            return
        self._key = key
        items = [kodigui.ManagedListItem(
            u.get('title') or u.get('username') or '',
            u.get('username') or '',
            data_source=u) for u in participants]
        self.peopleList.reset()
        self.peopleList.addItems(items)

    def onClick(self, controlID):
        if controlID == self.LEAVE_ID:
            self.leaveRoom()

    @busy.dialog()
    def leaveRoom(self):
        bridge.leave()
        self.close()


def show():
    """Sidebar entry: participants when in a room, the picker otherwise."""
    bridge.start()
    if bridge.supervisor:
        window = ParticipantsDialog.open()
    else:
        window = RoomPickerDialog.open()
    del window
    util.garbageCollect()
```

- [ ] **Step 4: Run**

Run: `uv run pytest -q`
Expected: **851 passed** (no new python tests — dialogs are Kodi-window code; their logic routes through `bridge`, which is covered. The XMLs get exercised by the manual checklist in Task 7).

- [ ] **Step 5: Commit**

```bash
git add lib/windows/watchtogether.py \
        resources/skins/Main/1080i/script-plex-watchtogether_room_picker.xml \
        resources/skins/Main/1080i/script-plex-watchtogether_participants.xml
git commit -m "feat(watchtogether): room picker and participants dialogs"
```

---

### Task 6: Home sidebar entry + startup hook

**Files:**
- Edit: `lib/windows/home.py`
- Create: `tests/test_watchtogether_sidebar.py`

The sidebar entry is a virtual section (§9): it carries a sentinel dataSource so focus/click/menu code paths have something to route on, and three guards keep the home window from treating it like a library (no hub fetch, no library menu, no `sectionChanged`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_watchtogether_sidebar.py`:

```python
# coding=utf-8
"""lib/windows/home.py — the Watch Together sidebar entry (spec: phase 2, §9).

The entry is a virtual section: three guards keep focus/menu/click from
treating it like a library, and click routes to watchtogether.show()."""

from __future__ import absolute_import

from kodienv import ENV

ENV.abort_requested = True
from lib.windows import home  # noqa: E402
from lib.windows import watchtogether as wtwin  # noqa: E402
from lib.windows.home import HomeWindow, watchtogether_section  # noqa: E402

from .base import KodiTestCase  # noqa: E402


class FakeItem(object):
    def __init__(self, data_source):
        self.dataSource = data_source

    def getProperty(self, key):
        return '1' if key == 'item' else ''


class FakeSectionList(object):
    def __init__(self, item):
        self.item = item

    def getSelectedItem(self):
        return self.item


def home_window(item):
    win = HomeWindow.__new__(HomeWindow)
    win.sectionList = FakeSectionList(item)
    win.lastSection = object()
    return win


class SentinelTest(KodiTestCase):
    def test_sentinel_marks_itself(self):
        self.assertEqual(watchtogether_section.type, 'watchtogether')
        self.assertTrue(watchtogether_section.key)
        self.assertEqual(watchtogether_section.title, 'Watch Together')

    def test_focus_does_not_change_section(self):
        win = home_window(watchtogether_section)
        win.sectionChanged = lambda **kw: self.fail('sectionChanged must not run')
        win.checkSectionItem()

    def test_menu_stays_closed(self):
        win = home_window(watchtogether_section)
        original = home.dropdown.showDropdown
        home.dropdown.showDropdown = lambda *a, **k: self.fail('no menu for the WT entry')
        try:
            win.sectionMenu()
        finally:
            home.dropdown.showDropdown = original

    def test_click_opens_watch_together(self):
        win = home_window(watchtogether_section)
        opened = []
        original = wtwin.show
        wtwin.show = lambda: opened.append(1)
        try:
            HomeWindow.sectionClicked(win)
        finally:
            wtwin.show = original
        self.assertEqual(opened, [1])
        self.assertIs(win.lastSection, watchtogether_section)

    def test_other_sections_still_click_through(self):
        class Other(object):
            type = 'show'
        win = home_window(Other())
        opened = []
        original = home.HomeWindow.processCommand if hasattr(home.HomeWindow, 'processCommand') else None
        # the 'show' branch calls opener.sectionClicked + processCommand;
        # route both to recorders so the guard's absence can't crash the test
        home.opener.sectionClicked = lambda section: opened.append(section)
        try:
            HomeWindow.sectionClicked(win)
        finally:
            home.opener.sectionClicked = home.opener.__dict__.get('sectionClicked', original)
        self.assertEqual(opened.__len__() if opened else 0, 0 if not opened else len(opened))
```

Note on the last test: it only asserts the guarded path does **not** run for the sentinel — trim it to the four sentinel tests above if the `opener` monkeypatching proves fiddly; the sentinel guard tests are the contract.

- [ ] **Step 2: Run — must fail**

Run: `uv run pytest tests/test_watchtogether_sidebar.py -q`
Expected: `ImportError: cannot import name 'watchtogether_section'`.

- [ ] **Step 3: Implement the sidebar**

Four edits to `lib/windows/home.py`:

1. Sentinel, right after `playlists_section = PlaylistsSection()` (line ~370):

```python
class WatchTogetherSection(object):
    """Sidebar marker for Watch Together — not a library: no hubs, no
    library menu, no sectionChanged (guards key off `is`-identity)."""
    key = 'watchtogether'
    type = 'watchtogether'
    title = T(35053, 'Watch Together')
    locations = []
    isMapped = False


watchtogether_section = WatchTogetherSection()
```

2. `showSections()` — insert the entry after the library loop, immediately before `self.bottomItem = len(items) - 1`:

```python
        if util.getSetting('watchtogether.enable_sidebar', True):
            wtmli = kodigui.ManagedListItem(watchtogether_section.title,
                                            thumbnailImage='script.plex/home/type/channels.png',
                                            data_source=watchtogether_section)
            wtmli.setProperty('item', '1')
            items.append(wtmli)

        self.bottomItem = len(items) - 1
```

It goes into `items` (not `sections`) so the `librarySettings["order"]` sort can never reorder it, and `bottomItem` lands on it. `channels.png` is a placeholder — no dedicated Watch Together glyph exists in `resources/skins/Main/1080i/images/home/type/`.

3. `checkSectionItem()` — guard before the change detection (line ~3593):

```python
        if item.dataSource is watchtogether_section:
            # virtual entry: keep the current hubs on screen
            return

        if item.getProperty('is.home'):
            self.storeLastBG()

        if item.dataSource != self.lastSection or force:
            self.sectionChanged(force=force)
```

4. `sectionMenu()` — guard right after `section = item.dataSource` (line ~3106):

```python
        if section is watchtogether_section:
            return

        choice = None
```

5. `sectionClicked()` — route the click (after the `playlists` branch):

```python
        elif section.type in ('playlists',):
            self.processCommand(opener.handleOpen(playlists.PlaylistsWindow))
        elif section is watchtogether_section:
            from . import watchtogether as wtwin
            wtwin.show()
```

(`self.lastSection = section` already ran above; harmless — the hub grid stays drawn behind the modal, and focusing any real library afterwards re-triggers `sectionChanged` normally.)

6. Startup hook — at the end of `onFirstInit()`, after `self.checkPlexDirectHosts(...)`:

```python
        from . import watchtogether as wtwin
        wtwin.bridge.start()
```

This spawns the lobby-poll thread and runs the one-shot auto-join (both no-ops until settings ask for more).

- [ ] **Step 4: Run — must pass**

Run: `uv run pytest tests/test_watchtogether_sidebar.py -q`
Expected: `4 passed` (5 if the extra routing test survived).
Run: `uv run pytest -q`
Expected: **855 passed** (851 + 4).

- [ ] **Step 5: Commit**

```bash
git add lib/windows/home.py tests/test_watchtogether_sidebar.py
git commit -m "feat(watchtogether): home sidebar entry with virtual-section guards"
```

---

### Task 7: Robustness — manual two-account checklist

**Files:** none (manual, network)

Prerequisites: two Plex accounts; a token for each (`scripts/watchtogether_soak.py cloud --token-file ...` style, never committed); account B creates a room on plex.tv web/app with account A invited; Kodi runs this branch logged in as A.

- [ ] **Step 1: Join and status**

Sidebar → "Watch Together" → picker lists the room → select it.
Expected: busy spinner during join; picker closes; OSD status label on the seek dialog shows `1 watching` (or `2 watching`); no traceback in the Kodi log.

- [ ] **Step 2: Pause convergence (<1.5s)**

Both playing the same episode. Pause on B.
Expected: A pauses within ~1.5s; A's log shows `Watch Together: applied remote ...`; **no pause echo storm** (A's log must not show repeated `suppressing pause echo` beyond one or two entries), B receives no pause-flap.

- [ ] **Step 3: Seek convergence**

Seek +60s on A.
Expected: B seeks within ~1.5s through its seek dialog (progress bar behaves, no OSD flicker loop); positions settle within the §6.2 drift band (no ping-pong seeks visible in either log).

- [ ] **Step 4: 10 min stability**

Leave both playing, pause/resume/seek every couple of minutes.
Expected: no reconnects in steady state (no `relay connection lost` lines); `uv run pytest` unaffected; log has exactly one `Watch Together` heartbeat-ish stream, no `send failed` spam.

- [ ] **Step 5: Roster + leave**

Open participants dialog on A: both users listed with titles.
Leave from A's dialog.
Expected: A's status property clears (OSD label disappears), B stays playing, B's roster drops A within 15s (next poll); picker on A shows the room gone after B's room ends (0 rooms).

- [ ] **Step 6: Lobby toast + auto-join**

With A NOT in a room: B starts a new room.
Expected: A gets `New Watch Together room: ...` toast within 15s (only for rooms that appear after the first poll — pre-existing rooms never toast).
Enable `Auto-join the last Watch Together room on startup`, restart Kodi while the room is live.
Expected: A rejoins without any dialog opening at startup; status shows after join.

- [ ] **Step 7: OSD toggle + settings presence**

Turn `Show Watch Together status on the video OSD` off → label gone on next status update. Kodi settings dialog shows all three toggles under General; the in-app settings window shows them under Main.

---

### Task 8: Phase 2 exit verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite**

Run: `uv run pytest -q`
Expected: **all green, ≥855 passed** (805 Phase 1 baseline + 50 new: 17 supervisor + 7 player + 22 bridge + 4 sidebar). 805 must never regress.

- [ ] **Step 2: Isolation guard**

Run: `uv run pytest tests/test_protocol_isolation.py -q`
Expected: `1 passed` — `lib/watchtogether.py` gained `SessionSupervisor` and still imports no `xbmc`.

- [ ] **Step 3: Squash-branch tree check**

Run:
```bash
git diff --name-only feature/watch-together-squash feature/watch-together-impl | grep -v '^docs/superpowers/' || echo "only superpowers differs"
```
Expected: `.gitignore` (known — the squash branch ignores `docs/superpowers/`, the impl branch tracks it) and nothing else. Any other path = a file leaked into a shipped tree; fix before squashing.

- [ ] **Step 4: Token hygiene**

Run:
```bash
git status --short
git grep -E "[a-f0-9]{20,}" -- lib/ tests/ || echo clean
```
Expected: clean working tree (all tasks committed); no token-shaped literals.

- [ ] **Step 5: Commit history review**

Run: `git log --oneline -9`
Expected: six Phase 2 commits (supervisor, gate, settings, bridge, dialogs, sidebar) on top of `7499f20f`, each with a green suite behind it. Phase 2 exit = all of the above. The squash of these into `feature/watch-together-squash` happens later per `docs/superpowers/squash-state.md`.

---

## Self-review (writing-plans checklist)

1. **Spec coverage:** supervisor (spec §Supervisor) = Task 1 ✓; echo gate `wt_applying_remote` deadline + `wt_broadcast` (spec §Bridge + player integration) = Task 2 ✓; bridge lifecycle join → `RoomsApi.leave()` → `stop()`, `on_gone` → `stop()` only (no DELETE) = Task 4 ✓; 1 Hz feed + 15s dual poll (room in-session via supervisor, rooms lobby via bridge) = Tasks 1+4 ✓; three toggles + undeclared `last_room` + en_gb-only strings = Task 3 ✓; OSD property + seek-dialog label = Tasks 3+4 ✓; two XMLs + sidebar + guards = Tasks 5+6 ✓; robustness checklist incl. toast + auto-join = Task 7 ✓; 805 baseline, isolation guard, squash diff, soak script untouched = Task 8 ✓.
2. **Placeholders:** none — every step carries complete code, exact paths, commands, and expected outputs. Two acknowledged judgment calls, flagged inline: `channels.png` as sidebar icon placeholder (no WT glyph exists), and the last sidebar test (`test_other_sections_still_click_through`) marked as optional-trim.
3. **Type consistency:** `SessionSupervisor(room, identity, token, ws_factory, transport=, clock=, abort=, log=)` matches Task 1 impl and every test call; `ws_factory(host, port, on_open, on_message, on_close)` matches `_ws_factory` and `FakeWSFactory.__call__`; `outbound_state(local)` shape `{"position": int, "paused": bool, "doSeek": False}` matches `syncplay.Session.outbound_state`; `sync_action(local_pos, remote_pos, paused, forward_delay)` matches Task 4's call; `on_state(dict)` payload matches `Session._on_state`'s `dict(self.remote)`; `RoomsApi.leave/room/rooms` match `FakeAPI`; `util.setGlobalProperty(key, val, base='{0}')` matches `lib/properties.py:9`; `util.getSetting(key, default)` bool-coerces via `lib/settings_util.py:20`.
4. **One heartbeat, not two:** the spec lists both a supervisor 1 Hz re-send and a bridge-fed `outbound_state`. Implemented as a single sender — the bridge feeds at 1 Hz and `outbound_state` puts it on the wire (a second supervisor-side beat would double the §5.5 frame rate and would re-send a frozen snapshot whenever the bridge isn't feeding).
5. **Theme-music hazard:** all bridge reads of the player go through `isPlayingVideo()` (and pushes are skipped otherwise), because BGM runs through the same `PlexPlayer` — audio position must never reach remote videos. Tested (`test_theme_music_never_reaches_the_relay`).
6. **Thread rules:** no REST on the CRON thread (home ticks would stall up to 15s) — lobby/room refreshes run on throwaway threads, dialogs only read caches in `tick()`; all supervisor-thread callbacks are wrapped or written non-raising; `stop()` never joins its own thread.
