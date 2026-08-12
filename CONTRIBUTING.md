# Contributing

This is a personal, single-user tool (see [CLAUDE.md](CLAUDE.md)), so
contributions aren't expected to be frequent. If you do send a pull request,
here's what applies.

## CLA

Before a first pull request can be merged, you need to agree to the
Contributor License Agreement below. A bot will comment on your PR asking you
to reply with the sign-off phrase; that reply is your agreement. You only do
this once per GitHub account.

This exists so the project's license can be changed in the future — including
to a different license, or a closed-source one — without needing to separately
track down and re-clear every past contributor. The project owner remains free
to do this with their own code regardless; the CLA only covers what's needed
to extend that same freedom to contributed code.

### Contributor License Agreement

By contributing to this repository, you agree that:

1. You have the right to submit the contribution under the terms of this
   agreement (it's your own original work, or you otherwise have the right
   to submit it).
2. You grant the project owner (John Torcivia) a perpetual, worldwide,
   non-exclusive, royalty-free, irrevocable license to use, reproduce,
   modify, distribute, sublicense, and relicense your contribution, in whole
   or in part, under any license terms — including terms different from the
   license this project is currently distributed under.
3. You retain copyright ownership of your own contribution. This agreement
   grants a license, not an assignment of copyright.
4. Your contribution is provided "as is," without warranty of any kind.

## Sending a change

Normal GitHub flow: fork, branch, open a PR against `main`. Explain the "why"
in the PR description, not just the "what" — see the commit conventions
already in this repo's history for the expected level of detail.

## Testing a change in a worktree

Development happens in linked worktrees under `.worktrees/`, so the main
checkout keeps serving the running stack. To test a branch's code against
real files, bring up a second stack that serves that worktree:

```bash
git worktree add .worktrees/fix-NNN-brief-desc -b fix/NNN-brief-desc origin/main
cd .worktrees/fix-NNN-brief-desc
make build                     # tsc + vite + Docker images
./backend/run-worktree-tests.sh tests/services/test_X.py -q   # backend tests
./ops/worktree-up.sh           # second stack on its own ports (prints them)
```

`./ops/worktree-up.sh` derives ports from the branch name and copies a
point-in-time snapshot of the main stack's database into the worktree stack
on first launch; `--reseed` re-copies it. The stack shares `BIOINFO_HOME`
with the main stack, so UI and read-path checks are safe, but think twice
before running a pipeline that rewrites an existing artifact.

## Tearing down a worktree test stack

After testing is complete, remove the second stack's containers, network,
and volumes, then delete the worktree itself:

```bash
# 1. Stop the worktree stack and delete its volumes (the containers it
#    created, its network, and its mongo/redis volumes — everything under
#    the biopipe-wt-<branch> compose project).
./ops/worktree-up.sh --down

# 2. Remove the worktree itself, then delete its branch. `git worktree
#    remove` only deletes the working tree; the branch is a separate ref
#    and must be deleted by name.
cd ~/Programming/local-bio-pipeliner
git worktree remove .worktrees/fix-NNN-brief-desc
git worktree prune
git branch -D fix/NNN-brief-desc    # only if the branch is merged or abandoned
```

What `./ops/worktree-up.sh --down` does: it runs
`docker compose down -v` against the worktree's project
(`biopipe-wt-<branch>`), stopping the API/web/worker containers and removing
the anonymous volumes they used (the seeded Mongo snapshot and Redis data).
It does **not** touch the main `biopipe` stack, its containers, or its data
volumes — the whole point of the separate compose project name.

What `git worktree remove` does: deletes the worktree directory
(`.worktrees/fix-NNN-brief-desc`). `git worktree prune` clears git's
bookkeeping for removed worktrees. The local branch is a separate ref, so
it is deleted by name with `git branch -D` once the PR is merged (the
remote branch is removed by the merge itself).

A worktree stack left running is harmless but noisy: it keeps its own Mongo
and Redis containers alive and consumes a port pair. The cleanup above is
idempotent — running `--down` again or removing an already-removed worktree
reports nothing to do rather than failing.
