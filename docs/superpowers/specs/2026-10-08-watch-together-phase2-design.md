# Watch Together for PM4K — Phase 2 design (P4 + P5)

Date: 2026-10-08
Branch: feature/watch-together-impl

## Goal

Add Kodi UI/player integration for Plex Watch Together (guest flow, host remains deferred). One plan covers P4 (player bridge) + P5 (UI).

## Decisions

- Supervisor lives in `lib/watchtogether.py` (Kodi-free, testable) — `SessionSupervisor`.
- Glue in `lib/windows/watchtogether.py` (only Kodi-touching bridge).
- Thread destroyed on leave (not singleton). On disconnect → backoff 1/2/4/8/30s, rejoin with Hello+List+Set{ready}. RoomGone/NotMember → `on_gone`, stop thread.
- Poll = 15s REST `room()` for roster/invites.
- Expanded settings toggles: enable sidebar entry, auto-join last room, show OSD status.
- `wt_applying_remote` flag gates local broadcast on player seek/pause/resume; `seekdialog._applyingSeek` untouched.

## Supervisor (Kodi-free)

Add to `lib/watchtogether.py`:
- `class SessionSupervisor(object)`: ctor `room, identity, token, ws_factory, transport=None, clock=None, abort=None`
- methods: `start()`, `stop()`, `outbound_state(local)`
- callbacks: `on_state`, `on_roster`, `on_disconnected`, `on_gone`, `on_event`
- loop: manage `Session` + `WSClient`; send Hello+List+Set{ready} on open/rejoin; 15s poll; backoff; stop on gone.

No xbmc imports. Uses stdlib + existing `RoomsApi`/errors.

## Bridge + player integration

- `lib/windows/watchtogether.py`: build supervisor/dialogs, manage `wt_applying_remote` (set/cleared in try/finally around remote-driven `player.seekAbsolute`/pause/resume, cleared on stop).
- `lib/player.py`: add `self.wt_applying_remote = False`; in `onPlayBackPaused`/`onPlayBackResumed`/`onPlayBackSeek` skip broadcast when flag set.
- `lib/windows/seekdialog.py`: unchanged (remote seeks via bridge → `doSeek`).

Lifecycle: join → start supervisor; leave/gone → `supervisor.stop()`, close dialogs, clear props.

## UI + settings

- `home.py`: sidebar entry + menu → `RoomPickerDialog`.
- XMLs: `resources/skins/Main/1080i/script-plex-watchtogether_room_picker.xml`, `script-plex-watchtogether_participants.xml`.
- OSD status via `setGlobalProperty('watchtogether.status', ...)`, shown in `script-plex-seek_dialog.xml`.
- Settings via `util.getSetting`: `watchtogether.enable_sidebar`, `watchtogether.auto_join_last`, `watchtogether.show_osd_status`.
- 15s poll drives roster + toasts (`util.showNotification`); clean leave on gone.

## Robustness & test plan

- Offline pytest for supervisor (fakes) — no `xbmc` in `lib/`, extend import-hygiene test.
- 805 baseline green, grow only.
- Manual two-account: pause <1.5s, seek converges, 10 min stability, roster correct, OSD doesn't hijack playback, 0 rooms after leave.
- `scripts/watchtogether_soak.py` unchanged.

## Verification

Only allowed tree diff vs impl: `docs/superpowers/`:
```bash
git diff --name-only feature/watch-together-squash feature/watch-together-impl | grep -v '^docs/superpowers/' || echo "only superpowers differs"
```
`uv run pytest -q` must stay green.
