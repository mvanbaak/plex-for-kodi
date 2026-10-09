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
