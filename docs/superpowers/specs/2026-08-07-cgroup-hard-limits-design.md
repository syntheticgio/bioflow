# Opt-in hard resource limits via cgroups

Design for [#72](https://github.com/syntheticgio/bioflow/issues/72), a child of
epic [#7](https://github.com/syntheticgio/bioflow/issues/7).
Written 2026-08-07.

## What this adds to the admission design

[The resource limits admission
design](2026-08-07-resource-limits-admission-design.md) chose admission over
enforcement: BioFlow refuses to *start* work that will not fit, and a job that
overruns its prediction is an accepted outcome. That design also recorded why,
and named the exception:

> Cgroup enforcement remains a legitimate thing to want, for someone who would
> rather lose a job than a machine. It is out of scope here and stays as its
> own issue.

This is that issue. It does not revisit the default -- soft admission stays the
default and stays unchanged. It adds a second, opt-in ceiling for the user who
wants the kernel to make the guarantee.

The two mechanisms are complementary rather than alternative, and that is the
point of the layering. Admission keeps jobs off the wall; the wall exists for
the mispredicted job that admission let through. A correctly configured system
hits the wall approximately never.

## Where the setting lives, and why it cannot live in the web UI

**The launcher owns it.** `launcher/src-tauri/src/settings.rs` rewrites `.env`
in the install directory and then runs `docker compose up -d` to recreate the
stack. Storage location, port, and network exposure already work exactly this
way. A cgroup limit is the same move: one more `.env` line and one more
recreate.

The web UI cannot own it, for a structural reason rather than a preference. A
cgroup limit is applied at container creation, so changing it requires
recreating the container -- and the API is *inside* the container being
recreated. A process cannot rewrite the compose environment that started it and
restart itself cleanly. The launcher runs outside the stack, which is what makes
it the only component that can legitimately do this.

This also matches when the user wants to make the decision: before launching,
alongside "where does my data live" and "what port," rather than buried in an
application that is already running.

## Blank means no hard cap

The launcher screen gets **one field, blank by default**. Blank means no hard
cap. Clearing the field turns enforcement back off.

There is deliberately **no separate on/off toggle** next to the number. A toggle
plus a number is two controls that can disagree -- an enabled toggle with an
empty box, or a filled box with the toggle off, both of which need a rule
nobody will remember. The number alone already expresses off.

This mirrors `ResourceLimits`, whose docstring already records the same
reasoning for the soft budget:

> None is a real state rather than a null needing cleanup: a fresh install has
> no opinion, and the machine's actual budget is the right default. The UI's
> "No limit" option writes None rather than a sentinel number.

Off-by-default falls out of Compose's own semantics rather than needing code:
`mem_limit: ${BIOFLOW_HARD_MEM_LIMIT:-0}` resolves to `0` when the variable is
unset, and `0` is Docker's own no-limit value -- the same thing `docker run
--memory=0` means -- not a sentinel this repo invented. (An empty-string
default was tried first and rejected: Compose v5 type-checks `mem_limit` as a
byte-size field and errors on `''` outright, caught by running `docker compose
config` against the real tool rather than assuming the substitution would
parse.) A fresh install has no cgroup limit without anyone having chosen that.

**The blank state must still be captioned.** An empty field with no explanation
reads as "unset, probably fine," not "no hard cap -- a job that overruns will
not be killed." Since blank is the state nearly every user will be in, it is the
state the acceptance criterion about stating the distinction most needs to
cover. The wording for both states is in [The wording](#the-wording) below.

## What compose does with it

```yaml
worker:
  mem_limit: ${BIOFLOW_HARD_MEM_LIMIT:-0}     # 0 = unlimited (Docker's own value)
  deploy:
    replicas: ${WORKER_REPLICAS:-2}           # launcher pins to 1 when a limit is set

api:
  environment:
    BIOFLOW_HARD_MEM_MB: ${BIOFLOW_HARD_MEM_MB:-}   # for the clamp; api is NOT capped
```

Three decisions are encoded there, and each rules out an alternative that looks
reasonable until it is tried.

**The limit goes on `worker` only.** Capping `api` would put the web UI itself
under an OOM ceiling. A user who set the limit slightly too low would lose the
interface they need in order to raise it, which is a worse outcome than anything
this feature prevents. `api` is a small web process whose footprint is unrelated
to job size; it is not what threatens the machine.

**Replicas are pinned to 1 whenever a limit is set.** `mem_limit` is
per-container, and `WORKER_REPLICAS` defaults to 2. A 16 GB limit across two
replicas permits 32 GB, so the wall would sit at twice the number the user
typed. Dividing the limit by the replica count was the alternative and is worse:
it makes the `.env` value disagree with the entered value, and it silently
halves the largest single job that can run. With hard limits on, one worker with
a ceiling that means what it says beats two with an ambiguous one.

**`api` receives the value as a plain environment variable, not by reading its
own cgroup.** This is the subtle one. `governor._read_cgroup_mem()` reads
`/sys/fs/cgroup/memory.max`, which inside `api` is *`api`'s own* cgroup -- and
under the decision above `api` is uncapped, so it reads `max`. An implementation
that had the web UI infer the hard limit from its own cgroup file would find
nothing and silently skip the clamp. That failure is in the dangerous direction:
it fails open, with no error, exactly when the user has asked for a guarantee.
Passing the number explicitly removes the inference.

## Three consumers, one number

**The kernel enforces it.** The worker's cgroup `memory.max` is the hard wall.
This is the entire mechanism; nothing in BioFlow implements enforcement.

**The governor reads it as its budget, with no code change.** `mem_budget_bytes()`
already falls back to `_read_cgroup_mem()` when `settings.bioinfo_mem_budget_mb`
is unset, and that runs inside the worker, whose cgroup *is* limited. So
admission automatically starts planning against the real ceiling the moment one
exists. This is the acceptance criterion the issue expects to satisfy for free,
and it does -- the existing code path was written for exactly this.

The effect is that the two mechanisms cooperate rather than merely coexist:
admission holds jobs back from the wall, so the wall is a backstop rather than a
routine occurrence.

**The web UI clamps the soft budget to it.** Settings · Resources reads
`BIOFLOW_HARD_MEM_MB` and refuses a soft budget above it.

Without the clamp, a user can set a 32 GB admission budget under a 16 GB hard
limit, and every job admission cheerfully approves is then killed by the kernel
-- the worst available version of this feature, and one that reads as BioFlow
being broken rather than as a misconfiguration. Clamping makes that combination
unrepresentable instead of merely discouraged.

**The clamp is one-directional.** A soft budget *below* the hard limit is normal
and expected -- it is the configuration where admission does its job and the
wall is never touched. Only *above* is refused.

## An OOM kill fails once, not five times

`pipeline_handlers.py:769` maps exit code 137 to `RetryableError` with "killed,
most likely out of memory." That is a good guess today: with no cgroup limit, a
137 means the host OOM killer fired under transient whole-machine pressure, and
retrying later may genuinely succeed.

The comment above that line says the 137 "is worth one retry," but nothing
enforces one: `RetryableError` re-enters the normal retry path, which is
`job_max_attempts: 5`. The comment is already describing an intent the code does
not implement, and this change should correct it rather than leave a note that
contradicts the line under it.

A hard limit inverts the reasoning. A job killed by its *own* cgroup ceiling is
killed by a wall that does not move, so it dies identically on every attempt.
With `job_max_attempts: 5`, one dead job becomes five full-length dead jobs,
each burning its entire runtime before hitting the same wall. That is the
failure mode the parent design objected to, multiplied by five:

> an OOM kill twenty minutes into an alignment produces a dead job with a log
> that says nothing useful

**When `BIOFLOW_HARD_MEM_MB` is set, a 137 is terminal rather than retryable**,
and the message names the cause instead of guessing at it:

> killed at the 16 GB hard limit — this job needs more memory than the limit
> allows

Two things improve at once. The user loses one job instead of five, and the log
line says the useful thing -- because with a known ceiling the cause is known,
where without one it genuinely is a guess. This is the cheapest available answer
to the parent design's main objection, which is why it belongs in the same slice
as the limit itself rather than in a follow-up.

Note the consequence for `job_timings`: these runs are recorded as failures.
Per CLAUDE.md, `_modelled()` already filters failed runs out of the predictive
fits, so an OOM-killed job's peak RSS -- the ceiling it *hit*, not what it
needed -- does not drag estimates downward. Provenance still shows the failure,
which is correct: the user should be able to see that the job died at the wall.

## The wording

The acceptance criterion is that the distinction between "will not plan to
exceed" and "cannot exceed" is stated wherever the limit is configured. That
means both states, in both places.

**Launcher, blank (default):**

> No hard cap. BioFlow will not *plan* to exceed your memory budget, but a job
> that mispredicts can still go over. Nothing is killed.

**Launcher, set to a value:**

> BioFlow *cannot* exceed 16 GB. A job that tries is killed and loses its work.
> Protects the machine; costs the job. Takes effect on restart.

**Web UI, Settings · Resources**, when a hard limit exists, next to the clamped
budget field: a statement that a hard limit of N GB is enforced and that the
admission budget cannot exceed it, with a pointer to the launcher as the place
it is changed.

"Takes effect on restart" is load-bearing in the launcher copy. The launcher
recreates the stack itself on apply, so in the normal path it is already true by
the time the user reads it -- but a user who edits `.env` by hand gets no
recreate, and the sentence is what tells them why nothing changed.

## Scope

**In scope:**

1. The launcher settings field, its blank-by-default semantics, and its copy.
2. `BIOFLOW_HARD_MEM_LIMIT` / `BIOFLOW_HARD_MEM_MB` written to `.env` by
   `settings.rs`, with `WORKER_REPLICAS` pinned to 1 when a limit is set.
3. `mem_limit` on `worker` and the env var on `api` in `docker-compose.yml`.
4. The web UI clamp in Settings · Resources, plus its copy.
5. Terminal-on-137 in `pipeline_handlers.py` when a hard limit is set.

**Out of scope:**

- **A CPU hard limit.** `cpus` is a throttle rather than a kill, so it has none
  of the "lose the job" character that makes the memory limit worth an explicit
  opt-in and careful wording -- a CPU-limited job runs slower and finishes.
  It is a separate, much smaller decision and does not need this design's
  machinery. `governor._read_cgroup_cpu()` already exists for it whenever it is
  wanted.
- **Per-subprocess `ulimit`**, which epic #7 already records as optional.
- **Any change to soft admission**, which stays the default and stays as
  designed.

This departs from the issue's "this is configuration rather than new code"
framing. That is correct as far as *enforcement* goes -- the kernel does all of
it and the governor already reads it. But the acceptance criterion about stating
the distinction cannot be met by YAML, and the clamp and the retry fix are both
real code. The alternative was shipping the compose change alone and leaving the
contradiction and the five-deaths retry loop as known traps.

## Testing

**The launcher (Rust, `cargo test`).** `settings.rs` already has
`apply_rewrites_env_and_preserves_extra_lines`, and the `env_extra` mechanism it
tests is exactly what must not break here. New cases:

- A blank limit writes no `BIOFLOW_HARD_MEM_LIMIT` line at all, rather than an
  empty assignment. Assert on absence -- an `BIOFLOW_HARD_MEM_LIMIT=` line
  would also work with Compose today, but it makes the "off" state depend on
  Compose's empty-value handling rather than on the variable being unset.
- A set limit writes both the compose-facing limit and the `_MB` value for the
  API, and pins `WORKER_REPLICAS=1`.
- Clearing a previously-set limit removes both lines and restores the replica
  default. This is the direction that regresses silently: a stale
  `BIOFLOW_HARD_MEM_LIMIT` left behind by a partial rewrite would keep enforcing
  a limit the user believes they removed.

**The clamp (`pytest`, via `./backend/run-worktree-tests.sh tests/ -q`).**
Per CLAUDE.md's warning about the passing direction proving nothing, assert the
*refusal*: with `BIOFLOW_HARD_MEM_MB` set to 16384, a soft budget of 32768 is
rejected. A test that only checks an acceptable budget still saves would pass
whether or not the clamp exists. Also assert that with no hard limit set, any
budget saves -- the clamp must not become an unconditional ceiling.

**Terminal-on-137.** Two tests, one per branch: with a hard limit set, a 137
raises a terminal error; without one, it still raises `RetryableError`. The
second is the regression guard -- the existing retry behaviour is correct on an
unlimited machine and must survive.

**One end-to-end check against a real stack, not only unit tests.** Per
CLAUDE.md's note that a green suite proved nothing for the suggestion rules,
bring up a worktree stack with a low limit via `./ops/worktree-up.sh` and
confirm two things the unit tests structurally cannot: that
`docker inspect` reports the memory limit actually applied to the worker
container, and that `governor.mem_budget_bytes()` inside that worker returns the
limit rather than host RAM. The second is the acceptance criterion that this
design claims comes for free, and "comes for free" is exactly the kind of claim
worth verifying once against the real thing.

## Follow-up

- A CPU hard limit, if wanted, as its own small issue.
- The refusal card from the parent design ([#7](https://github.com/syntheticgio/bioflow/issues/7)'s
  four-choice negotiation) has a natural extra line under hard limits: *Launch
  anyway* means something different when the kernel will kill the job regardless
  of the override. That card does not exist yet; when it is built, this
  interaction belongs in its design rather than retrofitted here.
