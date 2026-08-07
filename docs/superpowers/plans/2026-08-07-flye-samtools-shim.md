# flye-samtools Shim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a `flye-samtools` wrapper on `PATH` in the backend image so Flye's
consensus stage stops failing, unblocking every de novo assembly on this
deployment.

**Architecture:** One `RUN` block in `backend/Dockerfile` writes a three-line
`/bin/sh` wrapper that execs `/usr/bin/samtools`, then asserts it works in the
same layer. No application code changes.

**Tech Stack:** Docker (Debian trixie base), Flye 2.9.5+dfsg-1, samtools 1.21.

**Spec:** [`docs/superpowers/specs/2026-08-07-flye-samtools-shim-design.md`](../specs/2026-08-07-flye-samtools-shim-design.md)
**Issue:** [#67](https://github.com/syntheticgio/bioflow/issues/67)

---

## Read this before Task 1

**There is no unit test in this plan, and that is deliberate — do not add one.**

If you came here expecting red-green-refactor, this task shape will look
wrong. It isn't. The defect lives at the boundary between Flye's Python and a
binary on `PATH`. A `pytest` case could only observe it by mocking
`subprocess`, which is precisely the thing that is broken — the mock would
pass on a broken image and on a fixed one alike. CLAUDE.md records this trap
directly: in this repo, tests asserting a tool is *available* pass whether or
not the seam under them works.

The test here is `flye-samtools --version` inside the image, in the same `RUN`
layer as the thing it checks, and it runs on every build. Task 1 writes it.
Task 2 proves it fails on an image that lacks the shim, which is the
equivalent of watching a test go red before you make it green.

**One piece of pre-existing state you must clear first.** During brainstorming
a shim was written *by hand* into the running `biopipe-api-1` container to
confirm the approach. It is not from the Dockerfile and will not survive a
rebuild, but until Task 2 rebuilds, that container can run Flye successfully
for the wrong reason. Task 2's first step removes it so the failure you
observe is real.

---

## File Structure

| File | Change | Responsibility |
| --- | --- | --- |
| `backend/Dockerfile` | Modify, insert after line 91 | Installs the shim and asserts it |

That is the entire change surface. No Python, no tests directory, no compose
files. If you find yourself editing a second file, stop and re-read the spec's
"Out of scope" section — a runtime probe in `tools.flye()` and a
`suggestion_service.py` change were both considered and explicitly rejected.

---

### Task 1: Install the shim in the image

**Files:**
- Modify: `backend/Dockerfile:118` (insert immediately before the `# bwa-mem2 is the exception:` comment)

**Placement matters, and the obvious spot is the wrong one.** The block belongs
after the apt install that provides both `flye` and `samtools` — but that apt
block is *followed* by a long comment run (lines 92-116) that documents the
packages inside it: libcurl4t64/libgomp1, hmmer, subread, flye, bcftools. Those
comments belong to the block above them. Inserting at line 92, right after the
`rm -rf /var/lib/apt/lists/*`, would strand them from what they describe.

Insert after that comment run ends, immediately before the `# bwa-mem2 is the
exception:` comment at line 118.

- [ ] **Step 1: Read the surrounding context**

Run: `sed -n '100,120p' backend/Dockerfile`

Confirm you can see the `flye is the de novo assembler` comment (around line
103) and the `# bwa-mem2 is the exception:` line (around line 118). Your
insertion point is the blank line between them.

- [ ] **Step 2: Insert the shim block**

Insert exactly this, immediately before the `# bwa-mem2 is the exception:`
comment:

```dockerfile
# --- flye-samtools shim -----------------------------------------------------
#
# Flye 2.9.5 upstream vendors its own samtools build and invokes it as
# `flye-samtools`. Debian's flye 2.9.5+dfsg-1 unbundles that binary -- the
# +dfsg suffix says so -- and declares `Depends: minimap2, samtools`,
# expecting the packaged /usr/bin/samtools to serve instead.
#
# The unbundling patched one of the two call sites and missed the other:
# flye/polishing/alignment.py:27 was rewritten to "/usr/bin/samtools", but
# flye/utils/sam_parser.py:43 still reads SAMTOOLS_BIN = "flye-samtools" and
# reaches it from three places (two `depth` calls and a `view`). `dpkg -L
# flye` ships no such binary, so every assembly died at the consensus stage
# with "flye-samtools: not found" and an empty consensus.fasta. See
# https://github.com/syntheticgio/bioflow/issues/67.
#
# A wrapper rather than a symlink. Either works -- unlike bwa-mem2 below,
# samtools has no argv[0]-relative dispatch to break -- but the wrapper
# matches that precedent and states the indirection outright.
#
# The trailing --version is a build-time assertion, in this same RUN so it
# cannot drift from what it checks: the build fails if samtools ever moves off
# /usr/bin/samtools. Same shape as the `datasets --version` and
# `docker --version` checks further down this file.
RUN printf '#!/bin/sh\nexec /usr/bin/samtools "$@"\n' > /usr/local/bin/flye-samtools \
    && chmod +x /usr/local/bin/flye-samtools \
    && flye-samtools --version

```

- [ ] **Step 3: Verify the file still parses as a Dockerfile**

Run: `docker build --check -f backend/Dockerfile backend/`

Expected: no errors. If `--check` is unavailable on this Docker version, skip
this step — Task 2's real build covers it.

- [ ] **Step 4: Confirm the diff is one hunk in one file**

Run: `git diff --stat`

Expected: `backend/Dockerfile` only, roughly 28 insertions, 0 deletions. If any
other file appears, or if there are deletions, something went wrong — the
insertion should add lines and change nothing existing.

- [ ] **Step 5: Commit**

```bash
git add backend/Dockerfile
git commit -m "$(cat <<'EOF'
fix(docker): provide flye-samtools shim for Flye consensus stage (#67)

Debian's flye 2.9.5+dfsg-1 unbundles upstream's vendored samtools but
patched only polishing/alignment.py, leaving utils/sam_parser.py calling
the absent `flye-samtools`. Every assembly died at the consensus stage.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Prove the assertion catches the bug

This is the red half. You are checking that the build-time assertion added in
Task 1 would actually have failed on the broken image — an assertion that
cannot fail is worth nothing.

**Files:** none modified. This task only builds and observes.

- [ ] **Step 1: Remove the hand-written shim from the running container**

A shim was written by hand into `biopipe-api-1` during brainstorming. Remove it
so nothing observes a false success:

```bash
docker exec biopipe-api-1 rm -f /usr/local/bin/flye-samtools
```

Expected: no output. A "No such file" error is also fine — it means the
container was already restarted since.

- [ ] **Step 2: Confirm the bug is real on the pre-fix image**

```bash
docker exec biopipe-api-1 sh -c 'command -v flye-samtools || echo ABSENT'
```

Expected output: `ABSENT`

This is the failing state. If it prints a path instead, Step 1 did not take —
do not continue until this prints `ABSENT`, or you will "verify" a fix against
an image that was never broken.

- [ ] **Step 3: Build the fixed image**

From the **worktree root** (this is a worktree — plain `docker compose` is
blocked here by a `PreToolUse` hook, and correctly so):

```bash
docker build -f backend/Dockerfile -t biopipe-flye-shim-check backend/
```

Expected: build completes. Watch for the `flye-samtools --version` step near
the middle of the output printing `samtools 1.21` / `Using htslib 1.21`. That
line is the assertion passing.

If the build fails *at that step*, the shim is wrong — read the error before
changing anything.

- [ ] **Step 4: Confirm the shim resolves both subcommands Flye actually calls**

`sam_parser.py` calls exactly two subcommands. Check both:

```bash
docker run --rm biopipe-flye-shim-check sh -c 'flye-samtools --version | head -2; flye-samtools depth 2>&1 | head -1; flye-samtools view 2>&1 | head -1'
```

Expected: `samtools 1.21`, then `Using htslib 1.21`, then a `Usage: samtools
depth` line, then a `Usage: samtools view` line. Usage output on a bare
subcommand is success here — it proves the subcommand is recognized.

- [ ] **Step 5: Confirm Flye's own module sees it**

```bash
docker run --rm biopipe-flye-shim-check python3 -c "
from shutil import which
print('sam_parser sees:', which('flye-samtools'))
import sys; sys.path.insert(0, '/usr/lib/python3/dist-packages')
from flye.utils.sam_parser import SAMTOOLS_BIN
print('SAMTOOLS_BIN =', SAMTOOLS_BIN, '->', which(SAMTOOLS_BIN))
"
```

Expected: both lines resolve to `/usr/local/bin/flye-samtools`. A `None` on
the second line means the shim is not on the `PATH` Flye's own process sees,
which is the whole bug — stop and investigate.

- [ ] **Step 6: Clean up the throwaway image**

```bash
docker rmi biopipe-flye-shim-check
```

- [ ] **Step 7: No commit**

This task changes no files. Nothing to commit. If `git status` is dirty here,
you edited something you shouldn't have.

---

### Task 3: End-to-end verification with a real assembly

The build assertion proves the binary resolves. It does not prove Flye gets
past `consensus` — that needs a real run, and it is the only check that
actually closes #67.

**Files:** none modified.

- [ ] **Step 1: Bring up the worktree stack**

From the worktree root:

```bash
./ops/worktree-up.sh
```

Expected: UI on 5273, API on 8100. This builds from *this worktree's* source,
which is the point — never verify this against the main stack on 5173, which
serves main's image and does not have the fix.

- [ ] **Step 2: Run an assembly through the UI**

Open http://localhost:5273, pick a project with long reads, and start a Flye
assembly from the Actions tab.

Use the smallest read set available. This is a real assembly and the runtime
is dominated by the input size, not by anything this change touches — a
bacterial-scale or subsampled input proves the fix as well as a large one and
finishes in a fraction of the time.

- [ ] **Step 3: Watch the consensus stage specifically**

`worktree-up.sh` derives its project name from the worktree directory
(`biopipe-wt-<slug>`, set at `ops/worktree-up.sh:58`), so for this worktree it
is `biopipe-wt-issue-65-implementation-901b38`. Naming a project explicitly is
also what lets this `docker compose` call through the worktree hook:

```bash
docker compose -p biopipe-wt-issue-65-implementation-901b38 logs -f worker \
  | grep -iE 'STAGE|consensus|samtools|ERROR'
```

If that project name turns up nothing, get the real one from
`docker compose ls` rather than guessing.

Expected: `>>>STAGE: consensus` appears, is followed by `Computing consensus`,
and is **not** followed by `flye-samtools: not found`. The run should proceed
past consensus into the stages after it.

The precise failure this replaces, for comparison:

```
INFO: >>>STAGE: consensus
INFO: Computing consensus
/bin/sh: 1: flye-samtools: not found
ERROR: parse error in .../10-consensus/consensus.fasta on line 1: empty sequence
```

- [ ] **Step 4: Confirm the assembly produced a non-empty result**

The original failure signature was an empty `consensus.fasta`. Confirm the job
reached a terminal success state in the UI and that the resulting assembly
object has a non-zero size and a plausible contig count.

- [ ] **Step 5: Tear down the worktree stack**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 6: Restore the main stack if you touched it**

Only if Step 1 was skipped and the main stack was used instead. From the
**main checkout root** (not this worktree):

```bash
docker compose up -d --build api web worker
```

Then confirm it is serving the main checkout, not a worktree:

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

Expected: paths under the main checkout, nothing under `.claude/worktrees/`.

---

### Task 4: Close out the issue and merge

- [ ] **Step 1: Rebuild the main stack with the fix**

The fix is only real for day-to-day use once the 5173 stack carries it. From
the **main checkout root**, after the merge in Step 4 below — do this step
last if merging first, or repeat it after merging.

- [ ] **Step 2: Update issue #67**

```bash
gh issue comment 67 --body "$(cat <<'EOF'
Fixed. The diagnosis in the issue was close but the defect is narrower than "the Dockerfile is missing a symlink step".

Debian's `flye 2.9.5+dfsg-1` unbundles upstream's vendored samtools and declares `Depends: samtools`, which is satisfied — `/usr/bin/samtools` is present and correct. The bug is that the unbundling patched only one of Flye's two samtools call sites:

| File | `SAMTOOLS_BIN` | State |
| --- | --- | --- |
| `flye/polishing/alignment.py:27` | `/usr/bin/samtools` | patched |
| `flye/utils/sam_parser.py:43` | `flye-samtools` | **missed** |

`sam_parser.py` reaches it from three places (two `depth` calls, one `view`), which is why the failure surfaces at the consensus stage specifically.

Fix is a `flye-samtools` wrapper on `PATH` execing `/usr/bin/samtools`, plus a `flye-samtools --version` build-time assertion in the same layer so the build fails if samtools ever moves.

Spec: `docs/superpowers/specs/2026-08-07-flye-samtools-shim-design.md`
EOF
)"
```

- [ ] **Step 3: Label the issue**

```bash
gh issue edit 67 --add-label "status:ready"
```

- [ ] **Step 4: Merge to main and push**

Per CLAUDE.md: `main` is this project's dev trunk, and a green build plus a
verified assembly is the bar. Confirm `main` is clean and has not moved
(`git log --oneline main -1` should still be `98a299e`; if it moved, merge
main in and re-run Task 2 before continuing), then:

```bash
git checkout main && git merge --no-ff claude/issue-67-brainstorm-7e8de8 && git push origin main
```

- [ ] **Step 5: Close the issue**

```bash
gh issue close 67
```

---

## Notes for whoever executes this

**`docs/TODO.md` needs no entry.** This bug was tracked as a GitHub issue, not
a backlog entry — check with `grep -i flye docs/TODO.md` and, if it is genuinely
absent, there is nothing to move to `docs/TODO-done.md`. If it *is* there,
follow CLAUDE.md's close-out procedure: append ` — FIXED`, note what shipped,
and move the whole entry across.

**This worktree is named for issue 65, not 67.** The branch
(`claude/issue-67-brainstorm-7e8de8`) is right; the directory it sits in
belongs to an earlier task. Harmless for a one-file change, but do not be
confused by it, and do not "fix" the directory name mid-task.

**If the assembly in Task 3 fails somewhere *after* consensus**, that is a
different bug and a new issue — not a reason to reopen this one or to widen
this change. #67 is specifically the consensus-stage `flye-samtools` failure,
and the spec's scope boundary is deliberate.
