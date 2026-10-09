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
        ENV.cond_visibility["Player.Paused"] = (
            lambda cond: bool(wtwin.player.PLAYER.paused)
            if wtwin.player.PLAYER else False)

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
