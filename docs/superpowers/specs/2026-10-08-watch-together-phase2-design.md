# Watch Together for PM4K — Phase 2 design (P4 + P5)

Date: 2026-10-08
Branch: feature/watch-together-impl
Protocol reference: `docs/watchtogether.md` (authoritative; §numbers cited below)

## Goal

Add Kodi UI/player integration for Plex Watch Together (guest flow, host remains deferred). One plan covers P4 (player bridge) + P5 (UI).

Build order: 1 supervisor (+offline tests) → 2 bridge + player echo gate → 3 UI/settings → 4 robustness + manual checklist.

## Decisions

- Supervisor lives in `lib/watchtogether.py` (Kodi-free, testable) — `SessionSupervisor`.
- Glue in `lib/windows/watchtogether.py` — the only *new* file importing `xbmc` (edits to `player.py`/`home.py` stay small; `seekdialog.py` untouched).
- Thread destroyed on leave (not singleton): one fresh supervisor per join; `stop()` is idempotent and joins the thread. On disconnect → backoff 1/2/4/8/30s (cap 30s, reset after a successful open), rejoin with Hello+List+Set{ready}; retry until `stop()`/abort. `WSClient` has no auto-reconnect — the supervisor owns this.
- Poll = 15s REST, two owners: in a room → `RoomsApi.room()` (roster + membership, supervisor runs it); in the lobby → `RoomsApi.rooms()` (invite toasts, bridge runs it — no supervisor exists there). `room()` cannot show invites.
- Settings toggles (all new): sidebar entry, auto-join last room, show OSD status.
- Echo prevention: `wt_applying_remote` is a `time.monotonic()` deadline (`now + 2`) set before each remote-driven apply. `PlexPlayer.onPlayBackPaused`/`onPlayBackResumed`/`onPlayBackSeek` skip the local broadcast while `now < deadline`. Kodi may deliver those callbacks after the apply call returns, so try/finally around the apply is too short; the deadline self-heals (ceiling: a genuine local event inside the 2s window is dropped, the next event/heartbeat converges). `seekdialog._applyingSeek` untouched.

## Supervisor (Kodi-free)

Add to `lib/watchtogether.py`:
- `class SessionSupervisor(object)`: ctor `room, identity, token, ws_factory, transport=None, clock=None, abort=None` (`transport` feeds an internal `RoomsApi`; `clock`/`abort` keep time and shutdown fakeable in tests)
- methods: `start()`; `stop()` (idempotent, joins the thread); `outbound_state(local)` — stores the latest local snapshot and, when connected, sends one State frame now (no-op while disconnected)
- callbacks: `on_state`, `on_roster`, `on_disconnected`, `on_gone`, `on_event`
- loop: manage `Session` + `WSClient`; send Hello+List+Set{ready} on open/rejoin; re-send the snapshot at 1 Hz (§5.5 heartbeat); poll `room()` every 15s; backoff on disconnect. Any REST `RoomGone`/`NotMember` (poll, join or rejoin) → `on_gone` + stop; `AuthError` → stop too (never retry a dead token); `on_gone` never calls `RoomsApi.leave()` — 403/404 already mean you're out.

No xbmc imports. Uses stdlib + existing `RoomsApi`/errors.

## Bridge + player integration

- `lib/windows/watchtogether.py`: build supervisor + dialogs; own the local-state snapshot handed to `outbound_state(local)`; apply remote state — seek via `dialog.doSeek(offset)` (reuses the local seek path, so `_applyingSeek` and SeekHandler bookkeeping already behave, and no `seekdialog.py` edit is needed), pause/resume via the player — setting the `wt_applying_remote` deadline before each apply and on stop; run the 15s lobby `rooms()` poll for invite toasts while no supervisor runs.
- `lib/player.py`: add `self.wt_applying_remote = 0.0` (deadline); in `onPlayBackPaused`/`onPlayBackResumed`/`onPlayBackSeek` skip the broadcast while `time.monotonic() < self.wt_applying_remote`.
- `lib/windows/seekdialog.py`: unchanged.

Lifecycle: join → start supervisor. User leave → `RoomsApi.leave()` then `supervisor.stop()`. `on_gone` → `supervisor.stop()` only (no `DELETE`). Both close dialogs, clear props, reset the echo deadline. Startup with `watchtogether.auto_join_last` → join the stored room id through the same path.

## UI + settings

- `home.py`: sidebar entry (virtual entry via `showSections()`, §9), gated by `watchtogether.enable_sidebar`. No room joined → `RoomPickerDialog`; in a room → participants dialog (roster + the leave-room action).
- XMLs: `resources/skins/Main/1080i/script-plex-watchtogether_room_picker.xml`, `script-plex-watchtogether_participants.xml`; `script-plex-seek_dialog.xml` gains one label control (existing file, small edit).
- OSD status via `util.setGlobalProperty('watchtogether.status', ...)`, read in the skin as `$INFO[Window(10000).Property(watchtogether.status)]`, shown only when `watchtogether.show_osd_status`.
- Settings: `watchtogether.enable_sidebar` (default `True`), `watchtogether.auto_join_last` (default `False`), `watchtogether.show_osd_status` (default `True`) — dot-namespaced like `kiosk.*`, declared in `resources/settings.xml` with new en_gb `strings.po` labels (same shape as `auto_seek`), read via `util.getSetting`. Last room id stored under the undeclared key `watchtogether.last_room` (`util.setSetting`, same pattern as `previous_server.*`).
- 15s poll drives roster + toasts (`util.showNotification`); clean leave on gone.

## Robustness & test plan

- Offline pytest for the supervisor in `tests/test_watchtogether.py` (fake `ws_factory`/`transport`/`clock`) — the no-`xbmc` boundary for `lib/watchtogether.py` is already enforced by `tests/test_protocol_isolation.py` (`MODULES`), so no hygiene-test extension is needed.
- 805 baseline green, grow only.
- Manual two-account: pause <1.5s, seek converges, 10 min stability, roster correct, OSD doesn't hijack playback, 0 rooms after leave; lobby invite toast and `auto_join_last` on startup.
- `scripts/watchtogether_soak.py` unchanged.

## Verification

Only allowed tree diff vs impl: `docs/superpowers/`:
```bash
git diff --name-only feature/watch-together-squash feature/watch-together-impl | grep -v '^docs/superpowers/' || echo "only superpowers differs"
```
Today that prints `.gitignore` — expected: the squash branch ignores `docs/superpowers/` while the impl branch tracks it. Not a code change.
`uv run pytest -q` must stay green.
