# Shared storage for compute nodes over SMB — scoping design

Date: 2026-08-25.

Scopes [#843](https://github.com/syntheticgio/bioflow/issues/843), "Shared
storage for compute nodes over SMB".

#843 is an **epic**. This document settles the decisions its seven children
share — the protocol, the verification mechanism, the trust model, and the
order — so each child spec can cite them rather than re-deciding. It
deliberately does not specify mount flags, endpoint shapes, or UI copy; that is
each child spec's job.

It exists because [#835](https://github.com/syntheticgio/bioflow/issues/835)
(split-by-reference alignment) cannot be built honestly until it does.

## Why this blocks #835

`align_reads_chunked` fans out one `align_reads` sub-job per reference bucket.
Those sub-jobs are enqueued with no `target_node`
(`queue/chunked_align_handlers.py:56`), so they land in the global ready queue
and **any node may claim one**. That is correct only if every node reads the
same `BIOINFO_HOME`.

Nothing establishes that it does. `docker-compose.child-node.yml` gives each
node its own `/data` bind mount, and
`docs/superpowers/specs/2026-08-10-multi-node-design.md:69-72` is explicit that
cross-node data transfer was deferred to a Phase 2 that was never built.

So the current state is not "multi-node fan-out works". It is "multi-node
fan-out works if you happened to configure shared storage by hand, and fails
hours in with `Input reads not found` if you did not". #835 would build node
placement on top of an assumption the system never checks.

The epic's job is to make shared storage a **recorded, verified fact** the
scheduler can read.

## What exists today

Verified against this worktree on 2026-08-25:

- **`_provision_node`** (`api/v1/nodes.py:671`) runs nine phases:
  `validate_ssh`, `verify_docker`, `setup_install`, `write_env`, `install_key`,
  `pull_image`, `start_worker`, `verify`, `enrolled`. `NodeProvisionTask.phase`
  is a **free-form string with no enum** (`models/node_provision.py:18`), and
  the frontend prettifies it mechanically (`SettingsNodes.tsx:423-425`), so a
  new phase costs no model change and no frontend change.
- **The `#803` refusal is the precedent for everything here.**
  `UnroutablePrimaryHost` (`nodes.py:280`) is raised when discovery produces an
  address no other machine can reach, and caught at `:742-751` with the comment
  that states the principle: an unreachable address "produces a worker that
  starts, crash-loops against the wrong Mongo, and never enrolls -- while every
  step provisioning checks reports success". Unshared storage is the same
  failure shape and earns the same treatment.
- **`sentinel_path`** (`config.py:632-634`) is
  `$BIOINFO_HOME/.biopipe/VERSION`, and its docstring already states its purpose:
  "Proves the drive is actually mounted." A mount check has a home; it does not
  need a new mechanism invented for it.
- **`Node` has no `storage_location` field** (`models/node.py`). The value the
  user types into `ProvisionRequest.storage_location` (`nodes.py:47`, no
  validator) is rendered into the node's `.env` (`_render_node_env:474-475`)
  and then forgotten. The primary has no record of where any node's storage
  lives.
- **Revocation cleans up nothing.** `revoke_node` (`nodes.py:1073-1086`) flips
  `status = "revoked"` and saves. It does not touch the remote machine, wipe
  `ssh_key_enc`, or remove anything installed. There is no decommission
  endpoint.
- **There is no sudo anywhere in `backend/` or `ops/`.** Provisioning runs
  entirely as the unprivileged SSH user. `verify_docker` (`:768-777`) does not
  install Docker — it checks, and refuses with "Install Docker first". That is
  the established posture for root-requiring setup.
- **`BIOINFO_HOME` means two different things** either side of the container
  boundary: the host path in `.env`, hardcoded `/data` inside the container
  (`nodes.py:434`), with `BIOINFO_HOME_HOST` (`:436`) carrying the host value
  across for sibling containers started via the mounted Docker socket
  (`config.py:299-305`).

## Decision S1: SMB, not NFS

NFS is the reflex answer for a shared POSIX filesystem. It is the wrong one
here, for three reasons that compound.

**It does not authenticate.** NFSv3 exports to an IP range and trusts the
network. Setting that up on a user's behalf means opening their data to
anything that can reach the port — a decision an install wizard should not make
quietly. SMB requires a credential, which makes the same automation defensible.

**It leaks the UID problem into the data.** NFS passes numeric UIDs and
resolves them against each machine's own `/etc/passwd`; `root_squash` then maps
remote root to `nobody`, which is the single most common "NFS doesn't work"
experience. SMB's `uid=`/`gid=` mount options present every file as owned by a
chosen UID locally regardless of what the server stored. For containers running
as a known UID this is exactly right, and the whole class of identity bugs
disappears.

**macOS ships SMB.** `sharing` and `smbd` are stock binaries. The maintainer's
primary is macOS with `BIOINFO_HOME` on an external volume
(`docker-compose.yml:23`). An NFS server on macOS is possible and unpleasant;
SMB is the path the platform expects.

The cost is a credential to generate, store, and distribute. That is real work
— it is most of #847 — but it buys a defensible security posture rather than
an indefensible one.

**Consequence for the children:** every child assumes SMB. A future NFS option
would be additive, not a rework, provided the probe (S2) stays
protocol-agnostic.

## Decision S2: a round-trip probe is the only acceptable verification

Every child that claims storage is shared must prove it by writing on one
machine and reading on the other. Nothing weaker counts.

The alternatives all pass for the case that actually fails. Comparing
`storage_location` against the primary's `BIOINFO_HOME` passes when both
machines have an identically-named local directory — the exact silent failure
this epic exists to remove. Checking the node's mount table passes when
something is mounted there, not when *the right thing* is. Trusting `mount`'s
exit status passes for a successful mount of the wrong export.

So the probe writes content the other side cannot produce independently, and
reads it back. A per-probe nonce, not a static file: two machines that each ran
BioFlow's own initialisation will both have a `.biopipe/VERSION`, and if its
contents are derived from the version rather than randomly, a byte-comparison
of two independent homes would match. #844 must check this and choose
accordingly.

**Consequence for the children:** #844 owns the probe. #846, #848 and #849 all
call it rather than reimplementing a check, and none of them may report success
on any other basis. In particular #848's mount step is verified by the probe,
not by `mount` returning 0.

## Decision S3: storage status is tri-state, and unknown is not false

`Node.storage_shared` needs three states — shared, not shared, and **unknown**
— not a boolean.

A boolean forces every node enrolled before the field existed to read as one of
the two real answers, and both are wrong. Reading them as shared blesses an
unverified node, which is the failure being removed. Reading them as not-shared
silently stops a working deployment: the maintainer's nodes have been running
filesystem-dependent jobs correctly against hand-configured storage, and would
stop at the moment #845 merges.

Unknown is what makes #846 (migration) expressible at all — it is the state
that says "nobody has asked yet", which is the only true thing to say about a
pre-existing node.

**Enforcement still treats unknown as not-shared** (#845), because the safe
direction under uncertainty is to withhold work. The distinction is not about
what unknown *permits*; it is about being able to find those nodes and probe
them rather than having overwritten the fact that they were never checked.

**Consequence for the children:** #844 defines the tri-state. #845 enforces
unknown conservatively. #846 exists to drain unknown to a real answer, and must
land in the same release as #845 or before it — see S6.

## Decision S4: a node with unshared storage still enrols

An unshared node is not a broken node. It cannot read the primary's files, but
it can still run work that fetches its own inputs — SRA downloads, NCBI
assembly fetches — which is real capability worth keeping.

So the epic does not refuse such a node outright. It records the fact and lets
#845 withhold the specific work the node cannot do.

This is a deliberate departure from the `#803` precedent, which *does* refuse.
The difference is that an unroutable primary address leaves a node able to do
nothing at all, whereas unshared storage leaves it able to do a useful subset.
Refusing would trade a working capability for a simpler rule.

**Settled by #844 (2026-08-25):** provisioning **fails by default** on an
unshared probe, with an explicit `allow_unshared_storage` opt-in to enrol
anyway. Both halves hold — fail loudly, let the user say "enrol anyway" —
which keeps the epic's "fails with a remedy" criterion without discarding the
capability an unshared node still has. #844 also distinguishes a probe that
*cannot run* (timeout, primary cannot write) from one that runs and answers
false: the former raises rather than recording `false`, so an infrastructure
fault is never mistaken for a verified negative.

## Decision S5: Linux nodes only

macOS nodes mount with `mount_smbfs`, have no `/etc/fstab`, and need a
LaunchAgent or `autofs` for persistence. That is a second client implementation
end to end, not a flag on the first.

`docker-compose.child-node.yml` already assumes Linux, so this narrows nothing
that currently works.

**Consequence for the children:** #848 and #849 target Linux and must refuse an
unsupported platform explicitly rather than half-configuring it. A macOS-node
ticket is a clean follow-on once the Linux path is proven.

## Decision S6: probe before automation, and the order is load-bearing

The children split into two tiers:

**Tier 1 — make the current state honest.** #844 (probe and record), #845
(enforce), #846 (migrate). None of these set up storage; they establish what is
true about it and act accordingly. Landing only these leaves a system where
hand-configured shared storage works, unshared storage is caught at
provisioning instead of hours into a job, and #835 has the fact it needs.

**Tier 2 — set it up for the user.** #847 (share on the primary), #848 (mount
on the node), #849 (helper script), #850 (teardown). Convenience over a
capability that already works.

Tier 1 first, because **the probe is the automation's own success check**.
#848's mount is only complete when the probe passes; #849's script exits
non-zero on a failed probe. Building the mount machinery first means building
it against a verification that does not exist yet, and having no way to tell a
working mount from a plausible one.

There is a second reason. Tier 1 is small and its requirements are clear. Tier
2 contains the two genuinely hard problems in the epic — cross-process
credential handoff (#847) and privilege escalation (#848) — and both are easier
to specify once a real node has been hand-mounted and #835's fan-out has been
run across it. Sequencing that way means those designs answer questions
observed rather than imagined.

**Within tier 1, #846 must not land after #845.** S3 explains why: #845 makes
unknown behave as not-shared, which stops the maintainer's working nodes until
#846 has drained them to a real answer. Either #846 merges first, or the two
merge together.

## Decision S7: sudo is new surface, and it is #848's central problem

There is no sudo anywhere in the codebase. Mounting CIFS, installing
`cifs-utils`, and editing `/etc/fstab` all require root.

The precedent-following answer would be to refuse, the way `verify_docker`
refuses to install Docker. The maintainer has decided instead to **add sudo
support to provisioning**. That is a legitimate call — it is the difference
between one-click node setup and a documented manual procedure — but it is not
a detail of mounting. It is a new credential path that will affect every future
provisioning feature.

So #848 is not "run some mount commands". It is a security design that must
settle, at minimum: whether the credential is ever persisted (contrast
`ssh_key_enc`, which *is* Fernet-encrypted and stored because updates need it
later); how it reaches `sudo` without appearing in the node's process list; and
whether the sudo channel is scoped to named commands or unbounded.

**Consequence for the children:** #848 carries this and must state residual
risk plainly rather than reassuringly. #850 inherits the tension — if the
credential is not persisted, teardown has nothing to authenticate with, and
that must be resolved rather than discovered.

## Child issues

| # | Child | Tier | Depends on | Why this order |
|---|---|---|---|---|
| [#844](https://github.com/syntheticgio/bioflow/issues/844) | Probe and record shared storage | 1 | — | Everything else calls it. Defines the tri-state (S3) and owns the probe (S2). |
| [#845](https://github.com/syntheticgio/bioflow/issues/845) | Exclude non-shared nodes from FS-dependent work | 1 | #844 | Makes the flag mean something. Carries the job-type classification. |
| [#846](https://github.com/syntheticgio/bioflow/issues/846) | Migrate existing nodes | 1 | #844 | Must not land after #845 (S3, S6). |
| [#847](https://github.com/syntheticgio/bioflow/issues/847) | Configure the share from the launcher | 2 | #844 | Cross-process credential handoff is its crux, not the sharing command. |
| [#848](https://github.com/syntheticgio/bioflow/issues/848) | Mount on a Linux node during provisioning | 2 | #844, #847 | Carries the sudo design (S7). Verified by #844's probe, never by `mount`'s exit code. |
| [#849](https://github.com/syntheticgio/bioflow/issues/849) | Helper script outside the launcher | 2 | #844 | The manual path to the same verified state. |
| [#850](https://github.com/syntheticgio/bioflow/issues/850) | Tear down the mount on node removal | 2 | #848 | Inherits S7's credential tension. |

Tier 1 alone is a coherent stopping point: storage becomes a checked fact, bad
configuration is caught at provisioning, and #835 is unblocked. That property
is worth protecting — the epic can stop after #846 without leaving anything
half-built.

## Open questions (for the child specs, not settled here)

1. ~~**Does `.biopipe/VERSION` have content that differs between two
   independently initialised homes?**~~ **SETTLED (#844, 2026-08-25): no — it is
   a fixed constant, so the probe needs its own nonce.** `SENTINEL_CONTENT` is
   the literal `"biopipe-home-v1\n"` (`storage/home.py:26`), written verbatim on
   every home that has ever been initialised. Two machines that each ran
   `initialize_home()` against their own local `/data/scratch` hold
   byte-identical `.biopipe/VERSION` files, so a comparison would report shared
   for precisely the case this epic exists to catch. #844's probe instead writes
   `$BIOINFO_HOME/.biopipe/probe-<token>` containing that same token, so path
   *and* content must both coincide. `.biopipe/VERSION` keeps its job: it is a
   mount check, and a correct one — it is simply not an identity check.
2. ~~**Does `IoClass` already encode filesystem-dependence?**~~ **SETTLED
   (#845, 2026-08-25): no, and it cannot be made to.** The counterexample is
   exact: `download_sra_run` is declared `IoClass.HEAVY`
   (`queue/sra_handlers.py:57`) and is *the* job a non-shared node must keep
   running. `IoClass` measures disk throughput for a concurrency cap
   (`models/job.py:60-62`, sole consumer the `io_heavy` counter); filesystem
   dependence is whether the bytes already exist under the primary's
   `BIOINFO_HOME`. A download writes heavily and reads nothing. The properties
   are orthogonal, so deriving one from the other mis-classifies both ways.
   #845 therefore adds `queue/storage_dependence.py` — CLAUDE.md's *first*
   kind (genuinely derivable), inverted so the default is dependent and the
   forgetting direction is safe.

3. ~~**`.biopipe/lock` over SMB.**~~ **SETTLED (#848, 2026-08-25): not a
   blocker, but the lock stops meaning what it says.** Its only consumer,
   `_acquire_lock` (`storage/home.py:129`), already degrades a `flock` failure
   to a warning. The real problem is that this epic makes a shared home the
   *intended* state, and `flock` over CIFS is often only locally scoped — so
   every node would acquire "the" exclusive lock and the warning would never
   fire. #848 has compute nodes skip the lock and stops the docstring promising
   mutual exclusion.

4. ~~**Does the share require creating a system user?**~~ **SETTLED (#847,
   2026-08-25): yes, and it is that ticket's largest hidden cost.** Unavoidable
   on both platforms: macOS `smbd` authenticates against Open Directory and
   `sharing` cannot create a share-only user; Linux `smbpasswd -a` requires the
   Unix account to exist first. So #847 is honestly "create a system account
   and share a folder to it", not "share a folder". #847 proposes a hardened
   non-login `bioflow-share` account, disclosed by name and deleted on disable,
   **and records a smaller fallback** — detection-and-instruction only — if
   account creation is rejected. **This one is the user's call.**

   #847 also found that the reference command in #843's own body ships a
   **guest-accessible** share: `sharing`'s guest access defaults on, verified
   against this machine. `-g 000` is mandatory and the issue omitted it.

5. ~~**Is the SMB mountpoint visible to the node's Docker *daemon*?**~~
   **SETTLED (#848, 2026-08-25): it must be, and it is verified through a
   sibling container rather than over SSH.** #848 probes with
   `docker run --rm -v <storage_location>:/probe <backend-image> cat
   /probe/.biopipe/...`, because a namespace mismatch lets the worker read the
   data while DeepVariant fails — exactly the failure `BIOINFO_HOME_HOST`
   (`config.py:299-305`) exists to make explicit.

## Out of scope for this epic

- **Node-to-node data transfer.** The multi-node design's unbuilt Phase 2. This
  epic makes a shared filesystem work and records when one is absent; it does
  not move bytes between machines.
- **macOS nodes** (S5).
- **NFS, or any second protocol** (S1). The probe stays protocol-agnostic so
  one could be added later.
- **General node decommissioning.** #850 removes what #848 installed.
  Revocation's broader cleanup gap — the un-wiped `ssh_key_enc`, the
  `authorized_keys` line, `~/.bioflow`, the running containers — is a real
  finding but a separate issue.
- **#835 itself.** Bucket policy, node-aware placement, sequential degradation
  and generalising beyond alignment all wait on this epic and are specified
  there.
