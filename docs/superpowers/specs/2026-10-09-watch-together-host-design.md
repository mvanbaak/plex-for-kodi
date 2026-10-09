# Watch Together — host capability (v2) design

Adds the "host" half of Watch Together to PM4K: create a room from a media
item, invite friends/home users, wait in a lobby until everyone is ready, then
start playback together. v1 shipped guest-only; this is the deferred v2 slice
called out in `2026-10-06-watch-together-design.md` ("`createRoom` + invite
from PM4K, media-item context entry").

Protocol references are to `docs/watch-together.md`: §4 (create/invite REST),
§5.3 (`List` roster + `isReady`), §5.4 (`Set{ready}`), §6.4 (ready/lobby),
§11.9 (invite relationship gate).

## Goal

From Kodi, pick a movie/episode → create a WT room → invite people → watch in
sync, without the Plex web app. Success: a room started from PM4K is visible to
the people invited and they can join from any Plex client, including PM4K.

## Scope

**In scope (v2 host):**

- `POST /rooms` and `POST /rooms/{id}/invite` from PM4K.
- A **Start Watch Together** entry on a movie/episode context menu.
- A **host lobby** dialog: media title, participants with Ready/Invited
  status, an Invite action, Start / Cancel.
- An **invite picker** over plex.tv friends + Plex Home users.
- **Auto-start when every invited member is ready**, with a manual Start.
- A **guest lobby** (read-only variant of the same dialog).
- Inviting more people later from the room's hub-tile menu.

**Explicit non-goals (unchanged from v1):** shared playlists, ad-break sync,
chat, a share URL (none exists — §11.9), pubsub (keep the 15–20 s poll),
`controller` (dead field), host transfer, renaming rooms.

## Decisions (from brainstorming)

1. Hosting starts from the **media item context menu**; create + picker happen
   together, then the lobby; more invites are possible later from the room tile
   menu.
2. The lobby is a **simpler dialog** (not the official full media-detail page):
   media title + participant list + Invite + Start/Cancel.
3. The invite picker lists **friends + home users, no search**, excluding self
   and anyone already in the room.
4. **Eligibility:** own servers → home users + friends who are on that server's
   share list; **shared servers → all friends + home users, each labelled
   "access unknown"** (a sharee cannot enumerate a server they don't own).
5. **Auto-start when all invited members are ready**; the host can force Start.
6. The **host loads the item paused and is ready once buffered** (same rule as
   guests), so the whole room un-pauses together with no post-start stall.
7. Guests see the **same lobby dialog, read-only**.

## Flows

### Host

1. Item context menu (`hubMenu`, movie/episode) → **Start Watch Together**.
2. Build the item's `sourceUri` (`server://<machineId>/…/metadata/<ratingKey>`)
   and `POST /rooms` with `{sourceUri, title: item title}`.
3. Open the item **paused at 0** and show **LobbyDialog** over it.
4. Invite via the picker (`POST /rooms/{id}/invite {users:[id,…]}`).
5. Host is ready once buffered; guests likewise. **Auto-start when every
   invited member is ready**; **Start** forces it early.
6. Start = unpause self. The room's `State` flips to playing; guests unpause
   through the existing `_apply_remote`; the lobby closes.
7. **Cancel** = `DELETE` (leave) + close.

### Guest

1. Room appears on the Home hub (existing).
2. Click → join → open the item paused at the room's position (0 in a lobby).
3. While the room is unstarted, show the **same LobbyDialog read-only**: media
   title, participants, "waiting for the host", **Leave**.
4. Guest is ready once buffered.
5. When the room starts, close the lobby and play.

## Architecture

Option A from brainstorming: a bridge-managed lobby phase plus one small,
testable host helper.

| Unit | Responsibility | Depends on |
|---|---|---|
| `lib/watchtogether.py` — `RoomsApi.create` / `invite` | REST writes; return `Room` | injected transport (existing) |
| `lib/plexpeople.py` (new) | fetch plex.tv friends + a server's shared users; compute the eligible-invitee set | injectable plex.tv fetch; `homeUsers` |
| `lib/syncplay.py` | add a **ready/roster notification** (today `_on_set` updates `roster` silently) | – |
| `lib/windows/watchtogether.py` | bridge host flow `host(item)`, `LobbyDialog`, `InviteDialog`, readiness aggregation | the above + `videoplayer` |
| `lib/windows/home.py` | "Start Watch Together" in `hubMenu`; "Invite…" in the room tile menu | bridge |
| skin templates + strings | `script-plex-watchtogether_lobby.xml.tpl`, `script-plex-watchtogether_invite.xml.tpl`, new string ids, `THEME_VERSION` bump | – |

Each unit answers one question: `RoomsApi` — "how do I write to the cloud?";
`plexpeople` — "who may I invite, and can they play it?"; the bridge — "what is
the room doing?"; the dialogs — "what does the user see?".

### Interface sketch

```python
# lib/watchtogether.py
def create(self, source_uri, title, users=None):   # POST /rooms -> Room (201)
def invite(self, room_id, user_ids):               # POST /rooms/{id}/invite -> Room

# lib/plexpeople.py
def friends(token, fetch=None):                    # [{id, title, thumb}]
def shared_users(token, machine_id, fetch=None):   # [{id, title}] (owner only)
def eligible_invitees(token, item, owned, room_user_ids=(), fetch=None):
    # -> [Invitee(id, title, thumb, access_unknown)]
```

## Eligibility & invite picker

- **Own server** (`item.server.owned`): home users + friends present on the
  server's `shared_servers` list.
- **Shared server**: all friends + home users; each row carries
  `access_unknown` and the dialog labels it.
- Self and existing room members are excluded.
- Plain scroll list, avatar + name, multi-select, no search.
- The same picker serves the lobby's **Invite (+)** and the room tile menu's
  **Invite…**.

## Readiness & start

- Readiness: `isReady = video loaded AND not caching` — the existing
  `_update_ready` / `set_ready`, sent only on change (§5.4).
- Host: item open + paused → ready when buffered. Guest: same.
- **Auto-start:** for every user in `room.users` except self, they must be on
  `session.roster` with `isReady is True`, and self must be ready.
  `room.users` refreshes on the 15 s poll, so late invites count. A member who
  never joins blocks auto-start — the host presses **Start**.
- **Start** = unpause self; the existing local-change path
  (`on_local_change('play')` → `send_now`) propagates `paused:false`, and guests
  unpause via `_apply_remote`.
- **Guest lobby visibility:** show while the room is unstarted — relay `State`
  paused **and** `position < 1 s`; close the moment playback starts. The
  protocol has no explicit lobby flag, so this is a heuristic; the only false
  positive is a room paused below 1 s mid-movie, which is negligible.
- **Ready notification gap:** `syncplay._on_set` writes
  `roster[key]["isReady"]` without firing a callback, so the lobby's status list
  would not update live. Add a ready/roster notification (join/leave already
  notify via `on_event`).

## Error handling

- `POST /rooms` fails (network/auth) → toast, no lobby, stay on Home.
- `invite` `400` (target is not a friend/home user — §11.9) → mark that person
  failed, toast, keep the room (the rest may have succeeded), refresh the list.
- `401` → existing dead-token handling; surface "sign in again" for
  create/invite.
- plex.tv friends fetch fails → picker falls back to home users only + a
  message.
- Item has no resolvable `sourceUri` / is not playable → the entry is hidden.
- **Cancel** → leave (`DELETE`) + close. Documented: the room lingers for
  anyone already in it (there is no destroy endpoint — §11.9).
- Guests keep today's join-error handling.

## Testing strategy

- `RoomsApi.create` (201 → `Room`; 401/4xx/5xx) and `invite` (200 → `Room`;
  400 relationship; 500 non-numeric id) against the existing fake transport.
- `eligible_invitees`: own vs shared server; friends ∩ sharees; home users; self
  excluded; already-in-room excluded; `access_unknown` flags.
- Readiness aggregation: all-ready → auto-start; a missing/unready member blocks
  it; manual Start forces; a ready change fires a notification.
- Lobby/invite dialog props: Ready/Invited labels, access-unknown, host vs guest
  mode.
- Bridge host flow end-to-end with fakes: create → open paused → invite → start
  → unpause.
- Existing suites stay green.

## Risks / unknowns (verify in planning, read-only)

1. **plex.tv friends endpoint/shape** — `/api/v2/friends` (JSON) vs the legacy
   XML `/api/users/{id}/friends`. Confirm against the live token with a
   read-only probe; do not log any response that could carry a token.
2. **Id matching** — that a friend's id equals the id used by
   `shared_servers` and by `invite {users:[id]}`.
3. **Non-friend sharee gate** — whether a server-sharee who is not a friend
   passes the invite gate (§11.9 only proved an unrelated stranger is refused).
   If not, eligibility must intersect sharees with friends.
4. **Host opens paused** — the video window's `play()` blocks, so the pause must
   be applied from the bridge/supervisor side once playback starts (the thread
   the guest's remote-apply already uses), or a start-paused option added to the
   play chain.
5. **Guest lobby heuristic** — paused-at-0 detection (above).

## Open items (resolve in writing-plans)

- Exact plex.tv endpoints and their auth/headers; where the friends fetch lives.
- How the host's item opens paused and where the lobby window is hosted (over the
  video window, like the existing dialogs).
- Whether "Start Watch Together" should also appear on the item detail page
  (preplay) or only the hub context menu.
- Skin layout details for the two new dialogs.

## Process notes

- Work on `feature/watch-together-impl`; squash later per
  `docs/superpowers/squash-state.md` (rule 3).
- Spec → `writing-plans` → phased implementation, subagent-driven, review
  between phases.
- The templating staleness fix (`48a78163`, PR #299) stays excluded from the WT
  squash.
