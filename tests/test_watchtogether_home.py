# coding=utf-8
"""lib/windows/home.py — Watch Together hub rendering (spec: home hub)."""

from __future__ import absolute_import

from kodienv import ENV

ENV.abort_requested = True
from lib import watchtogether  # noqa: E402
from lib.windows import home  # noqa: E402
from lib.windows import watchtogether as wtwin  # noqa: E402
from lib.windows.home import HomeWindow  # noqa: E402

from .base import KodiTestCase  # noqa: E402

HUB_ID = wtwin.WATCHTOGETHER_HUB_ID


class DisplayFlagsTest(KodiTestCase):
    def win(self):
        return HomeWindow.__new__(HomeWindow)

    def test_display_type_is_ar16x9(self):
        self.assertEqual(self.win().getHubDisplayType(None, HUB_ID), "ar16x9")

    def test_render_flags(self):
        flags = self.win().getHubRenderFlags(None, HUB_ID)
        self.assertTrue(flags["ar16x9"])
        self.assertFalse(flags["with_art"])
        self.assertFalse(flags["with_progress"])


class CreatorTest(KodiTestCase):
    def item(self):
        room = watchtogether.Room({"id": "r1", "title": "Room", "sourceUri": "",
                                  "users": [{"id": 1, "title": "A"}]})
        return wtwin.WatchTogetherRoomItem(room, "http://img/1")

    def test_creator_sets_title_subtitle_thumb(self):
        win = HomeWindow.__new__(HomeWindow)
        mli = win.createWatchTogetherListItem(self.item())
        self.assertEqual(mli.label, "Room")
        self.assertEqual(mli.label2, "A")
        self.assertEqual(mli.thumbnailImage, "http://img/1")
        self.assertEqual(mli.dataSource.type, "watchtogether")

    def test_creator_registered(self):
        self.assertIn("watchtogether", HomeWindow.CREATE_LI_MAP)


class InjectionTest(KodiTestCase):
    def win(self):
        return HomeWindow.__new__(HomeWindow)

    def test_no_hub_returns_original(self):
        class Sec(object):
            key = None
        hubs = home.HubsList([1, 2])
        hubs.identifier = "orig"
        self.assertIs(self.win()._with_watchtogether_hub(hubs, Sec()), hubs)

    def test_hub_is_prepended_and_metadata_preserved(self):
        import lib.windows.watchtogether as wt
        class Sec(object):
            key = None
        sentinel = object()
        saved = wt.bridge.home_hub
        wt.bridge.home_hub = lambda: sentinel
        try:
            hubs = home.HubsList([1, 2])
            hubs.identifier = "orig"
            hubs.lastUpdated = 123
            out = self.win()._with_watchtogether_hub(hubs, Sec())
        finally:
            wt.bridge.home_hub = saved
        self.assertEqual(out[0], sentinel)
        self.assertEqual(list(out[1:]), [1, 2])
        self.assertEqual(out.identifier, "orig")
        self.assertEqual(out.lastUpdated, 123)

    def test_non_home_section_returns_original(self):
        import lib.windows.watchtogether as wt
        class Sec(object):
            key = "1"
        saved = wt.bridge.home_hub
        wt.bridge.home_hub = lambda: object()
        try:
            hubs = home.HubsList([1])
            self.assertIs(self.win()._with_watchtogether_hub(hubs, Sec()), hubs)
        finally:
            wt.bridge.home_hub = saved


class HiddenBypassTest(KodiTestCase):
    def test_watchtogether_hub_is_never_hidden(self):
        win = HomeWindow.__new__(HomeWindow)
        win.isHubHidden = lambda identifier, key: True
        self.assertFalse(win._isHubHiddenFor(wtwin.WATCHTOGETHER_HUB_ID, None, False))

    def test_other_hubs_still_respect_the_hidden_setting(self):
        win = HomeWindow.__new__(HomeWindow)
        win.isHubHidden = lambda identifier, key: True
        class Sec(object):
            key = None
        self.assertTrue(win._isHubHiddenFor("movie.recentlyadded", Sec(), False))

    def test_cross_section_hubs_are_not_hidden_checked(self):
        win = HomeWindow.__new__(HomeWindow)
        win.isHubHidden = lambda identifier, key: (_ for _ in ()).throw(AssertionError("must not check"))
        self.assertFalse(win._isHubHiddenFor("movie.recentlyadded", None, True))


class ClickRouteTest(KodiTestCase):
    def test_wt_item_routes_to_room_clicked(self):
        import lib.windows.watchtogether as wt
        clicked = []
        saved = wt.bridge.room_clicked
        wt.bridge.room_clicked = clicked.append
        try:
            room = watchtogether.Room({"id": "r", "title": "t", "sourceUri": "",
                                       "users": []})
            item = wt.WatchTogetherRoomItem(room)

            class MLI(object):
                dataSource = item

            class Control(object):
                def getSelectedItem(self):
                    return MLI()

            win = HomeWindow.__new__(HomeWindow)
            win.hubControls = (Control(),)
            win.hubItemClicked(400)
        finally:
            wt.bridge.room_clicked = saved
        self.assertEqual(clicked, [item.room])


class IsWtItemTest(KodiTestCase):
    def test_true_for_room_item(self):
        win = HomeWindow.__new__(HomeWindow)
        room = watchtogether.Room({"id": "r", "title": "t", "sourceUri": "",
                                   "users": []})
        self.assertTrue(win._isWatchTogetherItem(wtwin.WatchTogetherRoomItem(room)))

    def test_false_for_other(self):
        win = HomeWindow.__new__(HomeWindow)
        self.assertFalse(win._isWatchTogetherItem(object()))


class RefreshTest(KodiTestCase):
    def win(self):
        win = HomeWindow.__new__(HomeWindow)
        win._wtRoomsVersion = None
        win._shown = []
        win.showHubs = lambda *a, **k: win._shown.append((a, k))
        win.getCurrentHubsPositions = lambda section: {"p": 1}
        return win

    def test_no_change_no_redraw(self):
        import lib.windows.watchtogether as wt
        saved = wt.bridge.rooms_version
        wt.bridge.rooms_version = 5
        try:
            win = self.win()
            win._wtRoomsVersion = 5
            self.assertFalse(win.checkWatchTogetherHub())
            self.assertEqual(win._shown, [])
        finally:
            wt.bridge.rooms_version = saved

    def test_change_redraws_home(self):
        import lib.windows.watchtogether as wt
        saved = wt.bridge.rooms_version
        wt.bridge.rooms_version = 6
        try:
            win = self.win()
            win._wtRoomsVersion = 5
            self.assertTrue(win.checkWatchTogetherHub())
            self.assertEqual(len(win._shown), 1)
        finally:
            wt.bridge.rooms_version = saved
