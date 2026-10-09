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
