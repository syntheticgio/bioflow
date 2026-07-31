# Running backend tests from this worktree

CLAUDE.md's `docker compose exec api python -m pytest` is **wrong for a
worktree** and will silently mislead you.

The shared `biopipe` stack bind-mounts the *main* repo root:

```
/Users/syntheticgio/Programming/local-bio-pipeliner/backend/app
/Users/syntheticgio/Programming/local-bio-pipeliner/backend/tests
```

So `docker compose exec api pytest` runs **main's code**, not this
worktree's. Tests pass while proving nothing about your changes. Repointing
the stack at the worktree is not an option either -- per CLAUDE.md it
silently recreates *the* stack and breaks the instance on port 5173.

## Use this instead

A throwaway container off the same image and network, with this worktree's
source mounted:

```bash
docker run --rm --network biopipe_default \
  -v "$PWD/backend/app:/app/app" \
  -v "$PWD/backend/tests:/app/tests" \
  -w /app biopipe-api \
  python -m pytest tests/ -q
```

Run it from the worktree root (`$PWD` must be the worktree). Narrow to one
file or test by replacing `tests/` as usual:

```bash
docker run --rm --network biopipe_default \
  -v "$PWD/backend/app:/app/app" \
  -v "$PWD/backend/tests:/app/tests" \
  -w /app biopipe-api \
  python -m pytest tests/pipelines/test_tools.py -v
```

Verified 2026-07-31: 48/48 pass in `test_tools.py`, and a deliberately
failing sentinel test appended in the worktree *did* fail in the container,
confirming the mount is live rather than reading the image's baked-in copy.

`--network biopipe_default` gives the container the Mongo replica set, which
is why this works where a host venv does not.

## Its one real limitation: don't judge the *full* suite by it

**Reliable for a targeted file. Not a substitute for the full suite.**
`docker run` does not supply everything `docker compose` does, so
`pytest tests/` under it reports **15 failures and 134 errors** that are pure
artifact -- async fixtures that never get their plugin, and Mongo-backed
tests that cannot reach what they need.

Those numbers cost real time here: they looked like pre-existing breakage on
main and were nearly filed as a bug against the image. The same suite run the
documented way is clean:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner
docker compose exec api python -m pytest tests/ -q
```

→ `1716 passed` (verified 2026-07-31 on main, after this feature merged).

So: `docker run` for fast iteration on the files you are editing; the
CLAUDE.md `docker compose exec` form -- on main, after merging -- before
believing anything about overall health.

## Frontend and UI verification

Not possible from the worktree, and not worth making possible. Per the
user's instruction on 2026-07-31: **merge to main and test there.** The
instance on port 5173 is not production and is fine to test on.

That applies to Tasks 10-11 of the software-reference-help plan, which need
a browser.
