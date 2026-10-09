# Watch Together — host capability (v2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a PM4K user create a Watch Together room from a media item, invite friends/home users, and start playback together from a lobby.

**Architecture:** A thin REST layer (`RoomsApi.create`/`invite`), a small people/eligibility helper (`lib/plexpeople.py`), a lobby phase owned by the existing WT bridge, and two Kodi dialog windows (host lobby + invite picker). Readiness reuses the existing `Set{ready}` flow; "start" is just un-pausing the host.

**Tech Stack:** Python 2/3-compatible Kodi addon code, `pytest` via `uv run pytest`, Kodi skin templates.

**Spec:** `docs/superpowers/specs/2026-10-09-watch-together-host-design.md`

## Global Constraints

- Follow the existing addon style: `from __future__ import absolute_import`, `.format()`, no f-strings, no new dependencies.
- Tests run with `uv run pytest` (never bare `pytest`). Baseline before starting: **956 passed** on `feature/watch-together-squash`; the impl branch has more — confirm with `uv run pytest -q`.
- Token hygiene: never log a token value or a `401` response body (the spec's §4 rule).
- Skin content changes require bumping `THEME_VERSION` in `lib/util.py`.
- Templates live in `resources/skins/Main/1080i/templates/*.tpl`; generated `resources/skins/Main/1080i/*.xml` are gitignored artifacts.
- Invite ids sent to the API must be integers (`idRaw`/`userID`), never strings — a non-numeric id makes the server leak a Postgres error (`500`).
- Work on `feature/watch-together-impl`; commits use `SSH_AUTH_SOCK= GPG_TTY= git commit --no-gpg-sign`.
- Test scaffolding: extend the existing fakes — `tests/test_watchtogether_bridge.py`'s `BridgeTestCase` (`FakeSupervisor`/`FakePlayer`/`FakeAPI`/`FakeDialog`) and `tests/test_watchtogether.py`'s fake transport. The snippets below name the intent; match each file's existing construction pattern.

## Review Focus

Five inputs/conditions the spec implies that tests do not cover; each gets a test in the owning task.

1. **Friends fetch fails / returns empty** (community GraphQL down, offline) → the picker must degrade to home users only, never crash or block the lobby.
2. **Invite target is not a friend** (`400`) → that person is marked failed, the room stays, the others are still invited.
3. **An invited member never joins** → auto-start never fires; the manual **Start** still works.
4. **Item with no resolvable `sourceUri`, or on a shared server** → host entry hidden when unresolvable; shared-server rows labelled "access unknown".
5. **Host cancels after guests joined** → host leaves (`DELETE`); the room lingers for the guests, who see the host gone from the roster.

---

### Task 1: `RoomsApi.create` / `invite`

**Files:**
- Modify: `lib/watchtogether.py:128-133` (replace the two `NotImplementedError` stubs)
- Test: `tests/test_watchtogether.py` (add a `RoomsWriteTest`)

**Interfaces:**
- Consumes: existing `self._req(method, path, body)` and `Room`.
- Produces:
  - `RoomsApi.create(source_uri, title, users=None) -> Room` (raises `WatchTogetherError`/`AuthError`)
  - `RoomsApi.invite(room_id, user_ids) -> Room` (raises `ValueError` before any request when an id is not an int)

- [ ] **Step 1: Write the failing tests**

```python
def test_create_posts_source_uri_and_title(self):
    api, calls = self.api_with(201, {"id": "r1", "title": "T"})
    room = api.create("server://m/com.plexapp.plugins.library/library/metadata/1", "T")
    assert room.id == "r1"
    assert calls[0][0] == "POST" and calls[0][1] == "/rooms"
    assert calls[0][2]["sourceUri"].startswith("server://")

def test_invite_posts_user_ids(self):
    api, calls = self.api_with(200, {"id": "r1", "users": []})
    api.invite("r1", [1000002, 1000003])
    assert calls[0][1] == "/rooms/r1/invite"
    assert calls[0][2] == {"users": [1000002, 1000003]}

def test_invite_rejects_non_numeric_ids_without_requesting(self):
    api, calls = self.api_with(200, {"id": "r1"})
    with pytest.raises(ValueError):
        api.invite("r1", ["NaN"])
    assert calls == []          # never reaches the 500-leaking path

def test_create_401_raises_auth_error(self):
    api, _ = self.api_with(401, None)
    with pytest.raises(watchtogether.AuthError):
        api.create("server://x", "T")
```

(`self.api_with(status, payload)` is a small helper over the existing fake transport; follow the file's current pattern.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_watchtogether.py -k "create or invite" -v`
Expected: FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement**

`create` calls `self._req("POST", "/rooms", {"sourceUri": source_uri, "title": title, "users": users})` and returns `Room(payload)`. `invite` first validates `all(isinstance(i, int) for i in user_ids)` (raise `ValueError` otherwise), then `self._req("POST", "/rooms/%s/invite" % room_id, {"users": list(user_ids)})` and returns `Room(payload)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_watchtogether.py -k "create or invite" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/watchtogether.py tests/test_watchtogether.py
git commit -m "feat(watchtogether): implement RoomsApi create and invite"
```

---

### Task 2: `plexpeople.friends` (community GraphQL)

**Files:**
- Create: `lib/plexpeople.py`
- Test: `tests/test_plexpeople.py`

**Interfaces:**
- Produces:
  - `_http(method, url, headers, body=None) -> (status, content_type, body_bytes)` — never raises on HTTP errors
  - `friends(token, http=None) -> list[dict]` where each dict is `{"id": int, "title": str, "thumb": str}` (from `idRaw`/`displayName`/`avatar`); `[]` on any failure

- [ ] **Step 1: Write the failing tests**

```python
def test_friends_parses_graphql(self):
    body = json.dumps({"data": {"allFriendsV2": [
        {"user": {"id": "abc", "idRaw": 1000002, "displayName": "P", "avatar": "http://a"}},
    ]}}).encode()
    out = plexpeople.friends("tok", http=lambda *a, **k: (200, "application/json", body))
    assert out == [{"id": 1000002, "title": "P", "thumb": "http://a"}]

def test_friends_degrades_on_error(self):
    assert plexpeople.friends("tok", http=lambda *a, **k: (500, "text/html", b"")) == []

def test_friends_empty_is_empty(self):
    body = json.dumps({"data": {"allFriendsV2": []}}).encode()
    assert plexpeople.friends("tok", http=lambda *a, **k: (200, "application/json", body)) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plexpeople.py -v`
Expected: FAIL with `ModuleNotFoundError: lib.plexpeople` / `AttributeError`.

- [ ] **Step 3: Implement**

`friends` POSTs to `https://community.plex.tv/api` with JSON `{"query": "query GetAllFriends { allFriendsV2 { user { avatar displayName id idRaw username } createdAt } }", "operationName": "GetAllFriends"}` and headers `Content-Type: application/json`, `Accept: application/json`, `x-plex-token: <token>`, `x-plex-client-identifier: <PM4K CLIENT_ID>`, `x-plex-product`, `x-plex-platform`, `x-plex-version`. Parse `data.allFriendsV2[].user`; skip entries without `idRaw`; return `[]` on non-2xx, parse error, or missing data. `_http` uses stdlib `urllib` (the module must import without Kodi — keep it Kodi-free).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_plexpeople.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/plexpeople.py tests/test_plexpeople.py
git commit -m "feat(watchtogether): fetch plex friends via community GraphQL"
```

---

### Task 3: `plexpeople.shared_users`

**Files:**
- Modify: `lib/plexpeople.py`
- Test: `tests/test_plexpeople.py`

**Interfaces:**
- Produces: `shared_users(token, machine_id, http=None) -> list[dict]` where each dict is `{"id": int, "title": str}` (from `userID`/`username`); `[]` on failure (including the owner-only `404` for shared servers)

- [ ] **Step 1: Write the failing tests**

```python
def test_shared_users_parses_xml(self):
    xml = b'<MediaContainer><SharedServer userID="1000002" username="p"/></MediaContainer>'
    out = plexpeople.shared_users("tok", "mid", http=lambda *a, **k: (200, "application/xml", xml))
    assert out == [{"id": 1000002, "title": "p"}]

def test_shared_users_404_is_empty(self):
    assert plexpeople.shared_users("tok", "mid", http=lambda *a, **k: (404, "application/xml", b"")) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plexpeople.py -k shared -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'shared_users'`.

- [ ] **Step 3: Implement**

GET `https://plex.tv/api/servers/{machine_id}/shared_servers` with `X-Plex-Token`; parse the XML children's `userID`/`username`. Return `[]` on any non-200.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_plexpeople.py -k shared -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/plexpeople.py tests/test_plexpeople.py
git commit -m "feat(watchtogether): list a server's shared users"
```

---

### Task 4: `plexpeople.eligible_invitees`

**Files:**
- Modify: `lib/plexpeople.py`
- Test: `tests/test_plexpeople.py`

**Interfaces:**
- Consumes: `friends`, `shared_users` (above).
- Produces:
  - `Invitee` (namedtuple) `(id, title, thumb, access_unknown)`
  - `eligible_invitees(token, machine_id, owned, home_users, self_id=None, room_user_ids=(), http=None) -> list[Invitee]`
  - `home_users` is a list of `{"id": int, "title": str, "thumb": str}` supplied by the caller (the bridge passes `plexaccount.homeUsers`), so this stays Kodi-free and pure given inputs.

- [ ] **Step 1: Write the failing tests**

```python
def _pp(friends, sharees):
    def http(method, url, headers, body=None):
        if "community" in url:
            return (200, "application/json", json.dumps({"data": {"allFriendsV2":
                [{"user": {"idRaw": i, "displayName": t, "avatar": ""}} for i, t in friends]}}).encode())
        return (200, "application/xml",
                ("<MediaContainer>" + "".join('<SharedServer userID="%d" username="%s"/>' % (i, t) for i, t in sharees) + "</MediaContainer>").encode())
    return http

def test_own_server_intersects_friends_with_sharees(self):
    http = _pp([(1, "a"), (2, "b"), (3, "c")], [(2, "b"), (3, "c"), (4, "d")])
    out = plexpeople.eligible_invitees("tok", "mid", True, [{"id": 5, "title": "h", "thumb": ""}], self_id=9, http=http)
    assert sorted(i.id for i in out) == [2, 3, 5]
    assert all(not i.access_unknown for i in out)

def test_shared_server_offers_all_friends_flagged(self):
    http = _pp([(1, "a"), (2, "b")], [])
    out = plexpeople.eligible_invitees("tok", "mid", False, [{"id": 5, "title": "h", "thumb": ""}], http=http)
    assert sorted(i.id for i in out) == [1, 2, 5]
    assert all(i.access_unknown for i in out)

def test_excludes_self_and_room_members(self):
    http = _pp([(1, "a"), (2, "b")], [(1, "a"), (2, "b")])
    out = plexpeople.eligible_invitees("tok", "mid", True, [], self_id=1, room_user_ids=[2], http=http)
    assert out == []

def test_friends_failure_falls_back_to_home_users(self):
    http = lambda *a, **k: (500, "text/html", b"")
    out = plexpeople.eligible_invitees("tok", "mid", False, [{"id": 5, "title": "h", "thumb": ""}], http=http)
    assert [i.id for i in out] == [5]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plexpeople.py -k eligible -v`
Expected: FAIL (function not defined).

- [ ] **Step 3: Implement**

Own server (`owned=True`): friends whose `id` is in the sharee `userID` set, plus home users. Shared server: all friends plus home users, `access_unknown=True` on every row. Always drop `self_id` and any `room_user_ids`. Deduplicate by id. Preserve friend order, then home users.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_plexpeople.py -k eligible -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/plexpeople.py tests/test_plexpeople.py
git commit -m "feat(watchtogether): compute eligible invitees"
```

---

### Task 5: ready notification + `members_ready`

**Files:**
- Modify: `lib/syncplay.py` (`Session.__init__`, `_on_set`; add module functions)
- Test: `tests/test_syncplay.py`

**Interfaces:**
- Produces:
  - `Session.on_ready` callback attribute, fired as `on_ready(key, is_ready)` whenever `Set{ready}` updates an entry
  - `ready_member_ids(roster) -> set[str]` — userIDs whose entry has `isReady is True`
  - `members_ready(member_ids, roster) -> bool` — every member id present and ready

- [ ] **Step 1: Write the failing tests**

```python
def test_set_ready_fires_on_ready(self):
    seen = []
    s = syncplay.Session("room", "me", on_ready=lambda k, v: seen.append((k, v)))
    s.on_message({"Set": {"ready": {"username": "u", "isReady": True}}})
    assert seen and seen[-1][1] is True

def test_ready_member_ids_and_members_ready(self):
    roster = {
        syncplay.build_identity("d", "n", 1): {"isReady": True},
        syncplay.build_identity("d", "n", 2): {"isReady": False},
    }
    assert syncplay.ready_member_ids(roster) == {"1"}
    assert syncplay.members_ready(["1"], roster) is True
    assert syncplay.members_ready(["1", "2"], roster) is False
    assert syncplay.members_ready(["3"], roster) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_syncplay.py -k "on_ready or members_ready" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`_on_set`, after writing `entry["isReady"]`, calls `self.on_ready(key, ready.get("isReady"))` when set. `ready_member_ids` parses each identity with `strip_identity` and collects `userID` where `isReady is True` (compare as `str`). `members_ready` is `set(map(str, member_ids)) <= ready_member_ids(roster)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_syncplay.py -v`
Expected: PASS (existing syncplay tests still green).

- [ ] **Step 5: Commit**

```bash
git add lib/syncplay.py tests/test_syncplay.py
git commit -m "feat(watchtogether): notify on ready changes; aggregate readiness"
```

---

### Task 6: bridge — lobby rows + readiness wiring

**Files:**
- Modify: `lib/windows/watchtogether.py` (bridge: add `lobby_rows`, `_on_ready`, auto-start check)
- Test: `tests/test_watchtogether_bridge.py`

**Interfaces:**
- Consumes: `syncplay.members_ready`, `syncplay.ready_member_ids`, `RoomsApi` (Task 1).
- Produces:
  - `lobby_rows(room, roster, live_ids) -> list[dict]` each `{"title": str, "thumb": str, "status": "Ready"|"Invited"}`
  - bridge `_maybe_auto_start()` — calls `start_playback()` when every `room.users` id (minus self) is ready and self is ready.

- [ ] **Step 1: Write the failing tests**

```python
def test_lobby_rows_marks_ready_and_invited(self):
    room = FakeRoom(users=[{"id": 1, "title": "A"}, {"id": 2, "title": "B"}])
    roster = {syncplay.build_identity("d", "n", 1): {"isReady": True}}
    rows = bridge.lobby_rows(room, roster, live_ids={1, 2})
    assert [r["status"] for r in rows] == ["Ready", "Invited"]

def test_auto_start_when_all_members_ready(self):
    b = self.bridge(room_users=[1, 2])
    b._player_ready = True
    b.on_ready_for_test(roster={syncplay.build_identity("d","n",1): {"isReady": True},
                                syncplay.build_identity("d","n",2): {"isReady": True}})
    assert b.started is True

def test_auto_start_holds_when_a_member_missing(self):
    b = self.bridge(room_users=[1, 2])
    b._player_ready = True
    b.on_ready_for_test(roster={syncplay.build_identity("d","n",1): {"isReady": True}})
    assert b.started is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_watchtogether_bridge.py -k "lobby_rows or auto_start" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`lobby_rows` mirrors `room_info_rows` (avatar thumb, title); status is `"Ready"` when the user's id is in `ready_member_ids(roster)`, else `"Invited"`. Wire `sup.on_ready` → bridge handler that updates the dialog and calls `_maybe_auto_start`. `_maybe_auto_start` starts only when `members_ready(other_member_ids, roster)` and self is ready.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_watchtogether_bridge.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/windows/watchtogether.py tests/test_watchtogether_bridge.py
git commit -m "feat(watchtogether): lobby rows and auto-start on readiness"
```

---

### Task 7: bridge — host flow

**Files:**
- Modify: `lib/windows/watchtogether.py` (bridge: `host`, `invite`, `start_playback`, cancel)
- Test: `tests/test_watchtogether_bridge.py`

**Interfaces:**
- Consumes: `RoomsApi.create`/`invite`, `plexpeople.eligible_invitees`, `_watch_room` (existing).
- Produces:
  - `host(item) -> None` — create the room, open the item paused, show the lobby
  - `invite(user_ids) -> list[int]` — invite into the current room; returns the ids that failed (`400`)
  - `start_playback() -> None` — unpause self (the room starts)
  - `cancel_hosting() -> None` — leave (`DELETE`) and close

- [ ] **Step 1: Write the failing tests**

```python
def test_host_creates_room_with_item_source_uri(self):
    b = self.bridge()
    b.host(FakeItem(machine="m", rating_key="1", title="T"))
    assert b.api.created[0]["sourceUri"].endswith("/metadata/1")
    assert b.api.created[0]["title"] == "T"

def test_host_opens_item_paused(self):
    b = self.bridge()
    b.host(FakeItem(machine="m", rating_key="1", title="T"))
    assert b.opened_paused is True

def test_invite_reports_failed_targets(self):
    b = self.bridge()
    b.api.invite_result = watchtogether.WatchTogetherError("400")
    assert b.invite([1]) == [1]

def test_start_unpauses(self):
    b = self.bridge(); b.start_playback()
    assert b.player.paused is False

def test_cancel_leaves_without_destroying(self):
    b = self.bridge(); b.cancel_hosting()
    assert b.api.leaves == [b.room_id] and b.closed is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_watchtogether_bridge.py -k "host or invite or start or cancel" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`host`: resolve `machine_id = item.server.uuid`, `rating_key = item.ratingKey`, build `sourceUri = "server://{0}/com.plexapp.plugins.library/library/metadata/{1}".format(machine_id, rating_key)`; `create`; open the item paused (see spec "Host opens paused" — apply the pause from the bridge side once playback starts); show `LobbyDialog`. `invite`: call `api.invite`, collect failed ids on `WatchTogetherError`. `start_playback`: `player.control('play')` + `sup.send_now()`. `cancel_hosting`: `leave()` + close the dialog. Guard `host` when the item has no `rating_key`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_watchtogether_bridge.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/windows/watchtogether.py tests/test_watchtogether_bridge.py
git commit -m "feat(watchtogether): host flow — create, invite, start, cancel"
```

---

### Task 8: `LobbyDialog` (host + read-only guest)

**Files:**
- Create: `resources/skins/Main/1080i/templates/script-plex-watchtogether_lobby.xml.tpl`
- Modify: `lib/windows/watchtogether.py` (`LobbyDialog`), `lib/util.py` (`THEME_VERSION`), `resources/language/resource.language.en_gb/strings.po`
- Test: `tests/test_watchtogether_bridge.py` (rows already covered in Task 6; add a props test)

**Interfaces:**
- Consumes: `lobby_rows`, bridge `start_playback`/`cancel_hosting`/`invite`.
- Produces: `LobbyDialog` with a host mode (Start/Cancel/Invite) and a read-only mode (Leave, "waiting for the host").

- [ ] **Step 1: Write the failing test**

```python
def test_lobby_dialog_read_only_hides_start(self):
    d = watchtogether.LobbyDialog(room=FakeRoom(users=[]), roster={}, live_ids=set(), host=False)
    assert d.is_host is False          # dialog hides Start/Cancel in guest mode
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_watchtogether_bridge.py -k lobby_dialog -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Template: heading, media title, participant list (title + status), an Invite tile, Start/Cancel in host mode, Leave + a "waiting for the host" label in guest mode. Window class mirrors `RoomInfoDialog`/`ParticipantsDialog`; buttons route to the bridge. Bump `THEME_VERSION`; add the new string ids.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_watchtogether_bridge.py -k lobby_dialog -v`
Expected: PASS.

- [ ] **Step 5: Manual check**

Generate the skin, open the lobby from the item menu on Kodi; confirm participants update live and Start begins playback.

- [ ] **Step 6: Commit**

```bash
git add resources/skins/Main/1080i/templates/script-plex-watchtogether_lobby.xml.tpl lib/windows/watchtogether.py lib/util.py resources/language/resource.language.en_gb/strings.po tests/test_watchtogether_bridge.py
git commit -m "feat(watchtogether): host lobby dialog"
```

---

### Task 9: `InviteDialog` (picker)

**Files:**
- Create: `resources/skins/Main/1080i/templates/script-plex-watchtogether_invite.xml.tpl`
- Modify: `lib/windows/watchtogether.py` (`InviteDialog`), `lib/util.py`, `strings.po`
- Test: `tests/test_watchtogether_bridge.py`

**Interfaces:**
- Consumes: `plexpeople.eligible_invitees`, bridge `invite`.
- Produces: `InviteDialog` — multi-select list of eligible invitees; on OK calls `bridge.invite(selected_ids)`.

- [ ] **Step 1: Write the failing test**

```python
def test_invite_dialog_marks_access_unknown(self):
    rows = watchtogether.invite_rows([
        plexpeople.Invitee(1, "a", "", False),
        plexpeople.Invitee(2, "b", "", True),
    ])
    assert rows[1]["access_unknown"] == "1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_watchtogether_bridge.py -k invite_rows -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`invite_rows(invitees)` builds the list-item properties (title, thumb, `access_unknown`). Window mirrors `ParticipantsDialog`; multi-select; OK → `bridge.invite(selected)`. Bump `THEME_VERSION`; add string ids. Fall back to home users only when `friends` returns `[]` (Review Focus 1).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_watchtogether_bridge.py -k invite_rows -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add resources/skins/Main/1080i/templates/script-plex-watchtogether_invite.xml.tpl lib/windows/watchtogether.py lib/util.py resources/language/resource.language.en_gb/strings.po tests/test_watchtogether_bridge.py
git commit -m "feat(watchtogether): invite picker dialog"
```

---

### Task 10: home.py entries

**Files:**
- Modify: `lib/windows/home.py` (`hubMenu`, `_watchtogether_hub_menu`)
- Test: `tests/test_watchtogether_home.py`

**Interfaces:**
- Consumes: bridge `host`/`invite`.
- Produces: "Start Watch Together" on movie/episode items with a resolvable `sourceUri`; "Invite…" on a room tile.

- [ ] **Step 1: Write the failing tests**

```python
def test_start_watch_together_shown_for_movie(self):
    opts = self.menu_options(ds=Movie(machine="m", rating_key="1"))
    assert any(o["key"] == "start_watch_together" for o in opts)

def test_start_watch_together_hidden_without_source(self):
    opts = self.menu_options(ds=Movie(machine=None, rating_key=None))
    assert not any(o["key"] == "start_watch_together" for o in opts)

def test_room_tile_menu_has_invite(self):
    opts = self.room_menu()
    assert any(o["key"] == "invite" for o in opts)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_watchtogether_home.py -k "start_watch or invite" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add `{'key': 'start_watch_together', 'display': T(..., 'Start Watch Together')}` in `hubMenu` for `ds.TYPE in ('movie', 'episode')` when `ds.server` and `ds.ratingKey` exist; handle it by calling `bridge.host(ds)`. Add `{'key': 'invite', ...}` to `_watchtogether_hub_menu` → `InviteDialog`. (Review Focus 4: hidden when unresolvable.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_watchtogether_home.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/windows/home.py tests/test_watchtogether_home.py
git commit -m "feat(watchtogether): host/invite entries in the item and room menus"
```

---

### Task 11: guest lobby

**Files:**
- Modify: `lib/windows/watchtogether.py` (bridge join path + `LobbyDialog` read-only mode)
- Test: `tests/test_watchtogether_bridge.py`

**Interfaces:**
- Consumes: `LobbyDialog(host=False)`, `lobby_rows`.
- Produces: when a guest joins a room whose relay `State` is paused with `position < 1`, show the read-only lobby; close it when playback starts.

- [ ] **Step 1: Write the failing tests**

```python
def test_guest_shows_lobby_for_unstarted_room(self):
    b = self.bridge(); b.on_state({"position": 0.0, "paused": True, "doSeek": False})
    assert b.lobby_shown is True

def test_guest_no_lobby_for_playing_room(self):
    b = self.bridge(); b.on_state({"position": 120.0, "paused": False, "doSeek": False})
    assert b.lobby_shown is False

def test_guest_closes_lobby_when_playback_starts(self):
    b = self.bridge(); b.on_state({"position": 0.0, "paused": True, "doSeek": False})
    b.on_state({"position": 1.5, "paused": False, "doSeek": False})
    assert b.lobby_shown is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_watchtogether_bridge.py -k guest -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In the bridge's state handling, if not hosting and the room is unstarted (`paused` and `position < 1`), show `LobbyDialog(host=False)`; close it on the first state with `paused is False`. Leave = `leave()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_watchtogether_bridge.py -v`
Expected: PASS.

- [ ] **Step 5: Full suite**

Run: `uv run pytest -q`
Expected: green, with the new tests included.

- [ ] **Step 6: Commit**

```bash
git add lib/windows/watchtogether.py tests/test_watchtogether_bridge.py
git commit -m "feat(watchtogether): read-only guest lobby"
```

---

## Self-Review

**Spec coverage:** Goal/flows → Tasks 7, 8, 11; `create`/`invite` → Task 1; friends/home-users/sharees + eligibility → Tasks 2–4; readiness/auto-start → Tasks 5–6; error handling → Tasks 7 (failed invites, cancel), 9 (friends fallback), 10 (hidden entry); dialogs → Tasks 8–9; home entries → Task 10; guest lobby → Task 11; testing → every task. Host-opens-paused is resolved in Task 7; guest heuristic in Task 11.

**Review Focus mapping:** 1 → Task 9 (friends `[]` fallback) + Task 4 (`friends_failure_falls_back_to_home_users`); 2 → Task 7 (`invite_reports_failed_targets`); 3 → Task 6 (`auto_start_holds_when_a_member_missing`); 4 → Task 10 (hidden) + Task 4 (`access_unknown`); 5 → Task 7 (`cancel_leaves_without_destroying`).

**Type consistency:** `idRaw`/`userID` are ints throughout; `Invitee(id, title, thumb, access_unknown)` is produced in Task 4 and consumed in Task 9; `members_ready`/`ready_member_ids` defined in Task 5 and used in Task 6; `lobby_rows` defined in Task 6 and consumed in Task 8/11.
