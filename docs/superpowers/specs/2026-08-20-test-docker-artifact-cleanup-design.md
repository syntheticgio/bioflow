# Docker artifact cleanup after tests — design

Date: 2026-08-20.

Closes [#719](https://github.com/syntheticgio/bioflow/issues/719), "Make sure
stacks/volumes are cleaned up after tests".

## The problem, measured

On this machine on 2026-08-20:

```
TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE
Images          323       9         176.5GB   67.87GB (38%)
Containers      19        15        932.6MB   5.247MB (0%)
Local Volumes   492       11        77.32GB   75.43GB (97%)
Build Cache     615       0         85.71GB   21.3GB
```

481 dangling volumes. Their size distribution is the fingerprint that
identifies the source:

- **237** volumes at ~340 MB
- **241** volumes at 0 B

Two volumes per test run, ~340 MB and 0 B — which is exactly what one
`mongo:7` container strands.

## Root cause: `--rm` and `rm -f` do not remove anonymous volumes

`backend/run-worktree-tests.sh` starts a private Mongo replica set per run
(line 134):

```bash
docker run -d --rm --name "$MONGO_NAME" --network biopipe_default \
  mongo:7 --replSet rs0 --bind_ip_all
```

and cleans up (line 129):

```bash
cleanup() {
  docker rm -f "$MONGO_NAME" >/dev/null 2>&1 || true
  ...
}
trap cleanup EXIT
```

`mongo:7` declares `VOLUME [/data/db /data/configdb]` (verified via
`docker history --no-trunc mongo:7`), so each run creates two **anonymous**
volumes. Neither `--rm` nor `docker rm -f` removes them; **`-v` is required.**

Verified empirically rather than from documentation:

```
rm -f (no -v):  before=483 after=485  leaked=2
rm -fv:         before=485 after=485  leaked=0
```

The `/data/db` volume holds whatever the suite wrote — ~340 MB — and
`/data/configdb` stays empty at 0 B. That is precisely the observed
237/241 split.

## Why it was invisible

Three things conspired, and all three are worth recording because each would
independently have hidden it:

1. **The cleanup looks correct.** There is a `cleanup()` function and a
   `trap cleanup EXIT`. The trap fires reliably. It removes the container and
   silently leaves the volumes, so code review sees handled cleanup.
2. **Nothing reports it.** No script, CI job, or Makefile target runs
   `docker system df`, and there is no prune tooling anywhere in the repo
   (`grep` for `volume prune` / `system prune` / `builder prune` finds
   nothing).
3. **The per-run cost is small.** 340 MB is unremarkable; 237 runs is 75 GB.

**`ops/worktree-up.sh` does not have this bug** — it uses `down -v`
throughout, which is why the leak is specific to the test path and why "stacks
are cleaned up" felt true while volumes accumulated.

## Decision T1: fix the leak with `-v`, and fix it at the source

`cleanup()` becomes `docker rm -fv`. One flag, both containers.

Do **not** address this by adding a periodic prune that deletes dangling
volumes. That treats a deterministic leak as background noise, and a prune
broad enough to catch these would also catch volumes another workflow
legitimately left dangling. The leak has one cause and one fix.

## Decision T2: a named volume is not the alternative

The tempting alternative — give the test Mongo a named volume so it is
reusable — is wrong here, and for a reason the script itself documents.

Its comment records why the private replica set exists at all: sharing Mongo
with the main stack produced "7 failed, then 1872 passed, then 5 failed",
fixed by isolation to five consecutive identical runs. A **named** volume
would persist state across runs and reintroduce exactly that class of
order-dependent failure, in a form that is harder to see because it survives
container recreation.

Anonymous-and-removed is the right lifecycle: fresh per run, gone after.

## Decision T3: a guard, because the next `docker run` will repeat this

The fix is one flag, so the durable risk is that someone adds a third
`docker run` to a test path and strands volumes again — the same silent
failure, rediscovered in six months at another 75 GB.

**The guard is a shell test, not a lint rule.** A test that asserts the
script's cleanup path removes volumes cannot run without Docker, so it belongs
with the ops checks rather than in pytest:

- Count dangling volumes before and after a minimal invocation of the script's
  own start/cleanup path.
- Assert the delta is **zero**, not "small".

This is the check that would have caught the bug the day it was written, and
it is the only part of this work that is not one character.

*Weaker alternative considered:* grep the repo for `docker run` without `-v` in
cleanup. Cheap, but it encodes the shape of today's mistake rather than the
property that matters (no volume survives a run), and it produces false
positives on every legitimate `docker run --rm` that mounts nothing.

## Decision T4: reclamation is a documented manual step, not automation

75 GB of volumes, 21 GB of reclaimable build cache, and 68 GB of reclaimable
images exist right now and will not remove themselves.

**Reclaiming is the user's call, not the tooling's.** A script that prunes
Docker on this machine can delete another project's data — `docker volume
prune` is machine-wide and knows nothing about which volumes belong to
BioFlow. This repo already takes that posture with `worktree-up.sh --prune`,
which acts only on stacks matching its own project-name prefix and offers
`--dry-run` first.

So: document the commands in `docs/agent-notes.md` beside the existing
Docker-disk material, scoped as narrowly as each case allows, with the
destructive ones shown `--dry-run`-first. Do not add a `make clean-docker`
that runs `docker system prune -af`.

**What this buys beyond the immediate 75 GB:** per the existing note that a
hung worktree test script or a crash-looping Mongo is usually a full Docker
disk, this is the condition that makes test failures unreadable — the symptom
appears as a test hang, never as "disk full". Fixing the leak removes the main
producer of that condition.

## Requirements

- **R1.** A completed run of `backend/run-worktree-tests.sh` leaves zero
  additional Docker volumes, whether it passes, fails, or is interrupted.
- **R2.** The same holds for the `--with-sshd` and `--with-node` paths, which
  start a second container.
- **R3.** A check exists that fails if a future change reintroduces a volume
  leak in the test path, asserting a delta of exactly zero.
- **R4.** The reclamation commands for volumes, build cache, and images are
  documented, scoped as narrowly as possible, destructive ones shown with a
  dry-run form first.
- **R5.** No automation deletes Docker artifacts the user did not ask to
  delete.

## Testing

- **R1/R2** — the T3 guard, run once against each of the three invocation
  paths (plain, `--with-sshd`, `--with-node`).
- **Interrupt case** — the trap already fires on EXIT; confirm the volume
  delta is still zero when the script is killed mid-run (SIGINT), since that
  is how a developer usually stops a long suite and it is the path most likely
  to skip cleanup.
- No pytest changes: nothing here is Python.

## Verify before implementing

1. **Does `lscr.io/linuxserver/openssh-server` declare volumes too?** If so,
   R2 is not automatically satisfied by the same one-flag fix and the sshd
   container needs the same treatment (it already goes through the same
   `cleanup()`, so `-v` likely covers it — confirm rather than assume).
2. **Does the `docker run --rm` at line 282** (the pytest container itself)
   mount anything anonymous? It has no `-d`, so it is removed synchronously,
   but the same volume rule applies.
3. **Whether any of the 481 existing dangling volumes are not from this leak** —
   before recommending a bulk prune, confirm the 237/241 split accounts for
   substantially all of them, so the documented command does not casually
   delete something else's data.

## Out of scope

- **Image bloat.** 176 GB of images with 68 GB reclaimable is a real problem
  and a different one — it comes from rebuilding the backend image, not from
  tests. Worth its own issue if it keeps growing.
- **CI-side cleanup.** The measured leak is local; CI runners are ephemeral.
- **Automatic pruning on any schedule** (T4, R5).
- **Changing the test Mongo's isolation model** (T2).
