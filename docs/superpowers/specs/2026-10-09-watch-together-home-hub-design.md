# Watch Together for PM4K — Home hub design

Date: 2026-10-09
Branch: `feature/watch-together-impl`
Protocol reference: `docs/watch-together.md` (authoritative; §numbers cited below)

## Goal

Surface active Plex Watch Together rooms as the **first Home hub**, mirroring the
official Plex client. When the logged-in account has active rooms, a "Watch
Together" row appears at the top of Home whose tiles show the room's
episode/movie art with the room title and participant names underneath. When
there are none, the hub is not shown at all. Joining and switching rooms happens
from the hub; the room-picker dialog is retired.

## Context / constraints

- **The room record has no media art** (§4): `id`, `title`, `sourceUri`
  (`server://<machineIdentifier>/…/library/metadata/<ratingKey>`), `source` (a
  verbatim duplicate), `users[]` (`id`, `username`, `title`, `uuid`, `thumb`),
  `createdBy`, `startsAt`/`updatedAt`/`endsAt`, `syncplayHost`/`syncplayPort`.
  The tile art must be resolved from `sourceUri` against the **source** server.
- Home hubs are drawn from `self.sectionHubs[section.key]`; a client-built hub
  can be injected. `_showHubs` **skips hubs with no items** and assigns slots
  sequentially, so a prepended hub with items takes slot 0 and an empty one is
  simply skipped.
- The Home template already renders a per-hub title from the `hub.4XX` window
  property and hides a hub whose container is empty — **no template change is
  needed**.
- Hub items must map to a creator in `CREATE_LI_MAP` and survive `_showHub`'s
  `updateBackgroundFrom` / item-state reads, which touch `obj.get(...)`,
  `obj.type` and `obj.cachable`.
- The bridge already polls `GET /rooms` (lobby) on its 1 s thread while no
  supervisor runs, storing `rooms_cache`; it already parses `sourceUri` for
  join-playback (`parse_source_uri`).

## Decisions

- **Always on.** No setting: the hub is injected whenever there are rooms. This
  matches the official client. (`watchtogether.enable_sidebar` still gates the
  sidebar entry.)
- **Client-built hub, prepended in `_showHubs`.** Not added to `sectionHubs`, so
  a user's custom hub config cannot drop or reorder it.
- **16:9 art.** Resolve the item from `sourceUri`: episode → `defaultThumb`,
  otherwise `defaultArt` (falling back to `defaultThumb`). Resolve on the
  bridge's background poll thread and cache by room id; the hub never does
  network I/O on the UI thread. Unresolved → the bundled 16:9 fallback
  `script.plex/thumb_fallbacks/movie16x9.png`.
- **Picker retired.** Delete `RoomPickerDialog` and its template. The sidebar
  entry stays, for in-room participants/leave only.
- **No extra polling.** Keep the existing lobby poll (runs while no supervisor —
  the normal Home case). The hub may be briefly stale right after leaving a
  room; accepted.
- **Refresh via a version counter.** Home's `tick` compares
  `bridge.rooms_version` and redraws the home hubs when it changes.

## Components

### 1. Bridge — `lib/windows/watchtogether.py`

- `rooms_version` (int): bumped in `_poll_rooms` when the room id set or
  participant counts change.
- Art cache `room.id -> url` plus `_resolve_room_art(room)`:
  `parse_source_uri(room.source_uri)` → source server
  (`plexapp.SERVERMANAGER.serversByUuid.get(machine)`) →
  `plexobjects.listItems(server, '/library/metadata/<key>')` → 16:9 URL from the
  item. Failure (no server, no access, no item) caches the placeholder. Runs on
  the poll thread inside `_poll_rooms`, never the UI thread.
- `home_hub()` → a `WatchTogetherRoomsHub` built from `rooms_cache`, or `None`
  when empty.
- `room_clicked(room)`:
  - no supervisor → existing takeover-confirm → `join(room.id)`;
  - already in this room → open `ParticipantsDialog`;
  - in a different room → confirm switch → `leave()` then `join(room.id)`.
- Retire `RoomPickerDialog`; `show()` opens `ParticipantsDialog` when in a room,
  otherwise no-ops.
- `WatchTogetherRoomsHub(plexlibrary.BaseHub)`, modelled on `CollectionsHub`:
  `hubIdentifier='watchtogether.rooms'`, `title=T(35053, 'Watch Together')`,
  `getCleanHubIdentifier()` returns it verbatim, `reload()` rebuilds `items`
  from `rooms_cache`.
- `WatchTogetherRoomItem`: `type='watchtogether'`, `title` (room title),
  `subtitle` (participant names), `image` (cached URL or placeholder), `room`,
  `cachable=False`, and `get(key, default=None)` returning `default` so
  `updateBackgroundFrom`/state reads are inert.

### 2. Home — `lib/windows/home.py`

- `_showHubs`: after `getCombinedHubsForSection`, if `section.key is None` and
  `wtwin.home_hub()` is non-empty, build a fresh `HubsList([hub] + list(hubs))`
  preserving `.identifier`/`.lastUpdated`/`.invalid`, and use it as `hubs`.
- `getHubDisplayType`: `'watchtogether.rooms'` → `'ar16x9'`.
- `getHubRenderFlags`: `'watchtogether.rooms'` →
  `ar16x9=True, with_art=False, with_progress=False, text2lines=True` (so no
  `.art` access and no progress image).
- `CREATE_LI_MAP['watchtogether'] = createWatchTogetherListItem`:
  `ManagedListItem(item.title, item.subtitle, thumbnailImage=item.image,
  data_source=item)`, with `thumb.fallback` set to the 16:9 placeholder.
- `hubItemClicked`: if `mli.dataSource` is a `WatchTogetherRoomItem`, call
  `wtwin.room_clicked(item.room)` and return (bypass `opener.open`).
- `tick`: if `wtwin.bridge.rooms_version != self._wtRoomsVersion` and
  `lastSection is home_section`, update the stored version and
  `showHubs(home_section, update=True,
  reselect_pos_dict=self.getCurrentHubsPositions(home_section))`.

### 3. Participant names

Join `users[].title` (fallback `username`): two → "A and B"; more → "A, B and C".
Kodi truncates the label as needed. Empty → fall back to `T(35054, '{} watching')`.

## Strings

- Reuse `35053` ("Watch Together") for the hub title and `35054` for the
  participant-count fallback.
- Add one en_gb string for the switch confirmation, e.g. `35061`
  ("Leave the current room and join this one?"). The takeover confirm reuses
  `35060`.

## Files

- `lib/windows/watchtogether.py` — hub, item, art cache, `rooms_version`,
  `room_clicked`, retire the picker.
- `lib/windows/home.py` — injection, display flags, creator, click route, tick
  refresh.
- Delete `resources/skins/Main/1080i/templates/script-plex-watchtogether_room_picker.xml.tpl`.
- `resources/language/resource.language.en_gb/strings.po` — new string.
- Tests: `tests/test_watchtogether_bridge.py` (hub/item/room_clicked/art),
  `tests/test_watchtogether_sidebar.py` (unchanged), template tests.

## Error handling

- No rooms → `home_hub()` returns `None`; nothing injected.
- Art unresolved → placeholder; the resolver never raises on the poll thread.
- Source server/item unavailable → placeholder.
- Join/switch failure → the existing notification path.

## Testing

- Unit: `WatchTogetherRoomsHub` built from a fake `rooms_cache` (item count,
  title, identifier); `WatchTogetherRoomItem` fields; participant-name joining
  (0/1/2/3 users); `home_hub()` empty → `None`; `room_clicked` routing with a
  fake bridge (join / switch / participants); art resolver returns the
  placeholder on failure and a URL on success.
- Template inventory: removing the picker template and its `xmlFile` reference
  keeps both directions of the reference test consistent.
- Manual/hardware: hub renders first with 16:9 art, hides when there are no
  rooms, click joins / confirms a switch / opens participants.

## Out of scope

- A Watch Together glyph next to the hub title (would need a template tweak).
- Host capability (create/invite) — v2.
- Polling the lobby while a supervisor is active.
- Changing or removing the sidebar entry.

## Verification

- `uv run pytest -q` stays green (baseline grows by the new tests).
- Squash-tree check unchanged: only `docs/superpowers/` may differ.
