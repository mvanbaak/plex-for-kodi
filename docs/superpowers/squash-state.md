# Squash state — shareable branch

Status record for `feature/watch-together-squash`. Lives in `docs/superpowers/`
so it is committed here (development branch) but **never shipped** on the
squash branch, like the spec and plan next to it.

## Where the squash branch stands

`feature/watch-together-squash` is the branch for sharing outside this repo.
Tree matches `feature/watch-together-impl`, with `docs/superpowers/` excluded
(its whole directory, spec + plan + this file).

It currently holds Phase 1 as two commits on base `2707bbe7`:

- `c518970d` chore: uv dev tooling and Watch Together protocol research
- `48469941` feat(watchtogether): Phase 1 protocol library

## Rules for future squashes

1. **Everything up to `66f6eedd` on `feature/watch-together-impl` is already
   squashed.** Do not re-squash those commits. The granular history exists on
   the impl branch only and stays there.
2. Development continues on `feature/watch-together-impl` with granular
   commits (Phase 2: Kodi UI implementation).
3. When it is time to update the shareable branch: squash **only the commits
   added after `66f6eedd`**, and append the result to
   `feature/watch-together-squash` as a single commit — the kodi-ui
   implementation commit.
4. After each such squash, verify the way Phase 1 was verified — the only
   allowed tree difference between the squash branch and impl is
   `docs/superpowers/`:

   ```bash
   git diff --name-only feature/watch-together-squash feature/watch-together-impl | grep -v '^docs/superpowers/' || echo "only superpowers differs"
   ```

   Also run `uv run pytest -q` on the squash branch — 805 passed at Phase 1
   exit, grow from there.
5. Never commit `docs/superpowers/` or anything token-shaped to the squash
   branch — token hygiene rules from the spec apply: no token values
   anywhere.
