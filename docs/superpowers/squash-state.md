# Squash state — shareable branch

Status record for `feature/watch-together-squash`. Lives in `docs/superpowers/`
so it is committed here (development branch) but **never shipped** on the
squash branch, like the spec and plan next to it.

## Where the squash branch stands

`feature/watch-together-squash` is the branch for sharing outside this repo.
Its tree matches `feature/watch-together-impl` **except** `docs/superpowers/`
(the whole directory: specs, plans, this file) and `.gitignore` (the squash
branch ignores `docs/superpowers/`; the impl branch tracks it — that one-line
diff is expected and is the only non-`docs/superpowers/` difference).

Base `2707bbe7` (`1.14.1`), then:

- `27f7dda8` chore: uv dev tooling and Watch Together protocol research
- `198cf1a3` feat(watchtogether): Phase 1 protocol library
- `8a8ec00f` feat(watchtogether): Phase 2 Kodi UI integration  ← current tip

**Not pushed.** The squash branch is local only; push it only when explicitly
asked.

## Rules for future squashes

1. **Everything up to `51bd5141` on `feature/watch-together-impl` is already
   squashed** (into `8a8ec00f`). Do not re-squash those commits. The granular
   history lives on the impl branch only and stays there.
2. Development continues on `feature/watch-together-impl` with granular
   commits.
3. To update the shareable branch, squash **only the commits added after
   `51bd5141`** and append the result to `feature/watch-together-squash` as a
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
       | grep -v '^docs/superpowers/' || echo "only superpowers differs"
   uv run pytest -q
   git checkout feature/watch-together-impl
   ```

   Note: the `git checkout HEAD -- lib/templating/render.py tests/test_templates.py`
   line above only matters while `48a78163` lives on the impl branch but is
   excluded from the squash. If PR #299 merges first (or the fix is otherwise
   dropped from impl), that line becomes a no-op (the files already match).

   Then update rule 1's marker (`51bd5141` → `$MARKER`) and the "current tip"
   above.
4. Verification after every squash: the command in the recipe prints
   **only `.gitignore`**, and `uv run pytest -q` is green on the squash branch.
   Baselines: 805 passed at Phase 1 exit, **945 passed at Phase 2 exit**.
5. Never commit `docs/superpowers/` or anything token-shaped to the squash
   branch — token hygiene rules from the spec apply: no token values anywhere.
6. When the squash branch is pushed and a PR is opened upstream
   (`pannal:develop_kodi21`), the PR body **must** list the caveats from the
   "Draft PR body" section below. `docs/superpowers/` never ships, so the PR
   description is the only place reviewers (and users) ever see them.

## Follow-ups not yet squashed

After `8a8ec00f` (Phase 2 exit), these are new granular commits on
`feature/watch-together-impl`, to be squashed later per rule 3:

- `3e237459` room tile context menu — join / remove / info (done).
- `505ef628` Leave + Participants buttons in the video OSD (done).
- `48a78163` templating staleness fix — cherry-picked to a separate branch
  (`fix/template-staleness`, PR #299 against `pannal:develop_kodi21`); it is
  NOT part of the WT squash.

Known caveats shipped in `8a8ec00f`: tempo catch-up only truly applies on Kodi
21.1+ (on 21.0 `Player.SetTempo` is refused; the bridge logs once and hard-seeks),
and auto-join rejoins the session but does not auto-start playback.

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
# Plex Watch Together — Home hub, join/leave, OSD sync

Adds Plex Watch Together as a guest client:

- Rooms appear as the **first Home hub**, hidden when there are none.
- A tile joins the room (confirm on takeover / switching rooms); while watching
  a room, clicking its tile opens the participants dialog.
- Room tiles have a context menu: Join / Remove / Info.
- The video OSD gains a single person-icon button (Leave room / Participants).
- The room info dialog lists the media item and participants (live marker for
  the room you are in).
- Leaving sends `DELETE` (drops membership); stopping playback only
  disconnects, keeping membership so the tile can rejoin.

## Known limitations

- **Tempo catch-up needs Kodi 21.1+.** On 21.0 `Player.SetTempo` is refused; the
  bridge logs once and falls back to a hard seek. (Hard-seek sync still works.)
- **Auto-join does not auto-start playback.** If enabled, it rejoins the session
  on startup but the user still starts playback themselves.
- **Joining a room can stutter for the first few seconds.** Playback opens at 0
  and then syncs to the room's position; the seek's buffer refill — plus a second
  corrective seek that lands while it is still refilling — freezes briefly. Sync
  is stable afterwards. Not fixed here; the fix is to open the video at the room
  position instead of seeking into it.
- **Guest-only in v1.** Creating/inviting to rooms (host capability) is planned
  for a follow-up.

Not included: the templating staleness fix, tracked separately (PR #299).
```
