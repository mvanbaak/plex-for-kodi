# Watch Together for PM4K — design spec

Date: 2026-10-06
Branch: `feature/watch-together-impl` (cut from `feature/watch-together-research`,
which keeps the protocol research isolated)
Protocol reference: `docs/watch-together.md` (authoritative; this spec does not
restate protocol details)

## Goal

Implement Plex Watch Together in PM4K so a Kodi user can join someone else's
room and watch in sync with the host. Host capability (create room, invite)
is deferred to v2 but planned so it slots in without rework.

## Scope

**v1 (guest-only):**
- Browse available rooms (`GET /rooms`), join, sync-watch playback.
- Rich UX: sidebar entry, room picker, OSD sync line, participants dialog,
  leave action, poll-driven invite/leave toasts.
- Local pause/seek broadcast to the room (protocol-native: last `setBy` wins;
  no host-only write gate exists).
- Guest joins and follows; the host (web app) starts the room.

**v2 (deferred, structure reserved):**
- `createRoom` + invite from PM4K, media-item context entry.
- Tempo-based catch-up for Kodi 21+ (v1 is hard-seek only).
- Pubsub room notifications (v1 polls every 15–20 s).
- Takeover UI polish (explicit "you're controlling" state).

**Explicit non-goals (v1):** shared playlists, ad-break sync, chat/avatars/
notifications beyond toasts, `controller` flag, home-server edge flows.

## Architecture

Four new files (per research doc §10.1):

| File | Job | Kodi imports |
|---|---|---|
| `lib/ws.py` | minimal RFC6455 client (TLS, text frames, ping/pong, backoff) | none |
| `lib/watchtogether.py` | rooms REST + model (mirrors `myplexaccount.py`); v2 methods stubbed | none |
| `lib/syncplay.py` | protocol session FSM, `SyncState`, drift/RTT math, ignore rules | **none** (testability rule) |
| `lib/windows/watchtogether.py` | dialogs, OSD, player bridge | only file touching `xbmc` |

Edits to existing code, all small and gated:
- `lib/player.py` — broadcast on pause/resume/seek callbacks
  (`:3047`/`:3055`/`:3098`); sync-override flag consulted alongside
  `shouldSendTimeline`-style gating (`:145`).
- `lib/windows/seekdialog.py` — sync-override so `_applyingSeek` (`:2024`)
  and offset tracking don't treat remote-driven seeks as local ones.
- `lib/windows/home.py` — sidebar entry.

**Locked decisions:**
- Sync engine owns its own reader thread; polls
  `util.MONITOR.abortRequested()`; not folded into `_videoMonitor` (session
  must live in lobby without playback); no signal handoff;
  `BackgroundThreader` is one-shot and wrong here.
- Room id is a bearer secret: never log at debug, never render as a share link.
- TLS verification stays on for `*.syncplay.plex.services`.
- New dependency: none (hand-rolled WS client; `ssl`/`socket` only).
- Protocol layer must stay cheap to delete wholesale (service is announced
  deprecated): no protocol code in core player paths beyond the listed hooks.

**Preconditions (not code):** plex.tv account token (room discovery; unavailable
in local mode), and library access to the host's PMS (membership ≠ media access).

## Phases

Each phase has a deliverable, tests, and an exit gate. Baseline guard: `uv run
pytest` must stay green at every phase boundary — the 717 existing tests never
regress, new tests only add.

### P1 — `lib/ws.py`
Deliver: RFC6455 client — TLS connect, handshake, text frames, ping→pong,
close, reader thread with reconnect backoff. No Plex/Kodi imports.
Tests (offline pytest `tests/test_ws.py`): frame encode/decode round-trips
(masked client frames, fragmentation, payload-length boundaries), handshake
key derivation vs vector from probe captures.
Exit: offline tests green.

### P2 — `lib/syncplay.py`
Deliver: identity JSON (<150 bytes, `username` double-encoding, `/_+$/`
strip), `Hello`/`List`/`State` echo loop, `Set{ready}`, ignore rules
(`setBy`-is-self, `ignoringOnTheFly.{client,server}`), drift + RTT math,
fg/bg asymmetry, plain `SyncState`. Consumes frames, emits actions —
transport injected.
Tests (offline pytest `tests/test_syncplay.py`): drift/ping math edge cases
(paused vs playing, negative RTT, stale/absent `serverRtt`), ignore-rule
decisions against a recorded relay transcript fixture in
`tests/fixtures/syncplay/`.
Exit: offline tests green; research doc §8 checklist items for session/sync
loop pass against the transcript.

### P3 — `lib/watchtogether.py` + live protocol proof (still no Kodi)
Deliver: REST model — token, `GET /rooms`, `GET /rooms/{id}`, join-by-id.
v2 methods (`createRoom`, invite) present as stubs raising NotImplementedError.
Tests (dev-only live script, **not in pytest**): two real accounts, real relay
— join → Hello → List → 15 min State-echo soak → `Set{ready}` → clean leave;
assert roster/positions match and 0 rooms remain. Runs actual `lib/ws.py` +
`lib/syncplay.py`.
Exit: live soak passes — protocol proven end-to-end before any Kodi code.

### P4 — Player integration (highest risk)
Deliver: `lib/windows/watchtogether.py` bridge — remote State →
`seekAbsolute`/pause/resume; local events → broadcast; sync-override flag
wired through player + seek dialog; lobby/ready flow (join before playback);
join starts playback from room `sourceUri`.
Tests: extractable event-mapping unit tests where Kodi-free; manual
two-account Kodi checklist — pause propagates <1.5 s, seek converges, OSD open
doesn't hijack playback (fg/bg), no echo loops, 10 min stability, 0-rooms
cleanup after.
Exit: checklist passes.

### P5 — UI
Deliver: sidebar entry, room picker, OSD line ("3 watching — following X"),
participants dialog, leave-room action, poll-driven invite/leave toasts.
Tests: manual UI walk-through; 717 baseline intact (GUI code largely
uncovered per repo policy).
Exit: v1 feature-complete and usable as guest.

### P6 — Host capability (v2, planned)
Deliver: real `createRoom`/invite/delete, friends list, media-item entry.
Fills the P3 stubs; no architecture change.

## Testing strategy

1. **Offline pytest (CI):** `tests/test_ws.py`, `tests/test_syncplay.py`
   (recorded relay transcript fixture), `tests/test_watchtogether.py`
   (fake HTTP responses, same pattern as existing recorded-XML plexnet
   tests). Import-hygiene test: `lib/ws.py` and `lib/syncplay.py` import
   without `kodistubs`.
2. **Dev-only live validation:** runnable script using real `lib/` modules +
   local tokens against the real relay; re-run at each phase regression.
   Never touches pytest; tokens never committed.
3. **Manual two-account Kodi checklist:** trimmed from research doc §8
   conformance checklist to what v1 implements.

## Risks

- **P4 desync class** (remote vs local seek bookkeeping) — highest; gated by
  two-account checklist; UI waits for it.
- **Service deprecation** — protocol isolated for cheap removal.
- **Kodi <21** — no tempo sync; hard-seek only in v1.
- **Local mode** — no room discovery without account token; documented
  precondition.
- **Invite latency** — 15–20 s poll accepted; pubsub promoted first if it stings.
- **Two-account manual burden** — P1–P3 are fully testable before Kodi.

## Open items (resolve in writing-plans)

- Exact name/location of the live validation script.
- P6 invite UI data source (plex.tv friends vs home users).

## Process notes

- All work on `feature/watch-together-impl`; `feature/watch-together-research`
  remains the untouched research snapshot.
- Spec → `writing-plans` → phased implementation, subagent-driven, review
  between phases.
