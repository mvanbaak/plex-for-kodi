# coding=utf-8
"""lib/windows/watchtogether.py — the Home hub data model (spec: home hub)."""

from __future__ import absolute_import

from kodienv import ENV

ENV.abort_requested = True
from lib import util, watchtogether  # noqa: E402
from lib.windows import watchtogether as wtwin  # noqa: E402

from .base import KodiTestCase  # noqa: E402

ROOM = {
    "id": "ca8cfezmke4",
    "title": "A Fazenda · T18 · E25 · Episode 25",
    "sourceUri": "server://abc/com.plexapp.plugins.library/library/metadata/227117",
    "users": [
        {"id": 1, "username": "michiel", "title": "Amanda & Michiel"},
        {"id": 2, "username": "yogarine", "title": "Alwin & Andréa"},
    ],
}


class ParticipantNamesTest(KodiTestCase):
    def names(self, users):
        return wtwin.participant_names(users)

    def test_no_users_is_empty(self):
        self.assertEqual(self.names([]), "")

    def test_one_user(self):
        self.assertEqual(self.names([{"title": "Amanda"}]), "Amanda")

    def test_two_users_joined_with_and(self):
        self.assertEqual(self.names([{"title": "A"}, {"title": "B"}]), "A and B")

    def test_three_users_use_commas_and_and(self):
        self.assertEqual(self.names([{"title": "A"}, {"title": "B"}, {"title": "C"}]),
                         "A, B and C")

    def test_falls_back_to_username(self):
        self.assertEqual(self.names([{"username": "yogarine"}]), "yogarine")


class RoomItemTest(KodiTestCase):
    def item(self, image=None):
        return wtwin.WatchTogetherRoomItem(watchtogether.Room(ROOM), image)

    def test_fields(self):
        item = self.item("http://img/1")
        self.assertEqual(item.type, "watchtogether")
        self.assertEqual(item.title, ROOM["title"])
        self.assertEqual(item.subtitle, "Amanda & Michiel and Alwin & Andréa")
        self.assertEqual(item.image, "http://img/1")
        self.assertFalse(item.cachable)

    def test_missing_image_uses_placeholder(self):
        self.assertEqual(self.item().image, wtwin.WATCHTOGETHER_PLACEHOLDER)

    def test_get_is_inert(self):
        self.assertIsNone(self.item().get("art"))
        self.assertEqual(self.item().get("art", "x"), "x")

    def test_empty_users_fall_back_to_count(self):
        room = watchtogether.Room(dict(ROOM, users=[]))
        item = wtwin.WatchTogetherRoomItem(room)
        self.assertEqual(item.subtitle, util.T(35054, "{} watching").format(0))


class RoomsHubTest(KodiTestCase):
    def test_identifier_and_title(self):
        hub = wtwin.WatchTogetherRoomsHub(lambda: [])
        self.assertEqual(hub.hubIdentifier, wtwin.WATCHTOGETHER_HUB_ID)
        self.assertEqual(hub.getCleanHubIdentifier(), wtwin.WATCHTOGETHER_HUB_ID)
        self.assertEqual(hub.title, util.T(35053, "Watch Together"))

    def test_reload_rebuilds_from_factory(self):
        items = []
        hub = wtwin.WatchTogetherRoomsHub(lambda: items)
        self.assertEqual(hub.items, [])
        items.append("x")
        hub.reload()
        self.assertEqual(hub.items, ["x"])

    def test_reset_does_not_touch_item_containers(self):
        # our items are not PlexObjects: BaseHub.reset would AttributeError
        hub = wtwin.WatchTogetherRoomsHub(lambda: [object()])
        hub.reset()          # must not raise
        self.assertFalse(hub.more.asBool())


class HomeHubTest(KodiTestCase):
    def setUp(self):
        super(HomeHubTest, self).setUp()
        self.bridge = wtwin.WatchTogetherBridge()

    def test_no_rooms_no_hub(self):
        self.assertIsNone(self.bridge.home_hub())

    def test_rooms_build_items_and_bump_version(self):
        self.bridge.api = _StubAPI([ROOM])
        self.bridge._poll_rooms()
        hub = self.bridge.home_hub()
        self.assertIsNotNone(hub)
        self.assertEqual(len(hub.items), 1)
        self.assertIsInstance(hub.items[0], wtwin.WatchTogetherRoomItem)

    def test_version_changes_only_when_the_room_set_changes(self):
        self.bridge.api = _StubAPI([ROOM])
        self.bridge._poll_rooms()
        first = self.bridge.rooms_version
        self.bridge._poll_rooms()
        self.assertEqual(self.bridge.rooms_version, first, "same rooms: no bump")
        self.bridge.api = _StubAPI([])
        self.bridge._poll_rooms()
        self.assertGreater(self.bridge.rooms_version, first)

    def test_emptying_removes_the_hub(self):
        self.bridge.api = _StubAPI([ROOM])
        self.bridge._poll_rooms()
        self.assertIsNotNone(self.bridge.home_hub())
        self.bridge.api = _StubAPI([])
        self.bridge._poll_rooms()
        self.assertIsNone(self.bridge.home_hub())


class _StubAPI(object):
    def __init__(self, rooms):
        self._rooms = rooms

    def rooms(self):
        return [watchtogether.Room(r) for r in self._rooms]

    def room(self, room_id):
        return watchtogether.Room(ROOM)

    def leave(self, room_id):
        pass


class _FakeArt(object):
    def __init__(self, url):
        self.url = url

    def asTranscodedImageURL(self, w, h):
        return "{0}?w={1}&h={2}".format(self.url, w, h)


class _FakeItem(object):
    def __init__(self, type_, thumb=None, art=None):
        self.type = type_
        self.defaultThumb = thumb
        self.defaultArt = art


class ArtResolveTest(KodiTestCase):
    def setUp(self):
        super(ArtResolveTest, self).setUp()
        self.bridge = wtwin.WatchTogetherBridge()

    def _patch(self, server_items, server_present=True):
        import lib.windows.watchtogether as wt
        self._saved_list = wt.plexobjects.listItems
        self._saved_sm = wt.plexapp.SERVERMANAGER
        wt.plexobjects.listItems = lambda server, path: server_items

        class _SM(object):
            serversByUuid = {"abc": object()} if server_present else {}
        wt.plexapp.SERVERMANAGER = _SM()

    def tearDown(self):
        import lib.windows.watchtogether as wt
        wt.plexobjects.listItems = self._saved_list
        wt.plexapp.SERVERMANAGER = self._saved_sm
        super(ArtResolveTest, self).tearDown()

    def test_episode_uses_thumb(self):
        self._patch([_FakeItem("episode", thumb=_FakeArt("t"), art=_FakeArt("a"))])
        url = self.bridge._resolve_room_art(watchtogether.Room(ROOM))
        self.assertIn("t?", url)

    def test_movie_uses_art(self):
        self._patch([_FakeItem("movie", thumb=_FakeArt("t"), art=_FakeArt("a"))])
        url = self.bridge._resolve_room_art(watchtogether.Room(ROOM))
        self.assertIn("a?", url)

    def test_missing_server_is_placeholder(self):
        self._patch([], server_present=False)
        self.assertEqual(self.bridge._resolve_room_art(watchtogether.Room(ROOM)),
                         wtwin.WATCHTOGETHER_PLACEHOLDER)

    def test_no_item_is_placeholder(self):
        self._patch([])
        self.assertEqual(self.bridge._resolve_room_art(watchtogether.Room(ROOM)),
                         wtwin.WATCHTOGETHER_PLACEHOLDER)

    def test_poll_caches_and_prunes_art(self):
        self._patch([_FakeItem("episode", thumb=_FakeArt("t"))])
        self.bridge.api = _StubAPI([ROOM])
        self.bridge._poll_rooms()
        self.assertIn(ROOM["id"], self.bridge.room_art)
        self.bridge.api = _StubAPI([])
        self.bridge._poll_rooms()
        self.assertEqual(self.bridge.room_art, {})


class RoomClickedTest(KodiTestCase):
    def setUp(self):
        super(RoomClickedTest, self).setUp()
        self.bridge = wtwin.WatchTogetherBridge()
        self.room = watchtogether.Room(ROOM)
        self.calls = []
        self._saved_join = self.bridge.join
        self._saved_leave = self.bridge.leave
        self.bridge.join = lambda rid: self.calls.append(("join", rid))
        self.bridge.leave = lambda: self.calls.append(("leave",))
        self._saved_confirm_switch = wtwin.confirm_switch
        self._saved_confirm_takeover = wtwin.confirm_takeover
        self._saved_open = wtwin.ParticipantsDialog.open
        wtwin.ParticipantsDialog.open = lambda: self.calls.append(("participants",))

    def tearDown(self):
        self.bridge.join = self._saved_join
        self.bridge.leave = self._saved_leave
        wtwin.confirm_switch = self._saved_confirm_switch
        wtwin.confirm_takeover = self._saved_confirm_takeover
        wtwin.ParticipantsDialog.open = self._saved_open
        super(RoomClickedTest, self).tearDown()

    def test_no_room_confirms_takeover_then_joins(self):
        wtwin.confirm_takeover = lambda room: True
        self.bridge.room_clicked(self.room)
        self.assertEqual(self.calls, [("join", "ca8cfezmke4")])

    def test_takeover_declined_does_nothing(self):
        wtwin.confirm_takeover = lambda room: False
        self.bridge.room_clicked(self.room)
        self.assertEqual(self.calls, [])

    def test_same_room_opens_participants(self):
        self.bridge.supervisor = object()
        self.bridge.room = self.room
        self.bridge.room_clicked(self.room)
        self.assertEqual(self.calls, [("participants",)])

    def test_switch_confirmed_leaves_then_joins(self):
        wtwin.confirm_switch = lambda: True
        self.bridge.supervisor = object()
        self.bridge.room = watchtogether.Room(dict(ROOM, id="other"))
        self.bridge.room_clicked(self.room)
        self.assertEqual(self.calls, [("leave",), ("join", "ca8cfezmke4")])

    def test_switch_declined_does_nothing(self):
        wtwin.confirm_switch = lambda: False
        self.bridge.supervisor = object()
        self.bridge.room = watchtogether.Room(dict(ROOM, id="other"))
        self.bridge.room_clicked(self.room)
        self.assertEqual(self.calls, [])


class ShowEntryTest(KodiTestCase):
    def setUp(self):
        super(ShowEntryTest, self).setUp()
        self.opened = []
        self._saved_open = wtwin.ParticipantsDialog.open
        wtwin.ParticipantsDialog.open = lambda: self.opened.append(1)
        self._saved_start = wtwin.bridge.start
        wtwin.bridge.start = lambda: None
        self._saved_sup = wtwin.bridge.supervisor

    def tearDown(self):
        wtwin.ParticipantsDialog.open = self._saved_open
        wtwin.bridge.start = self._saved_start
        wtwin.bridge.supervisor = self._saved_sup
        super(ShowEntryTest, self).tearDown()

    def test_no_room_does_nothing(self):
        wtwin.bridge.supervisor = None
        wtwin.show()
        self.assertEqual(self.opened, [])

    def test_in_room_opens_participants(self):
        wtwin.bridge.supervisor = object()
        wtwin.show()
        self.assertEqual(self.opened, [1])
