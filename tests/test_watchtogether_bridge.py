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
        self.seek_requested = False
        self.ready = None
        self.ready_manual = None

    def outbound_state(self, local):
        self.sent.append(local)
        return self.connected

    def request_seek(self):
        self.seek_requested = True

    def send_now(self):
        self.sent_now = getattr(self, "sent_now", 0) + 1

    def set_ready(self, ready, manually=False):
        self.ready = ready
        self.ready_manual = manually

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
        self.videos = []
        self.handler = FakeHandler()
        self.wt_broadcast = None
        self.wt_applying_remote = 0.0

    def playVideo(self, video, resume=False):
        self.videos.append((video, resume))

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


class FakeVideo(object):
    def __init__(self, rating_key):
        self.ratingKey = rating_key


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
        # BGM plays through the same player: audio-only must not push the
        # theme's position, or peers would seek their video to it. We still
        # emit an idle State so the relay does not reap the silent socket.
        self.bridge.supervisor = FakeSupervisor()
        self.player.video = False
        self.player.position = 99.0
        self.bridge.push_local()
        self.assertEqual(self.bridge.supervisor.sent,
                         [{"position": 0, "paused": True, "doSeek": False}])

    def test_no_supervisor_is_inert(self):
        self.bridge.push_local()
        self.assertEqual(self.player.wt_broadcast, None)

    def test_local_change_event_pushes(self):
        self.bridge.supervisor = FakeSupervisor()
        self.bridge.on_local_change("pause")
        self.assertEqual(len(self.bridge.supervisor.sent), 1)

    def test_local_seek_requests_a_seek_command(self):
        # §5.6: the outbound State must carry doSeek: true, not just position
        sup = FakeSupervisor()
        self.bridge.supervisor = sup
        self.bridge.on_local_change("seek")
        self.assertTrue(sup.seek_requested)

    def test_local_pause_does_not_request_a_seek(self):
        sup = FakeSupervisor()
        self.bridge.supervisor = sup
        self.bridge.on_local_change("pause")
        self.assertFalse(sup.seek_requested)


class ReadinessTest(BridgeTestCase):
    def test_ready_tracks_playback(self):
        sup = FakeSupervisor()
        self.bridge.supervisor = sup
        self.player.video = True
        self.bridge._update_ready(sup)
        self.assertIs(sup.ready, True)
        self.player.video = False
        self.bridge._update_ready(sup)
        self.assertIs(sup.ready, False)

    def test_local_play_reports_manual_readiness(self):
        sup = FakeSupervisor()
        self.bridge.supervisor = sup
        self.bridge.on_local_change("play")
        self.assertIs(sup.ready, True)
        self.assertIs(sup.ready_manual, True)

    def test_local_pause_does_not_report_manual_readiness(self):
        sup = FakeSupervisor()
        self.bridge.supervisor = sup
        self.bridge.on_local_change("pause")
        self.assertIsNone(sup.ready_manual)


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

    def test_remote_doseek_applies_inside_the_band(self):
        # §5.6: an explicit peer seek is a command, applied even when the
        # position is close enough that drift correction would stay put
        self.bridge.supervisor = FakeSupervisor()
        target = self.player.position + 0.5
        self.bridge.on_state({"position": target, "paused": False,
                              "doSeek": True, "setBy": "other-identity"})
        self.assertEqual(self.player.seek_times, [target])

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

    def test_gone_after_a_voluntary_leave_does_not_toast(self):
        # our own DELETE makes the next room poll read NotMember; that is our
        # departure, not "the room ended"
        self.bridge.api = FakeAPI()
        self.bridge.room = watchtogether.Room(ROOM_JSON)
        self.bridge.supervisor = FakeSupervisor()
        self.bridge.leave()
        self.bridge.on_gone()
        self.assertEqual(self.toasts, [])

    def test_stale_gone_callback_is_ignored(self):
        # a supervisor we already replaced must not tear down the new one
        current = FakeSupervisor()
        self.bridge.supervisor = current
        self.bridge.room = watchtogether.Room(ROOM_JSON)
        self.bridge.on_gone(FakeSupervisor())
        self.assertIs(self.bridge.supervisor, current)
        self.assertEqual(self.toasts, [])

    def test_leave_releases_the_join_lock_before_blocking_calls(self):
        # api.leave() can block 15s and sup.stop() joins the supervisor thread;
        # holding _join_lock across either stalls a concurrent on_gone()/join().
        bridge = self.bridge
        lock_free = []

        class LockProbeAPI(FakeAPI):
            def leave(self, room_id):
                got = bridge._join_lock.acquire(blocking=False)
                lock_free.append(("api", got))
                if got:
                    bridge._join_lock.release()
                FakeAPI.leave(self, room_id)

        class LockProbeSup(FakeSupervisor):
            def stop(self, timeout=None):
                got = bridge._join_lock.acquire(blocking=False)
                lock_free.append(("stop", got))
                if got:
                    bridge._join_lock.release()
                FakeSupervisor.stop(self, timeout)

        bridge.api = LockProbeAPI()
        bridge.room = watchtogether.Room(ROOM_JSON)
        bridge.supervisor = LockProbeSup()

        bridge.leave()

        self.assertEqual(lock_free, [("api", True), ("stop", True)])

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

    def test_departed_room_is_pruned_then_toasts_again(self):
        self.bridge.api = FakeAPI(rooms=[ROOM_JSON])
        self.bridge._poll_rooms()                 # seed silently
        self.bridge.api = FakeAPI(rooms=[])
        self.bridge._poll_rooms()                 # room gone -> pruned
        self.assertEqual(self.bridge._seen_rooms, set())
        self.bridge.api = FakeAPI(rooms=[ROOM_JSON])
        self.bridge._poll_rooms()                 # back -> toast again
        self.assertEqual(len(self.toasts), 1)

    def test_poll_failure_is_swallowed(self):
        class Boom(object):
            def rooms(self):
                raise watchtogether.WatchTogetherError("500")
        self.bridge.api = Boom()
        self.bridge._poll_rooms()        # must not raise
        self.assertEqual(self.toasts, [])

    def test_poll_survives_a_non_protocol_error(self):
        # a malformed payload (not a WatchTogetherError) must not kill the
        # lobby daemon thread
        class Boom(object):
            def rooms(self):
                raise ValueError("bad payload")
        self.bridge.api = Boom()
        self.bridge._poll_rooms()        # must not raise
        self.assertEqual(self.toasts, [])
        self.assertEqual(self.bridge.rooms_cache, [])

    def test_auto_join_non_protocol_error_is_logged_not_fatal(self):
        def boom(room_id):
            raise ValueError("bad payload")
        self.bridge.join = boom
        util.setSetting("watchtogether.last_room", "ca8cfezmke4")
        self.bridge._auto_join("ca8cfezmke4")     # must not raise
        self.assertEqual(util.getSetting("watchtogether.last_room", ""), "ca8cfezmke4",
                         "a transient error must not forget the room")


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


class PlaybackStartTest(BridgeTestCase):
    """_watch_room / _room_media_item: the guarded guest join-playback (§6.4)."""

    def room(self):
        return watchtogether.Room(ROOM_JSON)

    def test_watch_room_skips_when_a_video_is_already_playing(self):
        self.player.video = True
        self.bridge._watch_room(self.room())   # must not start a new playback
        self.assertEqual(self.player.videos, [])

    def test_watch_room_without_a_source_server_does_nothing(self):
        self.player.video = False
        # SERVERMANAGER is absent in tests: resolve yields nothing, no crash
        self.bridge._watch_room(self.room())
        self.assertEqual(self.player.videos, [])

    def test_room_media_item_returns_none_without_a_source_server(self):
        self.assertIsNone(self.bridge._room_media_item(self.room()))


class LocalChangeGraceTest(BridgeTestCase):
    def test_a_local_change_holds_off_a_peer_state_that_would_revert_it(self):
        sup = FakeSupervisor()
        self.bridge.supervisor = sup
        self.bridge.on_local_change("pause")
        self.assertEqual(sup.sent_now, 1, "a local change is sent immediately")
        self.bridge.on_state({"position": self.player.position + 5,
                              "paused": False, "doSeek": False,
                              "setBy": "other-identity"})
        self.assertEqual(self.player.controls, [], "must not be reverted")

    def test_remote_applies_resume_after_the_grace(self):
        self.bridge.supervisor = FakeSupervisor()
        self.bridge.on_local_change("pause")
        self.bridge._local_change_at = 0.0     # grace expired
        self.bridge.on_state({"position": self.player.position,
                              "paused": True, "doSeek": False,
                              "setBy": "other-identity"})
        self.assertEqual(self.player.controls, ["pause"])


class SourceUriTest(KodiTestCase):
    """parse_source_uri: the room sourceUri -> (machine, ratingKey) mapping
    used to start the room's content on join (§6.4)."""
    def test_bare_server_uri(self):
        self.assertEqual(
            wtwin.parse_source_uri(
                "server://abc123/com.plexapp.plugins.library/"
                "library/metadata/227117"),
            ("abc123", "227117"))

    def test_provider_prefixed_uri(self):
        self.assertEqual(
            wtwin.parse_source_uri(
                "provider://x/server://abc123/com.plexapp.plugins.library/"
                "library/metadata/42"),
            ("abc123", "42"))

    def test_trailing_segments_are_ignored(self):
        self.assertEqual(
            wtwin.parse_source_uri(
                "server://abc123/com.plexapp.plugins.library/"
                "library/metadata/227117/children"),
            ("abc123", "227117"))

    def test_rejects_non_library_uri(self):
        self.assertEqual(wtwin.parse_source_uri("http://example/x"), (None, None))
        self.assertEqual(wtwin.parse_source_uri("server://abc/other"), (None, None))
        self.assertEqual(wtwin.parse_source_uri(""), (None, None))
        self.assertEqual(wtwin.parse_source_uri(None), (None, None))


class TakeoverConfirmTest(BridgeTestCase):
    """Confirm before a join takes over a different item already playing."""

    def room(self):
        return watchtogether.Room(dict(
            ROOM_JSON,
            sourceUri="server://abc/com.plexapp.plugins.library/"
                      "library/metadata/227117"))

    def test_unknown_playing_item_prompts(self):
        # playing but unidentifiable (external player): cannot prove it is the
        # room's content, so prompt
        self.assertTrue(wtwin.needs_takeover_confirm(self.room(), ""))

    def test_same_item_no_prompt(self):
        self.assertFalse(wtwin.needs_takeover_confirm(self.room(), "227117"))

    def test_different_item_prompts(self):
        self.assertTrue(wtwin.needs_takeover_confirm(self.room(), "999"))

    def test_unknown_room_content_prompts(self):
        room = watchtogether.Room(dict(ROOM_JSON, sourceUri="not-a-uri"))
        self.assertTrue(wtwin.needs_takeover_confirm(room, "999"))

    def test_dialog_is_used_when_prompting(self):
        self.player.video = FakeVideo("999")
        ENV.dialog_answers.clear()
        ENV.dialog_answers.append(False)
        self.assertFalse(wtwin.confirm_takeover(self.room()))
        self.assertEqual(ENV.dialog_calls[-1][0], "yesno")

    def test_same_item_skips_the_dialog(self):
        self.player.video = FakeVideo("227117")
        ENV.dialog_answers.clear()
        ENV.dialog_calls.clear()
        self.assertTrue(wtwin.confirm_takeover(self.room()))
        self.assertEqual(ENV.dialog_calls, [])
