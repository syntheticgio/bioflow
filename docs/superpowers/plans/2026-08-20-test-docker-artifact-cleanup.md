# Docker artifact cleanup after tests — implementation plan

Date: 2026-08-20.

Closes [#719](https://github.com/syntheticgio/bioflow/issues/719). Companion to
`docs/superpowers/specs/2026-08-20-test-docker-artifact-cleanup-design.md`
(decisions T1–T4, requirements R1–R5).

The fix itself is one flag. The work is the guard (T3) and the documentation
(T4) — without those, this recurs and the 75 GB already stranded stays
stranded.

## Files to touch

| File | Change |
|---|---|
| `backend/run-worktree-tests.sh` | `cleanup()` at ~line 128: `docker rm -f` → `docker rm -fv`, both lines. Add a comment saying **why** the `-v` is load-bearing, because a future reader will otherwise see it as redundant beside `--rm` and remove it. |
| `ops/check-test-cleanup.sh` | **New.** The T3 guard: count dangling volumes before and after, assert a delta of exactly zero. |
| `docs/agent-notes.md` | A section on reclamation (T4/R4), beside the existing Docker-disk material. Narrowly scoped commands, destructive ones shown `--dry-run`-first. |

## Ordered steps

1. **The fix.**

   ```bash
   cleanup() {
     # -v, not just -f: mongo:7 declares VOLUME [/data/db /data/configdb], and
     # neither --rm nor `docker rm -f` removes anonymous volumes. Without this
     # every run stranded ~340 MB, which reached 75 GB before anyone noticed
     # (#719).
     docker rm -fv "$MONGO_NAME" >/dev/null 2>&1 || true
     docker rm -fv "$SSHD_NAME" >/dev/null 2>&1 || true
   }
   ```

   The comment is not decoration. The bug survived review precisely because
   the cleanup looked correct, and `-v` next to an existing `--rm` reads as
   redundant to anyone who has not measured it.

2. **Check the other two containers** (spec's verify items 1 and 2) before
   calling R2 done:
   - `lscr.io/linuxserver/openssh-server` — `docker history --no-trunc` for a
     `VOLUME` instruction. It goes through the same `cleanup()`, so step 1
     likely covers it; confirm rather than assume.
   - the `docker run --rm` at ~line 282 (the pytest container itself) — no
     `-d`, so it is removed synchronously, but the same volume rule applies.
     If it mounts anything anonymous, it needs `-v` too.

3. **The guard**, `ops/check-test-cleanup.sh`. Shape:

   ```bash
   before=$(docker volume ls -qf dangling=true | wc -l)
   # ... minimal invocation of the script's start + cleanup path ...
   after=$(docker volume ls -qf dangling=true | wc -l)
   ```

   **Assert the delta is exactly zero, not "small"** (R3). A tolerance is what
   turns a deterministic leak back into background noise, which is how this
   went unnoticed for 237 runs.

   Run it against all three invocation paths: plain, `--with-sshd`,
   `--with-node`.

   This lives in `ops/`, not pytest: it needs a Docker daemon, so it cannot
   run in the test container that the suite itself runs in.

4. **The interrupt case.** Kill the script mid-run with SIGINT and confirm the
   volume delta is still zero. The `trap cleanup EXIT` should cover it, but
   this is how a developer actually stops a long suite, and it is the path
   most likely to skip cleanup — so it is worth one manual check rather than
   an assumption.

5. **Documentation** (T4/R4). In `docs/agent-notes.md`, beside the existing
   Docker-disk material. Scope each command as narrowly as the case allows,
   and show the dry-run form first:

   ```bash
   docker system df                        # what is actually consuming space
   docker volume ls -qf dangling=true | wc -l
   docker builder prune --filter until=168h --dry-run
   ```

   **Do not add a `make clean-docker`.** `docker volume prune` is machine-wide
   and knows nothing about which volumes belong to BioFlow, so a convenience
   target here can delete another project's data (R5). The repo already takes
   this posture with `worktree-up.sh --prune`, which acts only on its own
   project-name prefix and offers `--dry-run`.

6. **Reclaim the existing 75 GB** — but per T4 this is the **user's** call to
   run, not the implementer's to automate. Before recommending a bulk command,
   confirm the 237×~340MB + 241×0B split accounts for substantially all 481
   dangling volumes (spec verify item 3), so the documented command is not
   casually deleting something else's data.

## What proves it worked

Not "the script has `-v` now". Run the suite twice and measure:

```bash
docker volume ls -qf dangling=true | wc -l
./backend/run-worktree-tests.sh tests/models -q
docker volume ls -qf dangling=true | wc -l
```

Same number both times. Before the fix this rises by exactly 2 per run —
measured, not inferred:

```
rm -f (no -v):  before=483 after=485  leaked=2
rm -fv:         before=485 after=485  leaked=0
```

## Out of scope

Per the spec: image bloat (176 GB, a different cause — rebuilds, not tests),
CI-side cleanup (runners are ephemeral), scheduled pruning (R5), and changing
the test Mongo's isolation model — its private replica set exists because
sharing one produced order-dependent failures, and a named volume would
reintroduce that in a harder-to-see form.
