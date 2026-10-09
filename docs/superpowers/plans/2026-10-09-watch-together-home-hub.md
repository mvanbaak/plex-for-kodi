# Watch Together Home Hub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show active Plex Watch Together rooms as the first Home hub, with the media's 16:9 art, the room title, and the participant names; clicking joins/switches/opens participants.

**Architecture:** A client-built `plexlibrary.BaseHub` (modelled on `CollectionsHub`) is built by the Watch Together bridge from its `rooms_cache` and prepended in `HomeWindow._showHubs` so it renders first; a custom list-item creator and a `hubItemClicked` interception render and route the tiles. Art is resolved from each room's `sourceUri` on the bridge's background poll thread and cached.

**Tech Stack:** Python (Kodi addon), `plexnet` (`plexlibrary`, `plexobjects`), the repo's `unittest`-based pytest suite run with `uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-10-09-watch-together-home-hub-design.md`

## Global Constraints

- Tests run with `uv run pytest`; the suite must stay green. Never `pip install`.
- The generated skin dir `resources/skins/Main/1080i/*.xml` is gitignored; skin sources are the `.xml.tpl` files under `resources/skins/Main/1080i/templates/`.
- Commits use `SSH_AUTH_SOCK= GPG_TTY= git commit --no-gpg-sign -m "..."` (GPG signing fails in this environment).
- Room ids are credentials: never log them at debug (log `exc.__class__.__name__`).
- The hub is **always on**; do not add a setting for it.
- New en_gb string id `35061` (switch-room confirmation); `35060` (takeover confirmation) already exists.
- `lib/watchtogether.py` must keep importing no `xbmc` (enforced by `tests/test_protocol_isolation.py`).

## Review Focus

Inputs/conditions the spec implies but its own tests may not cover; each has a test in its owning task:

1. A user **custom hub config** exists for Home — the WT hub must still render first (`isHubHidden` bypass). Task 3.
2. Every room's art fails to resolve — the hub still renders with the placeholder, no crash. Task 2.
3. The room list **empties** while Home is shown — the hub disappears (0-item skip). Task 1.
4. `home_hub()` is called before the first poll / bridge never started — `rooms_cache` empty → `None`. Task 1.
5. Participant names are long/unicode/absent — line 2 falls back to "N watching", no crash. Task 1.

---

### Task 1: Bridge room items and hub

**Files:**
- Modify: `lib/windows/watchtogether.py`
- Test: `tests/test_watchtogether_hub.py` (create)

**Interfaces:**
- Consumes: `watchtogether.Room` (`.id`, `.title`, `.participants`, `.source_uri`), `util.T`, `plexlibrary.BaseHub`.
- Produces:
  - `WATCHTOGETHER_HUB_ID = "watchtogether.rooms"`
  - `WATCHTOGETHER_PLACEHOLDER = "script.plex/thumb_fallbacks/movie16x9.png"`
  - `participant_names(users) -> str`
  - `class WatchTogetherRoomItem` — `WatchTogetherRoomItem(room, image=None)`; attrs `.type == "watchtogether"`, `.title`, `.subtitle`, `.image`, `.room`, `.cachable == False`; method `.get(key, default=None) -> default`
  - `class WatchTogetherRoomsHub(plexlibrary.BaseHub)` — `WatchTogetherRoomsHub(factory)` where `factory() -> list`; attrs `.hubIdentifier == WATCHTOGETHER_HUB_ID`, `.title`, `.items`; methods `.getCleanHubIdentifier(is_home=False) -> str`, `.reset()`, `.reload(**kwargs) -> self`
  - `WatchTogetherBridge.rooms_version: int`, `.room_art: dict`, `.home_hub() -> WatchTogetherRoomsHub | None`, `._build_room_items() -> list`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_watchtogether_hub.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_watchtogether_hub.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'participant_names'` (and the other names).

- [ ] **Step 3: Implement in `lib/windows/watchtogether.py`**

Add `plexlibrary` to the plexnet import: `from plexnet import plexapp, plexlibrary, plexobjects`.

Add the module constants and helpers near `parse_source_uri`:

```python
WATCHTOGETHER_HUB_ID = "watchtogether.rooms"
WATCHTOGETHER_PLACEHOLDER = "script.plex/thumb_fallbacks/movie16x9.png"


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
```

In `WatchTogetherBridge.__init__` add: `self.rooms_version = 0`, `self.room_art = {}`, `self._rooms_key = None`.

In `_poll_rooms`, right after `self.rooms_cache = rooms`, bump the version:

```python
        key = tuple(sorted((r.id, len(r.participants)) for r in rooms))
        if key != self._rooms_key:
            self._rooms_key = key
            self.rooms_version += 1
```

Add the bridge methods:

```python
    def _build_room_items(self):
        return [WatchTogetherRoomItem(r, self.room_art.get(r.id))
                for r in self.rooms_cache]

    def home_hub(self):
        if not self.rooms_cache:
            return None
        return WatchTogetherRoomsHub(self._build_room_items)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_watchtogether_hub.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/windows/watchtogether.py tests/test_watchtogether_hub.py
git commit -m "feat(watchtogether): room items and home hub model"
```

---

### Task 2: Bridge art resolution

**Files:**
- Modify: `lib/windows/watchtogether.py`
- Test: `tests/test_watchtogether_hub.py`

**Interfaces:**
- Consumes: `parse_source_uri`, `plexobjects.listItems`, `plexapp.SERVERMANAGER.serversByUuid`, `WATCHTOGETHER_PLACEHOLDER`.
- Produces: `WatchTogetherBridge._resolve_room_art(room) -> str`; `_poll_rooms` fills/prunes `self.room_art`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_watchtogether_hub.py`)

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_watchtogether_hub.py -k ArtResolve -q`
Expected: FAIL — `AttributeError: ... has no attribute '_resolve_room_art'`.

- [ ] **Step 3: Implement `_resolve_room_art` and wire it into `_poll_rooms`**

```python
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
```

In `_poll_rooms`, after the version bump block, resolve once per room and prune:

```python
        live = set()
        for room in rooms:
            live.add(room.id)
            if room.id not in self.room_art:
                self.room_art[room.id] = self._resolve_room_art(room)
        for room_id in list(self.room_art):
            if room_id not in live:
                del self.room_art[room_id]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_watchtogether_hub.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/windows/watchtogether.py tests/test_watchtogether_hub.py
git commit -m "feat(watchtogether): resolve room 16:9 art on the poll thread"
```

---

### Task 3: Home rendering — display flags, creator, injection

**Files:**
- Modify: `lib/windows/home.py`
- Test: `tests/test_watchtogether_home.py` (create)

**Interfaces:**
- Consumes: `wtwin.WatchTogetherRoomItem`, `wtwin.bridge.home_hub()`, `wtwin.WATCHTOGETHER_HUB_ID`, `HubsList`, `NO_HUB`.
- Produces:
  - `HomeWindow.getHubDisplayType` returns `'ar16x9'` for the WT identifier.
  - `HomeWindow.getHubRenderFlags` returns `{'with_progress': False, 'do_updates': True, 'text2lines': True, 'ar16x9': True, 'with_art': False}` for it.
  - `HomeWindow.createWatchTogetherListItem(obj, wide=False) -> ManagedListItem`
  - `HomeWindow._with_watchtogether_hub(hubs, section) -> HubsList`
  - `CREATE_LI_MAP['watchtogether']`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_watchtogether_home.py
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
                                  "users": [{"title": "A"}]})
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_watchtogether_home.py -q`
Expected: FAIL — `AttributeError: 'HomeWindow' object has no attribute '_with_watchtogether_hub'`.

- [ ] **Step 3: Implement in `lib/windows/home.py`**

Add a lazy import helper at the top of the two flag methods. In `getHubDisplayType`, before the `HUBS_MIXED_CONTENT` check:

```python
        from . import watchtogether as wtwin
        if identifier == wtwin.WATCHTOGETHER_HUB_ID:
            return 'ar16x9'
```

In `getHubRenderFlags`, right after the default `flags` dict:

```python
        from . import watchtogether as wtwin
        if identifier == wtwin.WATCHTOGETHER_HUB_ID:
            return {'with_progress': False, 'do_updates': True,
                    'text2lines': True, 'ar16x9': True, 'with_art': False}
```

Add the creator next to the other `create*ListItem` methods and register it:

```python
    def createWatchTogetherListItem(self, obj, wide=False):
        mli = kodigui.ManagedListItem(obj.title, obj.subtitle,
                                      thumbnailImage=obj.image, data_source=obj)
        mli.setProperty('thumb.fallback', 'script.plex/thumb_fallbacks/movie16x9.png')
        return mli
```

`CREATE_LI_MAP['watchtogether'] = createWatchTogetherListItem` (add the key to the dict literal).

Add the injection helper and call it in `_showHubs`:

```python
    def _with_watchtogether_hub(self, hubs, section):
        """Prepend the Watch Together hub on Home. Built fresh each draw so a
        custom hub config can neither drop nor reorder it; the hub object is
        shared, so item states still stick."""
        if section.key is not None or hubs is None:
            return hubs
        from . import watchtogether as wtwin
        hub = wtwin.bridge.home_hub()
        if hub is None:
            return hubs
        combined = HubsList([hub] + list(hubs))
        combined.identifier = getattr(hubs, 'identifier', NO_HUB)
        combined.lastUpdated = getattr(hubs, 'lastUpdated', 0)
        combined.invalid = getattr(hubs, 'invalid', False)
        return combined
```

> Note: `_showHubs` early-returns when the selected server exposes no hubs at all, so the WT hub cannot appear on such a server. That matches the official client (a Home with no hubs has no rows); no workaround is planned.

In `_showHubs`, immediately after the `combined_hubs` block (after `hubs = combined_hubs`):

```python
        hubs = self._with_watchtogether_hub(hubs, section)
```

And bypass the hidden check for the WT hub (the loop at `if not is_cross_section:`):

```python
        from . import watchtogether as wtwin
        if not is_cross_section and identifier != wtwin.WATCHTOGETHER_HUB_ID:
            if self.isHubHidden(identifier, section.key):
                hidden_count += 1
                continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_watchtogether_home.py -q`
Expected: PASS.

- [ ] **Step 5: Run the template tests** (the hub injects no template change; must stay green)

Run: `uv run pytest tests/test_templates.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lib/windows/home.py tests/test_watchtogether_home.py
git commit -m "feat(watchtogether): render the rooms hub on Home"
```

---

### Task 4: Home click route, refresh, and join/switch/participants

**Files:**
- Modify: `lib/windows/home.py`, `lib/windows/watchtogether.py`
- Test: `tests/test_watchtogether_home.py`, `tests/test_watchtogether_hub.py`

**Interfaces:**
- Consumes: `wtwin.WatchTogetherRoomItem`, `wtwin.needs_takeover_confirm`, `ParticipantsDialog`, `xbmcgui.Dialog().yesno`.
- Produces:
  - `lib/windows/watchtogether.py`: `playing_rating_key() -> str`, `confirm_takeover(room) -> bool`, `confirm_switch() -> bool`, `WatchTogetherBridge.room_clicked(room)`.
  - `lib/windows/home.py`: `HomeWindow.hubItemClicked` intercepts WT items; `HomeWindow.checkWatchTogetherHub() -> bool`; `HomeWindow._wtRoomsVersion`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_watchtogether_hub.py`:

```python
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
```

Append to `tests/test_watchtogether_home.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_watchtogether_hub.py tests/test_watchtogether_home.py -q`
Expected: FAIL — `AttributeError: ... 'room_clicked'`.

- [ ] **Step 3: Implement**

In `lib/windows/watchtogether.py`, add the module confirm helpers (move `playing_rating_key` out of `RoomPickerDialog`) and the bridge method:

```python
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
```

On `WatchTogetherBridge`:

```python
    def room_clicked(self, room):
        """Hub tile click: join, switch, or open participants for this room."""
        sup = self.supervisor
        if sup is not None and self.room is not None and self.room.id == room.id:
            ParticipantsDialog.open()
            return
        if sup is not None:
            if not confirm_switch():
                return
            self.leave()
        elif not confirm_takeover(room):
            return
        self.join(room.id)
```

In `lib/windows/home.py`, add the item check, the tick refresh, and the click intercept:

```python
    def _isWatchTogetherItem(self, ds):
        from . import watchtogether as wtwin
        return isinstance(ds, wtwin.WatchTogetherRoomItem)
```

In `hubItemClicked`, immediately after the `if mli.dataSource is None: return` check:

```python
        if self._isWatchTogetherItem(mli.dataSource):
            from . import watchtogether as wtwin
            wtwin.bridge.room_clicked(mli.dataSource.room)
            return
```

Add `self._wtRoomsVersion = None` to `HomeWindow.__init__` and:

```python
    def checkWatchTogetherHub(self):
        """Redraw Home when the Watch Together room set changed. Returns True
        when it redrew."""
        from . import watchtogether as wtwin
        version = wtwin.bridge.rooms_version
        if version == self._wtRoomsVersion:
            return False
        self._wtRoomsVersion = version
        self.showHubs(home_section, update=True,
                      reselect_pos_dict=self.getCurrentHubsPositions(home_section))
        return True
```

In `tick`, after the `if not self.lastSection or self._ignoreTick: return` guard:

```python
        if self.is_active and self.lastSection is home_section:
            self.checkWatchTogetherHub()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_watchtogether_hub.py tests/test_watchtogether_home.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/windows/home.py lib/windows/watchtogether.py tests/test_watchtogether_hub.py tests/test_watchtogether_home.py
git commit -m "feat(watchtogether): hub click join/switch/participants and Home refresh"
```

---

### Task 5: Retire the picker and add the switch string

**Files:**
- Modify: `lib/windows/watchtogether.py`, `resources/language/resource.language.en_gb/strings.po`
- Delete: `resources/skins/Main/1080i/templates/script-plex-watchtogether_room_picker.xml.tpl`
- Test: `tests/test_watchtogether_hub.py`, `tests/test_i18n.py`, `tests/test_templates.py`

**Interfaces:**
- Consumes: `ParticipantsDialog`, `bridge.start()`.
- Produces: `show()` opens `ParticipantsDialog` when in a room, else no-op; `RoomPickerDialog` and its template are gone.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_watchtogether_hub.py`)

```python
class ShowEntryTest(KodiTestCase):
    def setUp(self):
        super(ShowEntryTest, self).setUp()
        self.opened = []
        self._saved = wtwin.ParticipantsDialog.open
        wtwin.ParticipantsDialog.open = lambda: self.opened.append(1)
        self._saved_start = wtwin.bridge.start
        wtwin.bridge.start = lambda: None

    def tearDown(self):
        wtwin.ParticipantsDialog.open = self._saved
        wtwin.bridge.start = self._saved_start
        super(ShowEntryTest, self).tearDown()

    def test_no_room_does_nothing(self):
        wtwin.bridge.supervisor = None
        wtwin.show()
        self.assertEqual(self.opened, [])

    def test_in_room_opens_participants(self):
        wtwin.bridge.supervisor = object()
        wtwin.show()
        self.assertEqual(self.opened, [1])
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_watchtogether_hub.py -k ShowEntry -q`
Expected: FAIL — `show()` currently opens `RoomPickerDialog` when there is no supervisor, so `test_no_room_does_nothing` fails.

- [ ] **Step 3: Implement**

Delete the whole `class RoomPickerDialog(...)` block in `lib/windows/watchtogether.py`, and delete `resources/skins/Main/1080i/templates/script-plex-watchtogether_room_picker.xml.tpl`.

Replace `show()` with:

```python
def show():
    """Sidebar entry: participants/leave when in a room; rooms are discovered
    on the Home hub, so there is nothing to do otherwise."""
    bridge.start()
    if bridge.supervisor:
        window = ParticipantsDialog.open()
        del window
        util.garbageCollect()
```

Add to `resources/language/resource.language.en_gb/strings.po` after `#35060`:

```
msgctxt "#35061"
msgid "Leave the current room and join this one?"
msgstr ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_watchtogether_hub.py tests/test_i18n.py tests/test_templates.py -q`
Expected: PASS (the template inventory tests reconcile the removed template and its reference).

- [ ] **Step 5: Commit**

```bash
git add lib/windows/watchtogether.py resources/language/resource.language.en_gb/strings.po
git rm resources/skins/Main/1080i/templates/script-plex-watchtogether_room_picker.xml.tpl
git commit -m "feat(watchtogether): retire the room picker; rooms live on Home"
```

---

### Task 6: Exit verification

**Files:** none (verification only).

- [ ] **Step 1: Full suite**

Run: `uv run pytest -q`
Expected: all green (baseline grows by the new hub/home tests; nothing regresses).

- [ ] **Step 2: Isolation guard**

Run: `uv run pytest tests/test_protocol_isolation.py -q`
Expected: `1 passed` — `lib/watchtogether.py` still imports no `xbmc`.

- [ ] **Step 3: Squash-tree check**

Run:
```bash
git diff --name-only feature/watch-together-squash feature/watch-together-impl | grep -v '^docs/superpowers/' || echo "only superpowers differs"
```
Expected: all Phase 2 paths differ (the squash branch is still Phase 1); no unexpected path. This becomes the `.gitignore`-only check after the Phase 2 squash.

- [ ] **Step 4: Manual/hardware checklist (two accounts)**

- Home shows the "Watch Together" hub **first** when a room is active; hidden when there are none.
- Tiles show the episode/movie 16:9 art (placeholder when the source server is unavailable), the room title, and participant names.
- No room + other media playing → click prompts "take over playback?"; confirm joins.
- In a room → the room's tile opens participants/leave; another tile prompts to switch and then joins it.
- Sidebar entry: in a room → participants; otherwise no-op.
- With a custom Home hub config, the WT hub still appears first.

---

## Self-review

**Spec coverage:** always-on hub (Tasks 1, 3), 16:9 art + placeholder (Task 2), item/participant names (Task 1), first-position injection (Task 3), click join/switch/participants (Task 4), tick refresh (Task 4), picker retired + string (Task 5), no extra polling (no task — by design), error handling (Tasks 1, 2, 4), testing (Tasks 1–5), verification (Task 6).

**Type consistency:** `WATCHTOGETHER_HUB_ID`, `WatchTogetherRoomItem`, `WatchTogetherRoomsHub`, `home_hub()`, `room_clicked()`, `confirm_takeover()`, `confirm_switch()`, `checkWatchTogetherHub()`, `_with_watchtogether_hub()` are used with the same names/signatures in every task that references them.

**Review Focus:** five lines above, each pinned to a test in its owning task (Tasks 1, 2, 3).

**Proportion:** task bodies give signatures, values, and hook lines, not full transcriptions.
