# Squash state — shareable branch

Status record for `feature/watch-together-squash`. Lives in `docs/superpowers/`
so it is committed here (development branch) but **never shipped** on the
squash branch, like the spec and plan next to it.

## Where the squash branch stands

`feature/watch-together-squash` is the branch for sharing outside this repo.
Its tree matches `feature/watch-together-impl` **except** `docs/superpowers/`
(the whole directory: specs, plans, this file), `.gitignore` (the squash branch
ignores `docs/superpowers/`; the impl branch tracks it), and the excluded
templating staleness fix (`lib/templating/render.py`, `tests/test_templates.py`
— see rule 3). All three differences are expected.

Base `2707bbe7` (`1.14.1`), then (SHAs as of the **2026-10-10 redaction
rewrite**; the pre-rewrite SHAs `27f7dda8`/`198cf1a3`/`8a8ec00f`/`84e9d3a8`/
`aedf3899` are dead — see the note below):

- `61c55624` chore: uv dev tooling and Watch Together protocol research
- `5eeebc88` feat(watchtogether): Phase 1 protocol library
- `fcb146b6` feat(watchtogether): Phase 2 Kodi UI integration
- `a49018d1` feat(watchtogether): hub room menu, OSD controls, dialogs
- `e3feea9d` feat(watchtogether): host capability — create, invite, lobby  ← current tip

**Pushed** to `origin` (the fork) and open as **PR #300** against
`pannal:develop_kodi21` (`mvanbaak:feature/watch-together-squash`). The branch
was **force-pushed on 2026-10-10** to scrub real PII from its history — see the
redaction note below.

## 2026-10-10 redaction rewrite

The squash branch shipped real PII in test fixtures from the first WT commit:
`tests/test_syncplay.py` used a real plex account id (`2028816`), and
`tests/test_watchtogether_hub.py` used real usernames / display titles
(`michiel`, `yogarine`, `Amanda & Michiel`, `Alwin & Andréa`). The local
redaction map (`REDACTION-LOCAL-not-commit.md`, never committed) has the
real→placeholder mapping.

Fix: the fixtures were replaced with the map's placeholders on the impl branch
(`3c01aec1`), and the squash branch's **whole history** was rewritten with
`git filter-branch --tree-filter` (applying the same substitutions) and
force-pushed. Every squash-branch SHA changed; the pre-rewrite SHAs are dead.

Lessons: the redaction grep must scan the **whole tree**, not just `docs/`
(rule 4), and test fixtures are a real leak surface.

## Rules for future squashes

1. **Everything up to `f67fcc89` on `feature/watch-together-impl` is already
   squashed** (into the squash branch: `fcb146b6`, `a49018d1`, then
   `e3feea9d`). Do not re-squash those commits. The granular history lives on
   the impl branch only and stays there.
2. Development continues on `feature/watch-together-impl` with granular
   commits.
3. To update the shareable branch, squash **only the commits added after
   `f67fcc89`** and append the result to `feature/watch-together-squash` as a
   single commit. Recipe (run from the repo root):

   ```bash
   # marker = current impl HEAD, the new "already squashed up to" point
   MARKER=$(git rev-parse --short feature/watch-together-impl)

   git checkout feature/watch-together-squash
   git checkout feature/watch-together-impl -- .          # bring impl's tree
   git rm -r --cached -q docs/superpowers && rm -rf docs/superpowers
   git checkout HEAD -- .gitignore                        # keep the squash .gitignore
   # exclude the upstream templating staleness fix (48a78163, PR #299):
   # it is a general bug fix tracked separately, not part of the WT work
   git checkout HEAD -- lib/templating/render.py tests/test_templates.py
   git add -A
   git commit --no-gpg-sign -m "feat(watchtogether): <what changed>"

   # verify (see rule 4)
   git diff --name-only feature/watch-together-squash feature/watch-together-impl \
       | grep -vE '^docs/superpowers/|^lib/templating/render.py$|^tests/test_templates.py$' \
       || echo "(nothing else differs)"
   uv run pytest -q
   git checkout feature/watch-together-impl
   ```

   Note: the `git checkout HEAD -- lib/templating/render.py tests/test_templates.py`
   line above only matters while `48a78163` lives on the impl branch but is
   excluded from the squash. If PR #299 merges first (or the fix is otherwise
   dropped from impl), that line becomes a no-op (the files already match).

   Then update rule 1's marker (to `$MARKER`) and the "current tip" above.
4. Verification after every squash: the command in the recipe prints
   **only `.gitignore`**, and `uv run pytest -q` is green on the squash branch.
   Baselines: 805 passed at Phase 1 exit, 945 at Phase 2 exit, 956 at the
   `a49018d1` squash, **1050 at the `e3feea9d` squash**.

   **Also run a whole-tree redaction grep before pushing — not just `docs/`.**
   The 2026-10-10 leak lived in test fixtures (`tests/test_syncplay.py`,
   `tests/test_watchtogether_hub.py`); a docs-only grep never caught it. The
   local redaction map (`REDACTION-LOCAL-not-commit.md`, never committed) has
   the term list and recipe.
5. Never commit `docs/superpowers/` or anything token-shaped to the squash
   branch — token hygiene rules from the spec apply: no token values anywhere.
6. When the squash branch is pushed and a PR is opened upstream
   (`pannal:develop_kodi21`), the PR body **must** list the caveats from the
   "Draft PR body" section below. `docs/superpowers/` never ships, so the PR
   description is the only place reviewers (and users) ever see them.

## Follow-ups not yet squashed

Squashed into `e3feea9d` (impl marker `f67fcc89`): the v2 host capability —
`RoomsApi.create`/`invite`, the `plexpeople` eligibility helper (community
GraphQL friends + server sharees), the `syncplay` ready callback, the host flow
+ `LobbyDialog`, the `InviteDialog` picker, home menu entries, the guest lobby,
the whole-branch review fixes, and the live-test fixes (host lobby mode, lobby
over Home, ESC handling, friend-name fallback, focus defaults, diagnostics
strip). Nothing outstanding.

Still separate: the templating staleness fix (`48a78163`) — cherry-picked to
`fix/template-staleness` (PR #299 against `pannal:develop_kodi21`); NOT part of
the WT squash.

Known caveats shipped on the squash branch (`fcb146b6`, `a49018d1`,
`e3feea9d`): tempo catch-up only truly applies on Kodi 21.1+ (on 21.0
`Player.SetTempo` is refused; the bridge logs once and hard-seeks); auto-join
rejoins the session but does not auto-start playback; the host lobby runs over
Home (the video opens on Start, so a short start delay) because a modal dialog
shown over the video leaves input focus on the video window; the friends list
comes from an undocumented community GraphQL endpoint (degrades to home users
when it fails).

Join-time startup stutter (observed live, left as-is): joining a room opens the
video at offset 0, then the first relay `State` makes the bridge seek to the
room's position. That mid-playback seek refills the buffer (the visible freeze);
because the room keeps advancing while it refills, a second corrective seek lands
shortly after — two stalls in the first few seconds, then sync is stable. The
proper fix is to open the video at the room position (needs the first `State`
before playback, so an explicit start offset would have to be threaded through
`videoplayer.play` → `playVideo` → `_playVideo`); the cheaper WT-only mitigation
is to skip a drift-based remote seek while the player is still seeking/caching.

## Draft PR body (for the final upstream PR)

Rule 6 requires this to be pasted (and kept current) into the PR description
when `feature/watch-together-squash` is pushed. Copy from the block below.

```markdown
# Plex Watch Together — Home hub, join/leave, host lobby

Adds Plex Watch Together to PM4K.

**Guest**

- Rooms appear as the **first Home hub**, hidden when there are none.
- A tile joins the room (confirm on takeover / switching rooms); while watching
  a room, clicking its tile opens the participants dialog.
- Room tiles have a context menu: Join / Remove / Info.
- The video OSD gains a single person-icon button (Leave room / Participants).
- The room info dialog lists the media item and participants (live marker for
  the room you are in).
- Leaving sends `DELETE` (drops membership); stopping playback only
  disconnects, keeping membership so the tile can rejoin.

**Host**

- Start a room from a movie/episode: it is created on the cloud, a lobby opens,
  and the item plays once the room starts.
- Invite your Plex friends and Home users from a picker. On an item from a
  server you own, only friends who can reach that server are offered; items on a
  server shared to you are offered with an "access unknown" note.
- The lobby shows the media title and each participant's Ready/Invited status
  and auto-starts when every invited member is ready (or the host presses
  Start). A guest sees the same lobby read-only while the room is unstarted.
- Invite more people later from the room tile menu.

## Known limitations

- **Tempo catch-up needs Kodi 21.1+.** On 21.0 `Player.SetTempo` is refused; the
  bridge logs once and falls back to a hard seek. (Hard-seek sync still works.)
- **The host lobby runs over Home.** A modal dialog shown over the video leaves
  input focus on the video window, so the host's video opens on Start instead —
  a short start delay rather than a pre-buffered video.
- **Joining a room can stutter for the first few seconds.** Playback opens at 0
  and then syncs to the room's position; the seek's buffer refill — plus a second
  corrective seek that lands while it is still refilling — freezes briefly. Sync
  is stable afterwards.
- **Friends come from an undocumented community GraphQL endpoint.** It degrades
  to Home users when it fails.
- **Auto-join does not auto-start playback.** If enabled, it rejoins the session
  on startup but the user still starts playback themselves.

Not included: the templating staleness fix, tracked separately (PR #299).
```
