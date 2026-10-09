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

from kodi_six import xbmc, xbmcgui
from plexnet import plexapp, plexlibrary, plexobjects

from lib import plex, player, syncplay, util, watchtogether, ws
from . import busy, kodigui


def parse_source_uri(uri):
    """Room sourceUri -> (machineIdentifier, ratingKey), or (None, None).

    Accepts bare `server://<machine>/…/library/metadata/<key>` and the
    provider-prefixed `provider://…/server://<machine>/…` form (§4)."""
    if not uri or "server://" not in uri:
        return None, None
    rest = uri.rsplit("server://", 1)[1]
    machine, _, path = rest.partition("/")
    marker = "library/metadata/"
    idx = path.find(marker)
    if not machine or idx < 0:
        return None, None
    rating_key = path[idx + len(marker):].split("/")[0].split("?")[0]
    if not rating_key:
        return None, None
    return machine, rating_key


WATCHTOGETHER_HUB_ID = "watchtogether.rooms"
WATCHTOGETHER_PLACEHOLDER = "script.plex/thumb_fallbacks/movie16x9.png"
LOCAL_CHANGE_GRACE = 1.5   # seconds to hold off remote applies after a local change


def participant_names(users):
    """users[] -> "A", "A and B", "A, B and C" (title, falling back to username)."""
    names = []
    for user in users or []:
        if not isinstance(user, dict):
            continue
        name = user.get("title") or user.get("username")
        if name:
            names.append(name)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return "{0} and {1}".format(", ".join(names[:-1]), names[-1])


def _player():
    """The live PlexPlayer, or None during addon shutdown (player.PLAYER is
    deleted by player.shutdown() while daemon threads may still tick)."""
    return getattr(player, "PLAYER", None)


class WatchTogetherRoomItem(object):
    """One room tile. Not a PlexObject — the home renderer must not treat it
    as media, so `get()` is inert and `cachable` is False."""
    def __init__(self, room, image=None):
        self.room = room
        self.type = "watchtogether"
        self.title = room.title
        self.subtitle = participant_names(room.participants) or \
            util.T(35054, "{} watching").format(len(room.participants))
        self.image = image or WATCHTOGETHER_PLACEHOLDER
        self.cachable = False
        # Home's hub position/reselect and storeLastBG read these directly;
        # a plain object must still carry them (id itself is never logged).
        self.ratingKey = str(room.id)
        self.art = None
        self.thumb = None

    def get(self, key, default=None):
        return default


class WatchTogetherRoomsHub(plexlibrary.BaseHub):
    """Client-built Home hub (no Plex hub backs it), modelled on CollectionsHub.
    `factory()` rebuilds the item list from the bridge's live cache."""
    TYPE = "Hub"
    type = "watchtogether"
    hubIdentifier = WATCHTOGETHER_HUB_ID

    def __init__(self, factory, *args, **kwargs):
        super(WatchTogetherRoomsHub, self).__init__(False, *args, **kwargs)
        self._factory = factory
        self.items = factory()
        self.set("title", util.T(35053, "Watch Together"))

    def getCleanHubIdentifier(self, is_home=False):
        return self.hubIdentifier

    def reset(self):
        # BaseHub.reset reads items[0].container; ours are plain objects
        self.set("offset", 0)
        self.set("size", len(self.items))
        self.set("more", "")

    def reload(self, **kwargs):
        self.items = self._factory()
        return self


def needs_takeover_confirm(room, playing_key):
    """True when joining may take over a *different* item already playing.

    Call only when a video is playing. Unknown room content or an
    unidentifiable playing item both prompt, to be safe."""
    _, room_key = parse_source_uri(room.source_uri)
    if not room_key or not playing_key:
        return True
    return str(playing_key) != room_key


def playing_rating_key():
    video = getattr(player.PLAYER, "video", None)
    return str(getattr(video, "ratingKey", "") or "")


def confirm_takeover(room):
    """True if joining may proceed: nothing playing, the room's own content,
    or the user confirmed the takeover."""
    if not player.PLAYER.isPlayingVideo():
        return True
    if not needs_takeover_confirm(room, playing_rating_key()):
        return True
    return xbmcgui.Dialog().yesno(
        util.T(35053, "Watch Together"),
        util.T(35060, "You are already watching something else. "
                      "Join and take over playback?"))


def confirm_switch():
    return xbmcgui.Dialog().yesno(
        util.T(35053, "Watch Together"),
        util.T(35061, "Leave the current room and join this one?"))


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
        self.rooms_version = 0
        self.room_art = {}
        self._rooms_key = None
        self._rooms_seeded = False
        self._seen_rooms = set()
        self._thread = None
        self._stop = threading.Event()
        self._join_lock = threading.Lock()
        self._auto_join_done = False
        self._local_change_at = 0.0
        self._tempo = 1.0
        self._tempo_retry_at = 0.0
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
            try:
                sup = self.supervisor
                if sup is None:
                    if not self._rooms_seeded or ticks % 15 == 0:
                        self._poll_rooms()
                else:
                    if sup.connected != self._was_connected:
                        self._was_connected = sup.connected
                        self.update_status()
                    self.push_local()
                    self._update_ready(sup)
            except Exception:
                # never let one bad tick kill the lobby/heartbeat thread
                util.ERROR()

    def ensure_api(self):
        if self.api is None:
            self.api = watchtogether.RoomsApi(plexapp.ACCOUNT.authToken)
        return self.api

    # -- lobby (no supervisor running) ---------------------------------------

    def _poll_rooms(self):
        try:
            rooms = self.ensure_api().rooms()
        except Exception as exc:
            # WatchTogetherError is expected (offline/HTTP); anything else is a
            # payload bug — still swallow it so the lobby thread survives.
            # class name only: the room id is a credential (§7)
            util.DEBUG_LOG("Watch Together: room poll failed: {0}".format(exc.__class__.__name__))
            return
        self.rooms_cache = rooms
        key = tuple(sorted((r.id, len(r.participants)) for r in rooms))
        if key != self._rooms_key:
            self._rooms_key = key
            self.rooms_version += 1
        live = set()
        for room in rooms:
            live.add(room.id)
            if room.id not in self.room_art:
                self.room_art[room.id] = self._resolve_room_art(room)
        for room_id in list(self.room_art):
            if room_id not in live:
                del self.room_art[room_id]
        current = set(r.id for r in rooms)
        if not self._rooms_seeded:
            # first paint: remember, never toast what was already there
            self._rooms_seeded = True
            self._seen_rooms = current
            return
        for room in rooms:
            if room.id not in self._seen_rooms:
                util.showNotification(
                    util.T(35058, "New Watch Together room: {}").format(room.title))
        # prune gone ids so a room re-created later can toast again, and the
        # set cannot grow without bound
        self._seen_rooms = current

    def refresh_room(self):
        """Fresh GET /rooms/{id} while the participants dialog is open."""
        sup = self.supervisor
        if sup is None:
            return

        def work():
            try:
                self.room = self.ensure_api().room(sup.room.id)
            except Exception as exc:
                util.DEBUG_LOG("Watch Together: room refresh failed: {0}".format(
                    exc.__class__.__name__))

        thread = threading.Thread(target=work, name="wt-room")
        thread.daemon = True
        thread.start()

    # -- home hub ------------------------------------------------------------

    def _resolve_room_art(self, room):
        """16:9 art for a room, resolved from sourceUri against its source
        server. Never raises: any failure yields the placeholder."""
        machine, key = parse_source_uri(room.source_uri)
        if not machine or not key:
            return WATCHTOGETHER_PLACEHOLDER
        try:
            servers = getattr(plexapp.SERVERMANAGER, "serversByUuid", None) or {}
            server = servers.get(machine)
            if server is None:
                return WATCHTOGETHER_PLACEHOLDER
            items = plexobjects.listItems(server, "/library/metadata/%s" % key)
            if not items:
                return WATCHTOGETHER_PLACEHOLDER
            item = items[0]
            art = item.defaultThumb if getattr(item, "type", None) == "episode" \
                else item.defaultArt
            if not art:
                art = item.defaultThumb
            return art.asTranscodedImageURL(532, 299)
        except Exception:
            return WATCHTOGETHER_PLACEHOLDER

    def _build_room_items(self):
        return [WatchTogetherRoomItem(r, self.room_art.get(r.id))
                for r in self.rooms_cache]

    def home_hub(self):
        if not self.rooms_cache:
            return None
        return WatchTogetherRoomsHub(self._build_room_items)

    # -- local -> relay -------------------------------------------------------

    def push_local(self):
        """Feed the supervisor's snapshot at 1 Hz (§5.5). The supervisor sends;
        this only updates what it will send."""
        sup = self.supervisor
        pl = _player()
        if sup is None or pl is None:
            return
        # isPlayingVideo, not isPlaying: theme music runs through this same
        # player. Before playback starts, echo the room's position (never 0,
        # which would make us the driver at the start); the supervisor also
        # holds States until synced.
        if not pl.isPlayingVideo():
            session = sup.session
            pos = int((session.remote.get("position", 0) if session else 0) or 0)
            sup.outbound_state({"position": pos, "paused": True, "doSeek": False})
            return
        sup.outbound_state({
            "position": int(pl.getTime() or 0),
            "paused": bool(xbmc.getCondVisibility("Player.Paused")),
            "doSeek": False,
        })

    def _update_ready(self, sup):
        """§6.4: ready when video is loaded and not still buffering."""
        pl = _player()
        ready = bool(pl is not None and pl.isPlayingVideo()
                     and not xbmc.getCondVisibility("Player.Caching"))
        sup.set_ready(ready)

    def on_local_change(self, kind):
        """Kodi fired onPlayBack* for a local event (the gate let it through)."""
        util.DEBUG_LOG("Watch Together: local {0}, broadcasting state".format(kind))
        sup = self.supervisor
        if kind == "seek" and sup is not None:
            # §5.6: a local seek is a command, not just a position report
            sup.request_seek()
        if kind == "play" and sup is not None:
            # the user pressed play: that is a manual readiness (§6.4)
            sup.set_ready(True, manually=True)
        # a local change must win: send it now and hold off incoming states
        # briefly, or a peer's older State reverts it (last-setBy-wins, but
        # only after ours lands)
        self._local_change_at = time.monotonic()
        self.push_local()
        if sup is not None:
            sup.send_now()

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
        if time.monotonic() - self._local_change_at < LOCAL_CHANGE_GRACE:
            return          # our own change is in flight; don't be reverted
        pl = _player()
        # §6.3 foreground/background: v1 approximates "foreground" with "video
        # playing" (no ad-break sync — an explicit v1 non-goal). The lobby and
        # theme-music cases stay background and ignore the relay's playstate.
        if pl is None or not pl.isPlayingVideo():
            return
        local_pos = pl.getTime() or 0.0
        # publish our own position only once it actually matches the room: a
        # just-started video at ~0 (or a seek that hasn't landed yet) would
        # otherwise become the room position and drag everyone to the start
        if abs(local_pos - remote.get("position", 0.0)) < 2.0:
            sup.mark_synced()
        action = syncplay.sync_action(local_pos, remote.get("position", 0.0),
                                      remote.get("paused", True),
                                      session.latency.forward_delay)
        want_paused = remote.get("paused")
        is_paused = bool(xbmc.getCondVisibility("Player.Paused"))
        applied = False
        # arm the echo deadline only when we actually change something: remote
        # States arrive ~1 Hz, so arming on every frame would keep the gate
        # closed and swallow every genuine local event (design §Echo)
        if want_paused is not None and want_paused != is_paused:
            pl.wt_applying_remote = time.monotonic() + 2.0
            pl.control("pause" if want_paused else "play")
            self._set_tempo(1.0)
            applied = True
        if remote.get("doSeek"):
            # §5.6: a peer's explicit seek command — apply even inside the
            # drift band, where sync_action would stay put
            pl.wt_applying_remote = time.monotonic() + 2.0
            self._seek_to(remote.get("position", 0.0))
            self._set_tempo(1.0)
            applied = True
        elif action and action[0] == "seek":
            pl.wt_applying_remote = time.monotonic() + 2.0
            self._seek_to(action[1])
            self._set_tempo(1.0)
            applied = True
        elif action and action[0] == "tempo":
            # §6.2 gentle catch-up: slow to 0.95 with pitch preserved (Kodi 21+)
            result = self._set_tempo(action[1])
            if result is False:
                # no tempo here: degrade to a hard seek so drift still converges
                target = remote.get("position", 0.0) + \
                    (0 if remote.get("paused") else session.latency.forward_delay)
                pl.wt_applying_remote = time.monotonic() + 2.0
                self._seek_to(target)
                applied = True
            # result True: tempo applied; None: player busy, retry next tick
        else:
            self._set_tempo(1.0)   # inside the drift band: clear any catch-up
        if applied:
            util.DEBUG_LOG("Watch Together: applied remote {0}".format(action))

    def _set_tempo(self, tempo):
        """§6.2 pitch-preserved tempo catch-up, via JSON-RPC Player.SetTempo
        (Kodi 21+). Returns True when applied, False when unavailable/failed
        (caller hard-seeks), or None when the player is busy seeking/caching
        (retry next tick)."""
        if tempo == self._tempo:
            return True
        # SetTempo is refused while the player is mid-seek or buffering (seen
        # on Kodi 21.0); don't call it then, and don't hard-seek either.
        if (xbmc.getCondVisibility("Player.Seeking")
                or xbmc.getCondVisibility("Player.Caching")):
            return None
        now = time.monotonic()
        if now < self._tempo_retry_at:
            return False
        if util.KODI_VERSION_MAJOR < 21:
            self._tempo_retry_at = now + 3600.0
            util.LOG("Watch Together: tempo catch-up needs Kodi 21+ (have {0}); "
                     "using hard seek".format(util.KODI_VERSION_MAJOR))
            return False
        try:
            players = util.rpc.Player.GetActivePlayers() or []
            playerid = next((p["playerid"] for p in players
                             if p.get("type") == "video"), None)
            if playerid is None:
                return False
            util.rpc.Player.SetTempo(playerid=playerid, tempo=tempo)
            self._tempo = tempo
            util.DEBUG_LOG("Watch Together: tempo {0}".format(tempo))
            return True
        except Exception as exc:
            # SetTempo is advertised on Kodi 21 but the player may still refuse
            # it. Back off and hard-seek meanwhile.
            self._tempo_retry_at = now + 60.0
            self._tempo = 1.0
            util.LOG("Watch Together: SetTempo failed ({0}); using hard seek "
                     "(retry in 60s)".format(exc))
            return False

    def _seek_to(self, target):
        """Through the seek dialog when it exists (it owns the full local seek
        path — transcodes, bookkeeping); plain seekTime otherwise. During video
        playback the dialog is created up front, so the fallback is the edge."""
        target = max(target, 0)
        pl = _player()
        if pl is None:
            return
        dialog = getattr(getattr(pl, "handler", None), "dialog", None)
        if dialog:
            dialog.doSeek(int(target * 1000))
        else:
            pl.seekTime(target)

    # -- supervisor callbacks ---------------------------------------------------

    def on_roster(self, room):
        self.room = room
        self.update_status()

    def on_disconnected(self):
        util.DEBUG_LOG("Watch Together: relay connection lost, reconnecting")
        self.update_status()

    def on_event(self, kind, key):
        util.DEBUG_LOG("Watch Together: roster {0}".format(kind))

    def on_gone(self, sup=None):
        """Room ended / removed / dead token — supervisor thread. Teardown,
        no DELETE (§4). Dialogs notice supervisor=None and close themselves.

        `sup` is the supervisor that fired; when it is not the current one the
        callback is stale (we already left/rejoined) and must be ignored."""
        with self._join_lock:
            current = self.supervisor
            if current is None:
                # already left/detached: a NotMember poll after our own DELETE
                # is our departure, not a room that ended — do not announce it
                return
            if sup is not None and current is not sup:
                return
            self.supervisor = None
            self.room = None
            self._reset_player_link(forget_room=True)
        util.showNotification(util.T(35059, "The Watch Together room has ended"))
        if current:
            current.stop()

    # -- join / leave -----------------------------------------------------------

    def join(self, room_id):
        with self._join_lock:
            if self.supervisor is not None:
                return self.supervisor
        # fetch + build outside the lock: the REST call can block 15s and must
        # not stall a concurrent leave()/on_gone()
        room = self.ensure_api().room(room_id)     # raises Auth/NotMember/Gone
        identity = syncplay.build_identity(plex.CLIENT_ID, plex.getFriendlyName(),
                                           plexapp.ACCOUNT.ID)
        sup = watchtogether.SessionSupervisor(room, identity,
                                              plexapp.ACCOUNT.authToken,
                                              _ws_factory, log=util.DEBUG_LOG)
        sup.on_state = self.on_state
        sup.on_roster = self.on_roster
        sup.on_disconnected = self.on_disconnected
        sup.on_gone = lambda s=sup: self.on_gone(s)
        sup.on_event = self.on_event
        with self._join_lock:
            if self.supervisor is not None:   # someone joined while we fetched
                return self.supervisor
            sup.start()
            self.room = room
            self.supervisor = sup
            self._was_connected = False
            pl = _player()
            if pl is not None:
                pl.wt_broadcast = self.on_local_change
                pl.wt_applying_remote = 0.0
            util.setSetting("watchtogether.last_room", room_id)
            self.update_status()
        return sup

    def _room_media_item(self, room):
        """Resolve the room's sourceUri to a playable, or None (best effort)."""
        machine, rating_key = parse_source_uri(room.source_uri)
        if not machine or not rating_key:
            return None
        try:
            servers = getattr(plexapp.SERVERMANAGER, "serversByUuid", None) or {}
            server = servers.get(machine)
            if server is None:
                util.DEBUG_LOG("Watch Together: source server not available")
                return None
            items = plexobjects.listItems(server,
                                          "/library/metadata/%s" % rating_key)
            if not items:
                util.DEBUG_LOG("Watch Together: room item not found")
                return None
            return items[0]
        except Exception:
            util.ERROR()
            return None

    def _watch_room(self, room):
        """Open the room's content in the normal video player window, so space,
        the OSD and stop all behave as usual. Playback stops -> we end the
        session (socket closed, peers told), but keep membership so the tile
        can rejoin. Returns the player window's exit command (the caller
        processes it, like preplay does); must run on the main thread."""
        pl = _player()
        if pl is None or pl.isPlayingVideo():
            return None
        item = self._room_media_item(room)
        if item is None:
            return None
        from . import videoplayer
        command = None
        try:
            command = videoplayer.play(video=item)
        except Exception:
            util.ERROR()
        finally:
            if self.supervisor is not None:
                self.disconnect()
        return command

    def disconnect(self):
        """End the session without DELETE: closing the socket tells peers we
        left (§5.8) but the room keeps us, so the tile can rejoin. Contrast
        leave(), which is the explicit 'leave the room' (DELETE)."""
        with self._join_lock:
            sup, self.supervisor = self.supervisor, None
            self.room = None
            self._reset_player_link(forget_room=False)
        if sup:
            sup.stop()

    def leave(self):
        # Detach + reset under the lock, then do the blocking REST call and
        # thread join outside it: holding _join_lock across api.leave() (up to
        # 15s) and sup.stop() (joins the supervisor) would stall a concurrent
        # on_gone()/join() — and on_gone() runs on the supervisor thread that
        # stop() is waiting to join.
        with self._join_lock:
            sup, room = self.supervisor, self.room
            self.supervisor = None
            self.room = None
            self._reset_player_link(forget_room=True)
        if room is not None and self.api is not None:
            try:
                self.api.leave(room.id)
            except watchtogether.WatchTogetherError as exc:
                util.DEBUG_LOG("Watch Together: leave failed: {0}".format(
                    exc.__class__.__name__))
        if sup:
            sup.stop()

    def room_clicked(self, room):
        """Hub tile click: join and watch, switch, or (already watching) open
        participants. Playback stops -> leave, so a later click rejoins.
        Returns the video window's exit command, if any, for the caller."""
        sup = self.supervisor
        if sup is not None and self.room is not None and self.room.id == room.id:
            if _player() is not None and _player().isPlayingVideo():
                ParticipantsDialog.open()
                return None
            return self._watch_room(room)   # in the room but not watching
        leave_first = sup is not None
        if leave_first:
            if not confirm_switch():
                return None
        elif not confirm_takeover(room):
            return None
        try:
            self._join_or_switch(room.id, leave_first)
        except Exception as exc:
            util.DEBUG_LOG("Watch Together: join failed: {0}".format(
                exc.__class__.__name__))
            util.showNotification(str(exc))
            return None
        return self._watch_room(room)

    @busy.dialog()
    def _join_or_switch(self, room_id, leave_first):
        """Join/switch behind a busy spinner; the confirms stay in
        room_clicked so the busy window never covers them."""
        if leave_first:
            self.leave()
        self.join(room_id)

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
            util.DEBUG_LOG("Watch Together: auto-join failed: {0}".format(
                exc.__class__.__name__))
            util.setSetting("watchtogether.last_room", "")
        except Exception:
            # transient/unexpected: log but keep last_room so the next boot retries
            util.ERROR()

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


class ParticipantsDialog(kodigui.BaseDialog, util.CronReceiver):
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
        # the roster can be empty; focus the Leave button so the dialog is
        # never a dead end
        self.setFocusId(self.LEAVE_ID)
        util.CRON.registerReceiver(self)

    def onClosed(self):
        util.CRON.cancelReceiver(self)

    def tick(self):
        if bridge.supervisor is None:
            self.doClose()              # room gone / left from elsewhere
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
        self.doClose()
