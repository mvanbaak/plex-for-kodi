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

    def test_explicit_null_ends_at_does_not_crash(self):
        room = watchtogether.Room(dict(ROOM_JSON, endsAt=None))
        self.assertFalse(room.ended(now=1))


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
        self.assertEqual(ctx.exception.args, ("GET /rooms -> 401",))

    def test_3xx_is_an_error_not_a_success(self):
        api, _ = self.api((302, {"Location": "http://evil"}))
        with self.assertRaises(watchtogether.WatchTogetherError):
            api.room("x")

    def test_network_failure_maps_to_watchtogether_error(self):
        def boom(method, path, body=None, token=None):
            raise watchtogether.requests.ConnectionError("boom")

        api = watchtogether.RoomsApi(token="tok", transport=boom)
        with self.assertRaises(watchtogether.WatchTogetherError) as ctx:
            api.rooms()
        self.assertIn("ConnectionError", str(ctx.exception))
        self.assertNotIn("boom", str(ctx.exception))

    def test_room_empty_body_is_error(self):
        api, _ = self.api((200, None))
        with self.assertRaises(watchtogether.WatchTogetherError):
            api.room("x")

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


class FakeTimer(object):
    def __init__(self, interval, callback, repeat=True, name=None):
        self.interval = interval
        self.callback = callback
        self.repeat = repeat
        self.name = name
        self.canceled = False
        self.joined = False

    def cancel(self):
        self.canceled = True

    def join(self, timeout=None):
        self.joined = True


class FakeTimerFactory(object):
    def __init__(self):
        self.calls = []

    def __call__(self, interval, callback, repeat=True):
        t = FakeTimer(interval, callback, repeat=repeat)
        self.calls.append((interval, callback, repeat))
        return t

class FakeWS(object):
    def __init__(self, state=None):
        self.sent = []
        self.state = state or {"open": False, "ever": False, "closed": False}
        self._on_open = None
        self._on_message = None
        self._on_close = None
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def send(self, obj):
        self.sent.append(obj)
        if self.state.get("echo_on_send"):
            pass

    def close(self):
        self.closed = True
        if self._on_close:
            try:
                self._on_close("close")
            except Exception:
                pass

    def trigger_open(self):
        if self._on_open:
            self._on_open()

    def trigger_close(self):
        if self._on_close:
            self._on_close("close")


class FakeWSFactory(object):
    def __init__(self, ws=None):
        self.ws = ws or FakeWS()
        self.calls = []

    def __call__(self, host, port, on_open, on_message, on_close):
        self.ws._on_open = on_open
        self.ws._on_message = on_message
        self.ws._on_close = on_close
        self.calls.append((host, port))
        return self.ws


class FakeClock(object):
    def __init__(self, t=0.0):
        self.t = t
        self.m = t

    def time(self):
        return self.t

    def monotonic(self):
        return self.m

    def advance(self, s):
        self.t += s
        self.m += s


class TestSessionSupervisor(unittest.TestCase):
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

    def _room(self):
        return watchtogether.Room(self.ROOM_JSON)

    def _identity(self):
        return {"device": "d", "name": "n", "user": 1000002}

    def test_start_then_stop(self):
        room = self._room()
        identity = self._identity()
        tf = FakeTimerFactory()
        ws = FakeWS()
        wsf = FakeWSFactory(ws)
        api, _ = self.api((200, self.ROOM_JSON))
        sup = watchtogether.SessionSupervisor(room, identity, token="tok", ws_factory=wsf, transport=api._transport, timer_factory=tf)
        sup.start()
        sup.stop(timeout=0.01)
        self.assertTrue(sup._stop.is_set())

    def api(self, *responses):
        transport = FakeTransport(list(responses))
        return watchtogether.RoomsApi(token="tok", transport=transport), transport

    def test_connect_open_sends_hello_list_setready(self):
        room = self._room()
        identity = self._identity()
        tf = FakeTimerFactory()
        ws = FakeWS()
        wsf = FakeWSFactory(ws)
        api, _ = self.api((200, self.ROOM_JSON))
        sup = watchtogether.SessionSupervisor(room, identity, token="tok", ws_factory=wsf, transport=api._transport, timer_factory=tf)
        sup.start()
        ws.trigger_open()
        ws.trigger_close()
        sup.stop(timeout=0.01)
        self.assertTrue(ws.started)
        self.assertTrue(len(ws.sent) >= 3)

    def test_on_message_forwards_to_session_on_state(self):
        room = self._room()
        identity = self._identity()
        tf = FakeTimerFactory()
        ws = FakeWS()
        wsf = FakeWSFactory(ws)
        api, _ = self.api((200, self.ROOM_JSON))
        states = []
        sup = watchtogether.SessionSupervisor(room, identity, token="tok", ws_factory=wsf, transport=api._transport, timer_factory=tf)
        sup.on_state = lambda d: states.append(d)
        sup.start()
        ws.trigger_open()
        ws._on_message('{"State":{"playstate":{"position":5,"paused":true,"doSeek":false,"setBy":"x"}}}')
        ws.trigger_close()
        sup.stop(timeout=0.01)
        self.assertTrue(states)

    def test_poll_room_refreshes_roster(self):
        room = self._room()
        identity = self._identity()
        tf = FakeTimerFactory()
        ws = FakeWS()
        wsf = FakeWSFactory(ws)
        api, _ = self.api((200, self.ROOM_JSON), (200, dict(self.ROOM_JSON, users=self.ROOM_JSON['users'])))
        roster = []
        sup = watchtogether.SessionSupervisor(room, identity, token="tok", ws_factory=wsf, transport=api._transport, timer_factory=tf)
        sup.on_roster = lambda d: roster.append(d)
        sup.start()
        ws.trigger_open()
        ws.trigger_close()
        sup.stop(timeout=0.01)

    def test_poll_room_gone_calls_on_gone_and_stops(self):
        room = self._room()
        identity = self._identity()
        tf = FakeTimerFactory()
        ws = FakeWS()
        wsf = FakeWSFactory(ws)
        api, _ = self.api((200, self.ROOM_JSON), (404, "gone"))
        gone = []
        clock = FakeClock(0.0)
        sup = watchtogether.SessionSupervisor(room, identity, token="tok", ws_factory=wsf, transport=api._transport, timer_factory=tf, clock=clock)
        sup.on_gone = lambda: gone.append(True)
        sup.start()
        ws.trigger_open()
        ws.trigger_close()
        clock.advance(10.0)  # let backoff pass
        sup.stop(timeout=0.01)
        self.assertTrue(gone)

    def test_outbound_state_before_connect_only_stores(self):
        room = self._room()
        identity = self._identity()
        tf = FakeTimerFactory()
        ws = FakeWS()
        wsf = FakeWSFactory(ws)
        api, _ = self.api((200, self.ROOM_JSON))
        sup = watchtogether.SessionSupervisor(room, identity, token="tok", ws_factory=wsf, transport=api._transport, timer_factory=tf)
        sup.start()
        res = sup.outbound_state({"position": 1, "paused": True})
        self.assertFalse(res)
        sup.stop(timeout=0.01)

    def test_outbound_state_sends_while_connected(self):
        room = self._room()
        identity = self._identity()
        tf = FakeTimerFactory()
        ws = FakeWS()
        wsf = FakeWSFactory(ws)
        api, _ = self.api((200, self.ROOM_JSON))
        sup = watchtogether.SessionSupervisor(room, identity, token="tok", ws_factory=wsf, transport=api._transport, timer_factory=tf)
        sup.start()
        ws.trigger_open()
        res = sup.outbound_state({"position": 1, "paused": True})
        self.assertTrue(res)
        ws.trigger_close()
        sup.stop(timeout=0.01)
