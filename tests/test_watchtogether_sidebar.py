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
        win = home_window(FakeItem(watchtogether_section))
        win.sectionChanged = lambda **kw: self.fail('sectionChanged must not run')
        win.checkSectionItem()

    def test_menu_stays_closed(self):
        win = home_window(FakeItem(watchtogether_section))
        original = home.dropdown.showDropdown
        home.dropdown.showDropdown = lambda *a, **k: self.fail('no menu for the WT entry')
        try:
            win.sectionMenu()
        finally:
            home.dropdown.showDropdown = original

    def test_click_opens_watch_together(self):
        win = home_window(FakeItem(watchtogether_section))
        opened = []
        original = wtwin.show
        wtwin.show = lambda: opened.append(1)
        try:
            HomeWindow.sectionClicked(win)
        finally:
            wtwin.show = original
        self.assertEqual(opened, [1])
        self.assertIs(win.lastSection, watchtogether_section)
