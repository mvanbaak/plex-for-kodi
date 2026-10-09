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
   git add -A
   git commit --no-gpg-sign -m "feat(watchtogether): <what changed>"

   # verify (see rule 4)
   git diff --name-only feature/watch-together-squash feature/watch-together-impl \
       | grep -v '^docs/superpowers/' || echo "only superpowers differs"
   uv run pytest -q
   git checkout feature/watch-together-impl
   ```

   Then update rule 1's marker (`51bd5141` → `$MARKER`) and the "current tip"
   above.
4. Verification after every squash: the command in the recipe prints
   **only `.gitignore`**, and `uv run pytest -q` is green on the squash branch.
   Baselines: 805 passed at Phase 1 exit, **945 passed at Phase 2 exit**.
5. Never commit `docs/superpowers/` or anything token-shaped to the squash
   branch — token hygiene rules from the spec apply: no token values anywhere.

## Follow-ups not yet squashed

After `8a8ec00f` (Phase 2 exit), the following are planned as new granular
commits on `feature/watch-together-impl`, to be squashed later per rule 3:

- Leave / participants in the video player OSD (the OSD status label already
  exists; add a Leave action next to it).
- Leave / participants as a context menu on the room tile in the Home hub
  (the WT `hubMenu` branch is currently a no-op).

Known caveats shipped in `8a8ec00f`: tempo catch-up only truly applies on Kodi
21.1+ (on 21.0 `Player.SetTempo` is refused; the bridge logs once and hard-seeks),
and auto-join rejoins the session but does not auto-start playback.
