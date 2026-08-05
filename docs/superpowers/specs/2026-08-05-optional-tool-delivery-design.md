# Optional tool delivery design

**Date:** 2026-08-05

**Issue:** [#26](https://github.com/syntheticgio/bioflow/issues/26), the first
slice of epic [#5](https://github.com/syntheticgio/bioflow/issues/5). Unblocks
[#40](https://github.com/syntheticgio/bioflow/issues/40).

## Goal

Let a tool be absent from the backend image and installed later, from the UI,
without a terminal. This document settles three things #5 leaves open: which
tools qualify, how an optional tool is delivered and run, and how "not
installed yet" travels from the probe through the Actions tab to a button the
user can press.

## What already exists

DeepVariant is the working precedent and roughly half the mechanism.

It runs as a **sibling container** started through the host Docker daemon,
whose socket is mounted into both `api` and `worker` in
`docker-compose.override.yml`. Its probe (`tools.deepvariant`, `tools.py`)
is special-cased: there is no binary to find, so it asks whether a Docker
daemon is reachable and reports the image tag as the version. Image presence
is checked separately at job time by `_require_image` in
`queue/variant_handlers.py`, which raises a `PermanentError` telling the user
to run `docker pull` themselves.

See `docs/superpowers/specs/2026-07-31-deepvariant-sidecar-design.md` for why
that shape was chosen.

Two properties of the existing code shape everything below.

**The probe reports availability it has not checked.** `tools.deepvariant`
returns `available=True` whenever the Docker daemon answers, whether or not
the image was ever pulled. So `suggestion_service` offers the card,
`tools.require()` passes at launch, the job is accepted, and it dies at
`_require_image` with an instruction to open a terminal. Every user-visible
piece of this design depends on the probe telling the truth first; that is why
it is slice 1 and not a cleanup at the end.

**`Job.depends_on` already exists**, with an index (`by_depends_on`) and a
blocked state that is released by its last dependency finishing rather than by
the clock. `_require_image`'s own comment anticipates using it — *"When
`pull_image` exists as its own job this becomes a dependency instead of a
message."* The plumbing for the on-demand flow is already in the queue.

## The candidate rule

Eligibility is decided by **pipe topology first, size second**. Size alone
gives the wrong answer: it would nominate tools that cannot be moved and pass
over the reason the epic exists.

A tool is **core** — baked into the image — if any of these hold:

- It is on a default path the user never chose. fastp, samtools, minimap2, and
  NanoPlot for long-read QC are reached without anyone selecting them.
- Something else depends on it regardless of what the user selected. bcftools
  indexes every VCF *including the ones Clair3 wrote*; samtools sorts every
  alignment; hmmer exists in the image only because compleasm needs it.
- **It participates in a pipe.** This is the hard constraint, not a
  preference: `aligner | samtools sort` cannot span two containers without
  restructuring the runner. A tool that must be piped stays core whatever it
  weighs.
- It is small — under roughly 100 MB installed. The download flow costs more
  than it saves below that.

A tool is **optional** only if all of these hold:

- It is a terminal step: files in, files out, no pipe.
- It is a user-selectable alternative or an opt-in workflow, never the only
  route to a capability. A capability that vanishes entirely when its tool is
  absent should not be optional.
- It is expensive — over roughly 500 MB, **or** it needs a runtime the base
  image does not otherwise carry. DeepVariant is the archetype of the second
  clause: 2.3 GB of TensorFlow on a Python 3.10 nothing else here uses.
- Upstream publishes a **pinned OCI tag**. Not a source build, not an
  unpinned apt line. A runtime install that compiles from source is a failure
  waiting for a bad network day, and it fails on the user's machine where
  nobody can debug it.

### What this rule selects today

Applied to the current inventory, not much moves, and that is worth stating
plainly rather than discovering halfway through:

| Tool | Verdict | Why |
|---|---|---|
| Clair3 | **Optional** | ~600 MB with models, the largest single Dockerfile addition; terminal (BAM + ref → VCF); selected by platform, with bcftools always available as the other route |
| FastQC + JRE | Optional, marginal | ~200 MB, mostly the JRE; the Dockerfile already notes nothing on the default path needs it, since the numbers the UI shows come from fastp's JSON |
| NanoPlot | Core | Heavy (numpy/pandas/scipy/pyarrow/plotly) but it *is* the long-read QC default path |
| PyDESeq2 | Core | Heavy, but in-process Python returning a DataFrame — there is no subprocess to move into a container |
| samtools, bcftools, minimap2, bowtie2, hisat2, STAR, fastp | Core | Piped, depended-on, or small |
| sra-toolkit, datasets | Core | The download paths depend on them unconditionally |

**The value of this epic is forward-looking, not reclaiming today's image.**
It is what makes it possible to say yes to hifiasm, Kraken2, GATK, or a 5 GB
medaka without every user paying for it. Judged only as image-size reduction
against the current tool set, this work is poor value; judged as the thing
that removes the ceiling on future tools, it is worth building.

## Delivery: images only

An optional tool is an OCI image, pulled on demand and run as a sibling
container — the DeepVariant shape, generalized. **If a tool cannot be a pinned
image, it stays core.**

The alternative considered and rejected was installing into a persistent
volume mounted on `PATH` (`/opt/bioflow-tools` or similar), which would let
apt/conda/pip tools be optional too. It was rejected because it puts mutable
state outside the image: "installed" then depends on volume contents that a
rebuild does not touch and a fresh install does not have, and the two ways a
tool can be present diverge in how they are probed, versioned, and removed.
One mechanism that is atomic, pinned, and reversible is worth more here than
covering more tools. The cost is real and accepted: a tool distributed only
through apt or conda cannot be made optional.

This also gives uninstall for free, which is the subject of its own decision
below.

### Prerequisite: the daemon socket is dev-only today

The Docker socket is mounted in `docker-compose.override.yml` and **nowhere
else** — both the `api` and `worker` mounts are there. The launcher ships
`docker-compose.yml` alone (`launcher/src-tauri/src/setup/install.rs`, and
`BUNDLED_COMPOSE_RESOURCE` in `commands.rs`) and never the override, which
exists to build from local source.

So a launcher-installed user has no socket, and **DeepVariant does not work for
them today** — not as a consequence of this design, but already. Every optional
tool would inherit that. Moving the socket mount (and `BIOINFO_HOME_HOST`, for
the same sibling-container reason) into the base compose file is therefore a
prerequisite for this epic rather than a detail of it, and it makes the
privilege increase the override's comment describes apply to the shipped stack.
That is the right trade for a single-user local application, and it should be
stated in the launcher's own documentation rather than only in a compose
comment.

## `ToolMeta` carries the manifest

`ToolMeta` in `pipelines/tools.py` already describes every tool and already
has a completeness test (`test_every_tool_is_documented`) that fails when a
new tool arrives without `homepage`, `citation`, `license`, and `usage`. That
test is the lever: extend the type and extend the test.

```python
class Delivery(StrEnum):
    BUNDLED = "bundled"              # in the image; probe by PATH
    ON_DEMAND_IMAGE = "on_demand"    # pulled; probe by image presence

# added to ToolMeta
delivery: Delivery = Delivery.BUNDLED
image: str | None = None             # pinned tag, arch-resolved
download_bytes: int | None = None    # what the Install button promises
```

`test_every_tool_is_documented` grows a clause: a tool declaring
`ON_DEMAND_IMAGE` must supply `image` and `download_bytes`. A new optional
tool that forgets either fails the suite rather than shipping an Install
button that cannot state its cost.

Keeping this in `tools.py` rather than anywhere else is also what unblocks
#40. That issue's blocker is that the launcher cannot own the optional-tool
list without recreating the drift problem it avoided by shipping compose
verbatim. With the manifest here it is served by the existing
`GET /pipelines/tools`, and the launcher queries the stack instead of
hardcoding anything.

Arch resolution stays where it already is: `config.default_deepvariant_image`
picks the image by architecture because upstream publishes x86-64 only and
arm64 needs a community port. Optional tools inherit that pattern rather than
inventing a second one.

## Probing an optional tool

The probe for `ON_DEMAND_IMAGE` asks `docker image inspect <pinned tag>` and
reports three states rather than a boolean: **installed** (with the tag as
version), **not installed** (a real, expected state — not an error), and
**cannot tell** (no Docker client, daemon unreachable), which is the only one
that is a genuine failure.

`Tool.available` stays a boolean for every existing caller, and is false for a
not-installed optional tool. The distinction between "not installed" and
"broken" rides alongside it, because those two need different words in the UI
and a different card in the Actions tab.

### Cache invalidation is the part that will bite

Probe results live in a per-process `@lru_cache`. `api` and **two `worker`
replicas** are separate processes. An install performed by a worker does not
clear the API's cache, so the button completes, the API keeps serving the
pre-install probe, and the screen still says "not installed." That reads to a
user as the Install button not working, and it will not reproduce under a
single-process test.

`tool_cache.py` already excludes `deepvariant` from Redis persistence for a
neighbouring reason — its `Tool.path` is the *docker client's* path, so a
fingerprint-keyed cache would key availability to the identity of the wrong
binary and never change when the image is pulled or removed. That exclusion
stays correct and generalizes: **fingerprinting is meaningless for every
`ON_DEMAND_IMAGE` tool**, so the whole class joins `NOT_FINGERPRINTABLE`.

Invalidation instead: the install and uninstall jobs publish the tool name on
a Redis channel, and every process clears that tool's cache entry on receipt.
A short TTL on image-presence probes is the fallback if the pubsub path proves
fiddly, but it makes the button feel broken for the length of the TTL, so it
is the backstop rather than the design.

## Install is a job

A 3–9 GB pull needs progress, cancellation, a log, and retry. The queue
provides all four and the UI already renders job progress, so install is an
`install_tool` handler (`HandlerMode.SUBPROCESS`) with payload
`{"tool": "<name>"}`, shelling to `docker pull` and parsing its progress lines
into `ctx.progress()`.

Notes on the handler:

- `JobClass.USER_INTERACTIVE` — the user pressed a button and is watching it.
  Not `COMPUTE`, which is deliberately deprioritized for multi-hour pipeline
  work and would leave a download queued behind an alignment.
- **One install per tool at a time.** Two concurrent pulls of the same image
  are wasted bandwidth at best; the handler should find an in-flight install
  for that tool and return it rather than starting a second.
- Uninstall is the same handler family with `docker image rm`, refused while
  any running job uses that tool.

## The on-demand flow

When the user launches something whose optional tool is absent, the launch
dialog states the cost up front — "DeepVariant is not installed. This will
download about 3 GB first." On confirm, the launch enqueues the install job
and the real job with `depends_on: [install_job_id]`.

One click, but an informed one. Downloading multiple gigabytes because someone
clicked Align, with no warning, is the wrong default on a metered or slow
connection; making the user return to a separate screen and re-launch after
the download is friction with nothing behind it. The confirmation is where
those meet.

`_require_image` survives as a guard rather than a user-facing message: with
the dependency satisfied the image is present, and if it is somehow not, the
job should still fail cleanly rather than emit a Docker error.

## Actions tab: a third status

`SuggestionStatus` is `AVAILABLE` or `UNAVAILABLE` today, and an uninstalled
optional tool is neither — it is not blocked, it is one click from working.
Rendering it as `UNAVAILABLE` is the worst available outcome, because the card
reads as a permanent dead end and the user never learns the tool exists.

Add `NEEDS_INSTALL`, carrying the launch payload intact plus
`{tool, download_bytes}`. The card renders as an offer rather than a refusal,
and pressing it enters the flow above.

Per CLAUDE.md, `suggestion_service.py` is a hand-maintained mapping and a new
dispatch path that no rule can pick will never be suggested however cleanly it
installs. The rules have tests in
`backend/tests/services/test_suggestion_service.py`; the case to add asserts
the card flips to `NEEDS_INSTALL` when the image probe is patched to absent —
the direction that fails when the seam breaks. Asserting the available
direction passes whether or not the patch worked, since the image ships most
tools present.

## The screen: Settings › Tools

A new Settings section, not an extension of `/help/software`.

Settings is currently a single AI page (`SettingsView.tsx` renders
`Settings · AI` and `App.tsx` routes `/settings` and `/settings/ai`), so this
introduces the section nav that a second page implies.

One row per tool, **including bundled ones**, showing name, one-liner, state,
and size:

- **Included** — bundled, with its version. No button. Listing these is what
  makes the page the answer to "why is this card greyed out," and the reason
  it is worth showing tools that have no action attached.
- **Installed** — optional and present, with the image tag as version, and an
  Uninstall button.
- **Not installed** — optional and absent, with the download size and an
  Install button.
- **Installing** — with the job's progress, and cancel.
- **Failed** — the job's error, and retry.

The drift risk this choice carries is a second place that lists tools. It is
avoided by construction: both this page and `HelpSoftware.tsx` read the same
`GET /pipelines/tools` response built from `TOOL_META`, so neither holds a
list of its own. `/help/software` stays documentation and gains no buttons.

## Uninstall

**Offered exactly when Install was** — that is, for `ON_DEMAND_IMAGE` tools
only, never for bundled ones. The symmetry is deliberate: it removes any
per-tool judgment about whether removal "makes sense."

For a bundled tool there is nothing to uninstall; it is a layer of an image,
and a button implying otherwise would be lying. For an image-delivered tool it
is one `docker image rm`, and it is the only way to reclaim 9 GB without
opening a terminal — which is the same reason the Install button exists.

Guarded: refuse while a job using that tool is running, and confirm before
proceeding.

## Scope: tools, not data

Downloadable **data** — Kraken2 databases, BUSCO lineages, VEP caches — is out
of scope here despite sharing the UX and often being larger than the tools.
Its lifecycle questions are different enough (versioning, partial downloads,
per-project relevance, and an existing separate download path in
`uniprot_handlers` and `ncbi_assembly_handlers`) that folding it in would
widen this epic without making either half better.

The Settings › Tools page should be built so a Data section can be added
beside it later without restructuring, but no part of that is built now.

## Slices

1. **`ToolMeta.delivery` + an honest DeepVariant probe + cache invalidation.**
   Nothing user-visible except that DeepVariant stops claiming availability it
   does not have. Most of #26.
2. **`install_tool` job**, plus `POST`/`DELETE /pipelines/tools/{name}/install`.
3. **Settings › Tools**, including the section nav.
4. **`NEEDS_INSTALL`** in `suggestion_service`, and the confirm-then-chain
   launch flow.
5. **Launcher prefetch (#40)** — nearly free once slice 1 lands, since the
   manifest is already served.
6. **Move Clair3 out of the image.** This one is not optional garnish: an
   abstraction designed against a single example usually fits exactly that
   example. A second citizen with different mounts, a different output layout,
   and no `--version` story is what proves the seam generalizes — and if it
   does not, the cost of finding out is lowest here.
