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
