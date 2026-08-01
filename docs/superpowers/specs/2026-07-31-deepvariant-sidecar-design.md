# DeepVariant as an on-demand sidecar container

Makes DeepVariant a selectable variant caller, run from a separate container
image pulled the first time a user asks for it.

Addresses the DeepVariant entry in `docs/TODO.md`.

## What changed

Both refusal paths -- `backend/app/queue/variant_handlers.py` (~line 52) and
`backend/app/services/pipeline_service.py` (~line 1533) -- tell the user
DeepVariant "has no arm64 Linux build". That was true when written. It is not
true now, and a refusal that states a false reason is worse than one that
states a real constraint, because it sends the user looking for the wrong
thing.

A community port publishes a native `linux/arm64` image. Verified on this
machine, 2026-07-31:

| | |
|---|---|
| `docker manifest inspect` | `"architecture": "arm64", "os": "linux"` |
| Pull | succeeds, 2.99 GB compressed |
| On disk | **8.83 GB** |
| `run_deepvariant --version` | exits 0 |
| Models baked in | all six (wgs, wes, pacbio, ont_r104, hybrid, masseq), ~820 MB |

Source: https://github.com/antomicblitz/deepvariant-linux-arm64
Image: `ghcr.io/antomicblitz/deepvariant-arm64:v1.9.0-arm64.6`

## Why a sidecar rather than vendoring it into our image

The obvious move -- copy the binaries and models in with a multi-stage build,
as every other tool is installed -- does not survive looking at what is in the
image. The 8.83 GB is not weights. It is 2.3 GB of Python packages and
TensorFlow **on Python 3.10**, plus a 490 MB `/usr/lib`. Our backend image
ships Python 3.12.

So vendoring means carrying a second Python runtime and a full TensorFlow stack
for one caller, roughly doubling a 7.41 GB image. Clair3 is not a precedent for
this: it arrives via micromamba as a self-contained conda package, which is why
`install-clair3.sh` works as a single script.

Running the published image as a sibling container keeps our image at its
current size, and keeps their build *theirs* -- we are not maintaining a
TensorFlow toolchain we do not otherwise need.

### The cost, stated plainly

The worker gains a `docker` binary (~50 MB, absent today) and a mount of
`/var/run/docker.sock`. A container that can talk to the host's Docker daemon
can start any container, so this is a real privilege increase.

For this application it is an acceptable one: it is single-user, local-only,
non-critical by CLAUDE.md's own framing, and the worker already runs as root
inside its container. It is worth writing down anyway, because it is the kind
of thing that is easy to add and hard to notice later.

## The sharp edge: container paths are not host paths

This is the part that will silently produce a wrong result if it is got wrong,
so it gets its own section.

The worker sees storage at `/data`. The host has it at
`/Volumes/ModelExtension/BioinfoHelper`. A sibling container started via the
host's Docker daemon is **not** a child of the worker -- it gets its mounts
from the host filesystem. Passing the worker's own `/data` path to `docker run`
mounts an *empty* directory that happens to exist, and DeepVariant fails with a
"file not found" on a BAM that is plainly there.

So the design needs a host-path mapping:

- A new setting, `bioinfo_home_host`, carries the host path.
  `docker-compose.override.yml` already knows it -- it is the left half of the
  existing bind mount -- so it is set once there, not discovered at runtime.

  **Mind the name collision.** `BIOINFO_HOME` already means two different
  things depending on where it is read: on the compose *host* it is the host
  path (`${BIOINFO_HOME:-/Volumes/ModelExtension/BioinfoHelper}:/data`, in both
  `docker-compose.yml` and the override), while inside the container
  `docker-compose.yml` sets `BIOINFO_HOME: /data`. The new variable therefore
  needs a distinct name -- `BIOINFO_HOME_HOST` -- passed explicitly into the
  worker's environment as `${BIOINFO_HOME:-/Volumes/...}`, so the container
  receives the *host* value under a name that cannot be confused with the
  container path it already has.
- A pure function translates a container path to its host equivalent.
- It **raises** for any path not under `BIOINFO_HOME` rather than returning
  something plausible. A path that cannot be translated must stop the job, not
  produce a mount that silently resolves to nothing.

That function is the highest-value test in this feature, because its failure
mode is a confusing error at best and an empty VCF at worst.

## Pulling on demand

The image is not pulled at build time. It is pulled the first time a user
launches a DeepVariant run.

This keeps 8.83 GB off the disk of anyone who never uses it, which is most
users -- Clair3 and bcftools cover the existing chemistries.

**The pull is its own job, not a step inside the calling job.** A new
`pull_image` handler, enqueued and gated with `depends_on`, exactly as
`build_index` gates `align_reads` today. Three reasons this shape and not a
silent fetch:

- A 3 GB download inside a calling job makes the job look hung. As its own job
  it reports progress -- "Downloading DeepVariant, 1.2 of 3.0 GB" -- against
  machinery that already exists.
- A failed pull names itself as the reason, instead of surfacing as a variant
  caller that mysteriously did not start.
- It is cancellable through the existing cancel path.

If the image is already present, the job completes immediately.

`install-clair3.sh` argues models should be baked in because "a variant calling
job that has to download half a gigabyte before it starts is a job that fails
when the network is down, and this application is meant to run on a laptop that
may not have one." That reasoning still holds, and this design does not
dismiss it -- it answers it in two places. The pull happens *before* the run
rather than during it, so a network failure is a clear message and not a
half-finished analysis. And the planned helper installer gains a "download
optional tools now" option (recorded in `docs/TODO.md`), so a user who knows
they will be offline resolves it once, while online, at install time.

## Availability becomes three-state

Every other tool in `tools.py` is installed or not. DeepVariant is:

- **installed** -- image present locally, runs now
- **pullable** -- Docker reachable, image absent, will download on first use
- **unavailable** -- no Docker socket or no `docker` binary

The probe therefore checks for the docker CLI and a reachable socket, not for a
`deepvariant` binary on PATH. `tools.deepvariant()` reports available when we
*can* run it, so `require()` and the Actions card work unchanged -- but the UI
should say "will download on first use (~3 GB)" rather than presenting it as
identical to an installed tool. Presenting a 3 GB download as an ordinary
launch is the kind of surprise this file's other entries exist to prevent.

Note the tool-probe cache added on 2026-07-31 stores results keyed by a binary
fingerprint. A tool with no binary has no fingerprint, so DeepVariant's probe
result is not persisted -- correct here, since image presence can change
between runs.

## Caller selection

`caller_for_chemistry` currently routes ONT and HiFi to Clair3, everything else
to bcftools, and refuses CLR outright.

DeepVariant's models cover WGS, WES, PACBIO, ONT_R104, HYBRID and MASSEQ, so it
is a legitimate choice for most of those. **It does not become the automatic
default for any chemistry.** Clair3 is installed, already validated against a
real ONT run, and needs no download. DeepVariant becomes selectable in the
launch dialog, chosen explicitly.

CLR stays refused, for the reason already recorded: at CLR's error rate these
callers produce calls that look ordinary and are wrong.

A `model_type` parameter maps chemistry to DeepVariant's model, mirroring
`clair3_platform_for_chemistry`. Short reads → WGS (or WES when the user says
so); ONT → ONT_R104; HiFi → PACBIO.

## Validation before wiring

This is a community port whose author describes it as vibe coded, producing
calls that end up in methods sections. It gets checked before the UI offers it.

1. Run the image's bundled `deepvariant-quicktest`.
2. Call variants on a real BAM already in the library (`DRR1066343.bam`,
   *S. cerevisiae*) with DeepVariant and with the existing caller.
3. Compare concordance -- shared calls, calls unique to each, and whether the
   disagreements cluster anywhere suspicious.

The numbers go into this document when they exist. If concordance is poor,
that is a finding worth recording either way, and the feature does not ship on
the assumption that it is fine.

Two things to watch during that run, both currently unknown:

- `run_deepvariant --version` prints
  `AttributeError: 'MessageFactory' object has no attribute 'GetPrototype'` and
  still exits 0. It appears to be benign protobuf-version noise, but it has
  only been observed on `--version`, never on a real calling run.
- Whether a container started from the worker inherits enough memory for a
  WGS-scale run, and what happens at the boundary. DeepVariant is memory-hungry
  and this would be the first tool here whose resource limits are set outside
  our own `JobResources` accounting.

## Validation results (2026-08-01)

Run against `DRR1066343.bam` (*S. cerevisiae*, 1.37 GB, aligned with bwa-mem2
to `GCF_000146045.2_R64`), compared with the bcftools VCF already in the
library for the same BAM.

### It crashes out of the box. Two environment variables fix it.

The first run died with **`Fatal Python error: Illegal instruction`** (SIGILL,
exit 252), inside TensorFlow eager execution at the moment `call_variants` ran
inference. `make_examples` had already succeeded -- it read the BAM and found
candidates across all 17 contigs -- so this is not an input, path or mount
problem.

The cause is BF16. The image sets `DNNL_DEFAULT_FPMATH_MODE=BF16` and
`TF_ENABLE_ONEDNN_OPTS=1`, because the port targets Graviton3, where BF16 is
real. Under Docker on macOS the guest *advertises* `bf16` and `i8mm` in
`/proc/cpuinfo`, and the M3 Ultra host reports `hw.optional.arm.FEAT_BF16: 1`
-- but the instruction faults when actually executed. A feature bit exposed
through virtualisation without being fully implemented.

**The fix, verified:**

```
DNNL_DEFAULT_FPMATH_MODE=STRICT
TF_ENABLE_ONEDNN_OPTS=0
```

With both set, the same command exits 0 and produces a complete VCF (9,704
records, with a `.tbi`). `build_deepvariant_command` **must** pass both with
`-e`. Without them DeepVariant is unusable on this machine, and the failure is
a SIGILL deep inside TensorFlow that names nothing about its own cause -- so
this is not a detail that can be left to be rediscovered.

### Concordance

The raw record counts mislead, and it is worth writing down why, because the
first reading of them nearly failed this feature. DeepVariant emitted 9,704
records against bcftools' 6,641, and only 4,811 of bcftools' calls appeared in
DeepVariant's output at all -- 72%, which looks like a broken caller.

It is an artefact of comparing unfiltered outputs:

- **4,275 of DeepVariant's 4,893 "unique" records are `RefCall`** -- it
  explicitly asserting *this position is reference*. Only 618 were real PASS
  calls. bcftools does not emit these at all.
- **1,381 of bcftools' 1,830 unique calls are QUAL<20**, low-confidence calls
  DeepVariant declined to make.

Comparing what each caller actually asserts -- DeepVariant `PASS` against
bcftools `QUAL>=20`:

| | |
|---|---|
| DeepVariant PASS | 5,115 |
| bcftools QUAL>=20 | 4,861 |
| **Shared** | **4,318** |
| bcftools only | 543 |
| DeepVariant only | 797 |

**88.8% of bcftools' high-confidence calls are confirmed by DeepVariant**, and
DeepVariant finds 797 more. The disagreement is weighted toward indels (2,201
called against bcftools' 484), which is the documented difference between a
deep-learning caller and a pileup-based one rather than evidence of a bad port.

Verdict: **usable**, with the BF16 fix mandatory. It stays an explicit user
choice rather than an automatic default, as this design already says.

## Structure

Following `variant_runner.py`'s existing split -- pure functions over strings
and paths, no queue or filesystem -- so the parts worth testing are testable
without Docker:

- `build_deepvariant_command(...)` returns the complete `docker run` argv as a
  list of strings: mounts, image ref, `--model_type`, `--ref`, `--reads`,
  `--output_vcf`, `--num_shards`. Pure, mirroring `build_clair3_command`.
- `host_path_for(container_path)` -- the translation above, raising on
  anything outside `BIOINFO_HOME`.
- `model_type_for_chemistry(chemistry)` -- mirroring
  `clair3_platform_for_chemistry`.
- The handler in `variant_handlers.py` dispatches to it alongside the existing
  Clair3 and bcftools branches, and its refusal is replaced.

Per CLAUDE.md: `TOOL_META` needs `homepage`, `citation`, `license` and `usage`
or `test_every_tool_is_documented` fails. Cite Google's DeepVariant paper for
the method, but state accurately that this is a community arm64 port and check
the port's own license rather than assuming it inherits upstream's.
`suggestion_service.py` needs a rule that can pick it, or the card never
lights up however cleanly the tool installs.

## Touches

`backend/app/pipelines/variant_runner.py`,
`backend/app/queue/variant_handlers.py`,
`backend/app/services/pipeline_service.py`,
`backend/app/pipelines/tools.py`, `backend/app/config.py`,
`backend/app/services/suggestion_service.py`,
`backend/app/queue/` (new `pull_image` handler), `backend/Dockerfile`
(docker CLI), `docker-compose.override.yml` (socket mount,
`BIOINFO_HOME_HOST`), and the launch dialog alongside the existing caller
choice.
