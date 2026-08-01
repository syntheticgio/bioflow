# DeepVariant Sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make DeepVariant a selectable variant caller, run from a separate container image pulled on first use.

**Architecture:** DeepVariant runs as a sibling container started by the worker via the host Docker daemon, not as a binary in our image. Command construction and container-to-host path translation are pure functions in `variant_runner.py`; the handler in `variant_handlers.py` dispatches to them alongside the existing Clair3 and bcftools branches. The image is pulled by its own `depends_on`-gated job so a 3 GB download is visible progress rather than a job that looks hung.

**Tech Stack:** Python 3.12, Docker CLI in the worker image, pytest.

**Spec:** `docs/superpowers/specs/2026-07-31-deepvariant-sidecar-design.md`

---

## Background the engineer needs

**Why a sidecar and not a normal tool install.** The published image is 8.83 GB
on disk. That is not model weights -- the six models total ~820 MB. It is 2.3 GB
of Python packages and TensorFlow **on Python 3.10**, plus a 490 MB `/usr/lib`.
Our backend image ships Python 3.12. Vendoring means carrying a second Python
runtime and a full TF stack for one caller, roughly doubling a 7.41 GB image.
Clair3 is not a precedent: it arrives via micromamba as a self-contained conda
package, which is why `install-clair3.sh` is one script.

**The image, verified on this machine 2026-07-31:**

```
ghcr.io/antomicblitz/deepvariant-arm64:v1.9.0-arm64.6
```

`docker manifest inspect` reports `"architecture": "arm64", "os": "linux"`.
`run_deepvariant --version` exits 0. Models are baked in at `/opt/models/{wgs,
wes,pacbio,ont_r104,hybrid_pacbio_illumina,masseq}`.

**The sharp edge -- read this before writing any code.** The worker sees storage
at `/data`. The host has it at `/Volumes/ModelExtension/BioinfoHelper`. A
sibling container started through the host's Docker daemon gets its mounts from
the *host* filesystem, not from the worker's. Passing the worker's own `/data`
path to `docker run` mounts an empty directory that happens to exist, and
DeepVariant fails "file not found" on a BAM that is plainly there.

Worse, `BIOINFO_HOME` already means both things depending on where it is read:

```yaml
# docker-compose.yml, inside the container:
BIOINFO_HOME: /data
# docker-compose.override.yml, on the host, left half of the bind mount:
- ${BIOINFO_HOME:-/Volumes/ModelExtension/BioinfoHelper}:/data
```

So the new variable must be named distinctly (`BIOINFO_HOME_HOST`) and passed
explicitly into the worker environment.

**Existing patterns to follow, not invent.**

- `variant_runner.py` holds pure functions over strings and paths --
  `build_clair3_command`, `build_bcftools_command`, `caller_for_chemistry`,
  `clair3_platform_for_chemistry`. No queue, no filesystem. New pure functions
  go here.
- `variant_handlers.py` holds the handler. `_run_clair3` and `_run_bcftools`
  are the shape `_run_deepvariant` should copy: resolve tool, build command,
  `run_subprocess(ctx, cmd, log_path=..., on_line=_progress_reporter(ctx))`,
  check exit code, verify the output file exists, `_rename_output`.
- `_model_path` (variant_handlers.py ~line 92) checks a model directory exists
  *before* the run so a missing model fails in the first second with a message
  naming the path, rather than as a traceback mid-job. Same instinct applies to
  the image check.
- Tests: `backend/tests/pipelines/test_variant_runner.py` and
  `test_variant_chemistry.py` for the pure functions.

**Run tests from the main repo root**, inside the container:
`docker compose exec api python -m pytest tests/ -q`. Never from a worktree --
CLAUDE.md explains why.

---

## File Structure

- **Modify** `backend/app/pipelines/variant_runner.py` -- `host_path_for`,
  `build_deepvariant_command`, `model_type_for_chemistry`,
  `DeepVariantParams`. Pure functions only.
- **Modify** `backend/app/config.py` -- `bioinfo_home_host`,
  `deepvariant_image`.
- **Modify** `backend/app/pipelines/tools.py` -- a `deepvariant()` probe that
  checks Docker rather than a binary on PATH.
- **Modify** `backend/app/queue/variant_handlers.py` -- `_run_deepvariant`,
  dispatch, and removal of the refusal.
- **Modify** `backend/app/services/pipeline_service.py` -- remove the second
  refusal.
- **Modify** `backend/Dockerfile` -- docker CLI in the worker image.
- **Modify** `docker-compose.override.yml` -- socket mount, `BIOINFO_HOME_HOST`.
- **Test** `backend/tests/pipelines/test_variant_runner.py`,
  `backend/tests/pipelines/test_deepvariant_paths.py` (new).

`pull_image` as its own handler is **deferred to a follow-up** (Task 9 records
why): it needs a job type, a launch path and UI, and this plan is already large.
Until then the image is pulled by the validation task and the handler fails with
an actionable message if it is absent.

---

## Task 1: Validate DeepVariant before building on it

**No code.** This runs first deliberately: it is a community port whose author
describes it as vibe coded, producing calls that end up in methods sections. If
it disagrees badly with the existing caller, that changes whether the rest of
this plan is worth executing.

**Files:** none. Record findings in the spec.

- [ ] **Step 1: Confirm the image is present**

```bash
docker image inspect ghcr.io/antomicblitz/deepvariant-arm64:v1.9.0-arm64.6 --format 'present {{.Size}}'
```

If absent:

```bash
docker pull ghcr.io/antomicblitz/deepvariant-arm64:v1.9.0-arm64.6
```

Expected: ~2.99 GB download, 8.83 GB on disk.

- [ ] **Step 2: Run the bundled quicktest**

```bash
docker run --rm ghcr.io/antomicblitz/deepvariant-arm64:v1.9.0-arm64.6 deepvariant-quicktest 2>&1 | tail -30
```

Expected: completes and reports success. If the command does not exist under
that name, find it: `docker run --rm --entrypoint sh <image> -c 'ls /opt/deepvariant/bin'`.

Note whether `AttributeError: 'MessageFactory' object has no attribute
'GetPrototype'` appears. It shows on `--version` and is believed benign; this is
the first chance to see whether it affects a real run.

- [ ] **Step 3: Call variants on a real BAM with DeepVariant**

The library has `DRR1066343.bam` (*S. cerevisiae*) with its reference. Find the
paths:

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject
async def m():
    await connect_to_mongo()
    for o in await DataObject.find({'format.kind': {'\$in': ['bam','fasta']}}).to_list():
        print(o.format.kind, o.name, o.storage.path if o.storage else None)
asyncio.run(m())" 2>&1 | grep -v '^{' | head -20
```

Then run DeepVariant against the host paths (note: `/Volumes/ModelExtension/BioinfoHelper`
is the host root, `/data` is what the container sees -- use the **host** path):

```bash
docker run --rm \
  -v /Volumes/ModelExtension/BioinfoHelper:/data \
  ghcr.io/antomicblitz/deepvariant-arm64:v1.9.0-arm64.6 \
  run_deepvariant \
    --model_type=WGS \
    --ref=/data/<reference path> \
    --reads=/data/<bam path> \
    --output_vcf=/data/tmp/dv-validation.vcf.gz \
    --num_shards=4
```

- [ ] **Step 4: Compare against the existing caller**

The same BAM already has a bcftools VCF in the library
(`DRR1066343.bcftools.vcf.gz`). Compare:

```bash
docker compose exec api bcftools stats /data/tmp/dv-validation.vcf.gz | grep -E "^SN" | head -10
docker compose exec api bcftools isec -n +2 -w1 /data/tmp/dv-validation.vcf.gz /data/<bcftools vcf path> -o /data/tmp/shared.vcf 2>&1 | tail -3
```

Record: total calls from each, how many are shared, and whether the
disagreements cluster on a contig or a variant type.

- [ ] **Step 5: Write the numbers into the spec**

Add a "Validation results" section to
`docs/superpowers/specs/2026-07-31-deepvariant-sidecar-design.md` with the
actual figures.

**If concordance is poor** (say, under 90% of bcftools calls found), stop and
report rather than continuing. That is a finding worth having, and it means this
feature should not ship as-is.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-07-31-deepvariant-sidecar-design.md
git commit -m "docs: record DeepVariant validation results"
```

---

## Task 2: Translate a container path to its host path

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/pipelines/variant_runner.py`
- Test: `backend/tests/pipelines/test_deepvariant_paths.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_deepvariant_paths.py`:

```python
"""Container-to-host path translation for sibling containers.

A container started through the host's Docker daemon gets its mounts from the
host filesystem, not from the worker's. Passing the worker's own /data path
mounts an empty directory that happens to exist -- so DeepVariant fails "file
not found" on a BAM that is plainly there. These tests pin the translation and,
more importantly, that an untranslatable path raises rather than being passed
through.
"""

import pytest

from app.errors import PermanentError
from app.pipelines import variant_runner


class TestHostPathFor:
    def test_translates_a_path_under_the_storage_root(self):
        assert variant_runner.host_path_for(
            "/data/objects/ab/abcdef.bam",
            container_root="/data",
            host_root="/Volumes/Drive/Bio",
        ) == "/Volumes/Drive/Bio/objects/ab/abcdef.bam"

    def test_translates_the_root_itself(self):
        assert variant_runner.host_path_for(
            "/data", container_root="/data", host_root="/Volumes/Drive/Bio"
        ) == "/Volumes/Drive/Bio"

    def test_accepts_a_path_object(self):
        from pathlib import Path

        assert variant_runner.host_path_for(
            Path("/data/x.bam"),
            container_root="/data",
            host_root="/Volumes/Drive/Bio",
        ) == "/Volumes/Drive/Bio/x.bam"

    def test_a_path_outside_the_root_raises(self):
        """The case that must never silently succeed. A /tmp path would mount
        an empty directory and produce a confusing 'file not found' on a file
        that exists."""
        with pytest.raises(PermanentError) as e:
            variant_runner.host_path_for(
                "/tmp/scratch.bam",
                container_root="/data",
                host_root="/Volumes/Drive/Bio",
            )
        assert "/tmp/scratch.bam" in str(e.value)

    def test_a_prefix_lookalike_is_not_translated(self):
        """`/database/x` starts with the characters of `/data` but is not under
        it. String-prefix matching would translate it and mount nothing."""
        with pytest.raises(PermanentError):
            variant_runner.host_path_for(
                "/database/x.bam",
                container_root="/data",
                host_root="/Volumes/Drive/Bio",
            )

    def test_missing_host_root_raises_with_a_fixable_message(self):
        """An unset BIOINFO_HOME_HOST must name the variable, since the fix is
        a compose edit and nothing else will hint at it."""
        with pytest.raises(PermanentError) as e:
            variant_runner.host_path_for(
                "/data/x.bam", container_root="/data", host_root=""
            )
        assert "BIOINFO_HOME_HOST" in str(e.value)
```

- [ ] **Step 2: Run it, expect failure**

```bash
docker compose exec api python -m pytest tests/pipelines/test_deepvariant_paths.py -v
```

Expected: FAIL, `AttributeError: module 'app.pipelines.variant_runner' has no attribute 'host_path_for'`

- [ ] **Step 3: Add the settings**

In `backend/app/config.py`, below `clair3_models_dir`:

```python
    # The *host* path that BIOINFO_HOME is mounted from, for starting sibling
    # containers. Distinct from BIOINFO_HOME, which is already overloaded: the
    # compose file uses it as the host path in the bind mount and sets it to
    # /data inside the container. A sibling container gets its mounts from the
    # host, so it needs this value and not the container's own view.
    # Empty when unset, which host_path_for reports as a fixable error.
    bioinfo_home_host: str = ""

    deepvariant_image: str = "ghcr.io/antomicblitz/deepvariant-arm64:v1.9.0-arm64.6"
```

- [ ] **Step 4: Write the implementation**

In `backend/app/pipelines/variant_runner.py`, add `from app.config import settings`
and `from app.errors import PermanentError` to the imports if absent, then add:

```python
def host_path_for(
    path: str | Path,
    *,
    container_root: str | None = None,
    host_root: str | None = None,
) -> str:
    """Where `path` lives on the Docker host.

    A sibling container started through the host's daemon mounts host paths, so
    the worker's own view of storage is the wrong thing to hand it. Passing
    `/data` to `docker run` mounts an empty directory that happens to exist,
    and the tool then fails "file not found" on a file that is plainly there --
    which is why anything outside the storage root raises here instead of being
    passed through hopefully.
    """
    container_root = (
        container_root if container_root is not None else str(settings.bioinfo_home)
    )
    host_root = host_root if host_root is not None else settings.bioinfo_home_host

    if not host_root:
        raise PermanentError(
            "BIOINFO_HOME_HOST is not set, so the host path for "
            f"{path} cannot be determined. Set it in docker-compose.override.yml "
            "to the same host directory BIOINFO_HOME is mounted from.",
            details={"path": str(path)},
        )

    # relative_to rather than a string prefix: `/database/x` starts with the
    # characters of `/data` without being under it, and translating it would
    # mount nothing.
    try:
        rel = Path(path).relative_to(Path(container_root))
    except ValueError:
        raise PermanentError(
            f"{path} is outside {container_root}, so it is not visible to a "
            "sibling container. Only files under the storage root can be "
            "passed to DeepVariant.",
            details={"path": str(path), "container_root": container_root},
        ) from None

    return str(Path(host_root) / rel) if str(rel) != "." else host_root
```

- [ ] **Step 5: Run the tests**

```bash
docker compose exec api python -m pytest tests/pipelines/test_deepvariant_paths.py -v
```

Expected: PASS, 6 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/config.py backend/app/pipelines/variant_runner.py backend/tests/pipelines/test_deepvariant_paths.py
git commit -m "feat: translate container paths to host paths for sibling containers"
```

---

## Task 3: Map a read chemistry to a DeepVariant model

**Files:**
- Modify: `backend/app/pipelines/variant_runner.py`
- Test: `backend/tests/pipelines/test_variant_chemistry.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_variant_chemistry.py`:

```python
class TestDeepVariantModelType:
    def test_short_reads_use_the_wgs_model(self):
        assert (
            variant_runner.model_type_for_chemistry(ReadChemistry.SHORT) == "WGS"
        )

    def test_ont_uses_the_r104_model(self):
        for chem in (ReadChemistry.ONT_SIMPLEX, ReadChemistry.ONT_DUPLEX):
            assert variant_runner.model_type_for_chemistry(chem) == "ONT_R104"

    def test_hifi_uses_the_pacbio_model(self):
        assert (
            variant_runner.model_type_for_chemistry(ReadChemistry.HIFI) == "PACBIO"
        )

    def test_unknown_falls_back_to_wgs(self):
        """Same reasoning as caller_for_chemistry: unknown means QC has not
        run, and short-read is both the common case and the safe guess."""
        assert (
            variant_runner.model_type_for_chemistry(ReadChemistry.UNKNOWN) == "WGS"
        )

    def test_clr_is_refused(self):
        """CLR's error rate defeats these models exactly as it defeats Clair3;
        a caller that returns ordinary-looking wrong calls is worse than one
        that refuses."""
        with pytest.raises(ValidationError):
            variant_runner.model_type_for_chemistry(ReadChemistry.CLR)
```

Check the imports at the top of that file already include `pytest`,
`ValidationError`, `ReadChemistry` and `variant_runner`; add whichever are
missing.

- [ ] **Step 2: Run it, expect failure**

```bash
docker compose exec api python -m pytest tests/pipelines/test_variant_chemistry.py::TestDeepVariantModelType -v
```

Expected: FAIL, no attribute `model_type_for_chemistry`

- [ ] **Step 3: Write the implementation**

In `backend/app/pipelines/variant_runner.py`, directly below
`clair3_platform_for_chemistry`:

```python
def model_type_for_chemistry(chemistry: ReadChemistry | None) -> str:
    """DeepVariant's --model_type for a chemistry.

    Mirrors `clair3_platform_for_chemistry`. The image carries six models; only
    three are reachable from a chemistry we infer. WES is a real model but
    cannot be guessed from reads -- exome capture is a property of the library
    prep, not of the signal -- so it is left to an explicit user choice rather
    than inferred wrongly.
    """
    if chemistry is ReadChemistry.CLR:
        raise ValidationError(
            "PacBio CLR reads are not suitable for variant calling: their "
            "error rate is too high for DeepVariant's models to produce "
            "reliable calls. Use HiFi/CCS reads instead.",
            details={"chemistry": chemistry.value},
        )
    if chemistry in (ReadChemistry.ONT_SIMPLEX, ReadChemistry.ONT_DUPLEX):
        return "ONT_R104"
    if chemistry is ReadChemistry.HIFI:
        return "PACBIO"
    return "WGS"
```

- [ ] **Step 4: Run the tests**

```bash
docker compose exec api python -m pytest tests/pipelines/test_variant_chemistry.py -v
```

Expected: PASS, including the pre-existing cases.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/variant_runner.py backend/tests/pipelines/test_variant_chemistry.py
git commit -m "feat: map a read chemistry to a DeepVariant model"
```

---

## Task 4: Build the docker run command

**Files:**
- Modify: `backend/app/pipelines/variant_runner.py`
- Test: `backend/tests/pipelines/test_variant_runner.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_variant_runner.py`:

```python
class TestDeepVariantCommand:
    def _cmd(self, **over):
        kwargs = dict(
            image="dv:test",
            bam=Path("/data/objects/aa/reads.bam"),
            reference=Path("/data/objects/bb/ref.fa"),
            output_vcf=Path("/data/tmp/out.vcf.gz"),
            container_root="/data",
            host_root="/HOST/bio",
            params=variant_runner.DeepVariantParams(threads=4, model_type="WGS"),
        )
        kwargs.update(over)
        return variant_runner.build_deepvariant_command(**kwargs)

    def test_mounts_the_host_root_not_the_container_root(self):
        """The whole point. Mounting /data would mount an empty directory on
        the host and fail on a file that exists."""
        cmd = self._cmd()
        assert "-v" in cmd
        assert "/HOST/bio:/data" in cmd
        assert "/data:/data" not in cmd

    def test_paths_are_passed_as_container_paths(self):
        """Inside the sibling container the mount is still at /data, so the
        tool's own arguments use container paths -- only the *mount* is
        translated."""
        cmd = self._cmd()
        joined = " ".join(cmd)
        assert "--reads=/data/objects/aa/reads.bam" in joined
        assert "--ref=/data/objects/bb/ref.fa" in joined
        assert "--output_vcf=/data/tmp/out.vcf.gz" in joined

    def test_passes_the_model_type_and_shards(self):
        cmd = self._cmd()
        joined = " ".join(cmd)
        assert "--model_type=WGS" in joined
        assert "--num_shards=4" in joined

    def test_runs_the_named_image(self):
        assert "dv:test" in self._cmd()

    def test_removes_the_container_when_done(self):
        """Without --rm a 8.8GB-image container is left behind per run."""
        assert "--rm" in self._cmd()

    def test_disables_bf16_fastmath(self):
        """Not cosmetic: without these the run dies with SIGILL inside
        TensorFlow. The image targets Graviton3 and defaults to BF16 fastmath,
        and Docker on macOS advertises bf16 in /proc/cpuinfo while faulting on
        the instruction. Measured 2026-08-01 -- a refactor that drops these
        reintroduces a crash whose message names nothing about its cause."""
        cmd = self._cmd()
        assert "DNNL_DEFAULT_FPMATH_MODE=STRICT" in cmd
        assert "TF_ENABLE_ONEDNN_OPTS=0" in cmd
        # Passed as `-e VALUE` pairs, so each must follow an -e.
        for var in ("DNNL_DEFAULT_FPMATH_MODE=STRICT", "TF_ENABLE_ONEDNN_OPTS=0"):
            assert cmd[cmd.index(var) - 1] == "-e"

    def test_a_bam_outside_the_storage_root_raises(self):
        with pytest.raises(PermanentError):
            self._cmd(bam=Path("/tmp/elsewhere.bam"))
```

Ensure that file imports `Path`, `pytest`, `PermanentError` and
`variant_runner`.

- [ ] **Step 2: Run it, expect failure**

```bash
docker compose exec api python -m pytest tests/pipelines/test_variant_runner.py::TestDeepVariantCommand -v
```

Expected: FAIL, no attribute `DeepVariantParams`

- [ ] **Step 3: Write the implementation**

In `backend/app/pipelines/variant_runner.py`, add the params dataclass beside
`Clair3Params` and `BcftoolsParams`:

```python
@dataclass
class DeepVariantParams:
    """DeepVariant invocation knobs."""

    threads: int = 4
    model_type: str = "WGS"  # {WGS, WES, PACBIO, ONT_R104, HYBRID_PACBIO_ILLUMINA, MASSEQ}

    def as_dict(self) -> dict:
        return {"threads": self.threads, "model_type": self.model_type}

    @classmethod
    def from_dict(cls, raw: dict | None) -> "DeepVariantParams":
        raw = raw or {}
        return cls(
            threads=int(raw.get("threads", 4)),
            model_type=str(raw.get("model_type", "WGS")),
        )
```

Then the command builder, below `build_bcftools_command`:

```python
def build_deepvariant_command(
    *,
    image: str,
    bam: Path,
    reference: Path,
    output_vcf: Path,
    params: DeepVariantParams,
    container_root: str | None = None,
    host_root: str | None = None,
) -> list[str]:
    """Assemble the `docker run` invocation for DeepVariant.

    Two path spaces are in play and mixing them is the failure this function
    exists to prevent. The *mount* uses the host path, because the daemon
    starting the container reads it from the host filesystem. The tool's own
    arguments stay container paths, because inside the sibling container the
    mount lands at the same place the worker sees it. So exactly one value is
    translated -- the left half of `-v` -- and everything else is passed
    through unchanged.
    """
    host_root_path = host_path_for(
        container_root if container_root is not None else str(settings.bioinfo_home),
        container_root=container_root,
        host_root=host_root,
    )
    # Validated, not used: raises for anything outside the storage root, which
    # would mount nothing and fail confusingly deep inside the tool.
    for p in (bam, reference, output_vcf):
        host_path_for(p, container_root=container_root, host_root=host_root)

    mount_at = container_root if container_root is not None else str(settings.bioinfo_home)
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{host_root_path}:{mount_at}",
        # Without these the run dies with SIGILL inside TensorFlow. The image
        # targets Graviton3 and defaults to BF16 fastmath; Docker on macOS
        # advertises `bf16` in /proc/cpuinfo but faults on the instruction.
        # Measured 2026-08-01 -- see the validation section of the design doc.
        "-e",
        "DNNL_DEFAULT_FPMATH_MODE=STRICT",
        "-e",
        "TF_ENABLE_ONEDNN_OPTS=0",
        image,
        "run_deepvariant",
        f"--model_type={params.model_type}",
        f"--ref={reference}",
        f"--reads={bam}",
        f"--output_vcf={output_vcf}",
        f"--num_shards={params.threads}",
    ]
```

- [ ] **Step 4: Run the tests**

```bash
docker compose exec api python -m pytest tests/pipelines/test_variant_runner.py -v
```

Expected: PASS, including pre-existing cases.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/variant_runner.py backend/tests/pipelines/test_variant_runner.py
git commit -m "feat: build the DeepVariant docker run command"
```

---

## Task 5: Put a docker CLI and the socket in the worker

**Files:**
- Modify: `backend/Dockerfile`
- Modify: `docker-compose.override.yml`

- [ ] **Step 1: Add the docker CLI to the image**

In `backend/Dockerfile`, near the other apt installs, add the Docker CLI *only*
(not the daemon):

```dockerfile
# The Docker CLI, for starting sibling containers -- DeepVariant runs from its
# own 8.8GB image rather than being vendored in here (see
# docs/superpowers/specs/2026-07-31-deepvariant-sidecar-design.md). The client
# alone, not docker.io: this container talks to the *host's* daemon through a
# mounted socket and must never run one of its own.
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*
```

Note: on Debian, `docker.io` pulls the daemon too. If image size matters, use
the static client binary instead:

```dockerfile
ARG DOCKER_CLI_VERSION=27.3.1
RUN set -eu; \
    case "$(uname -m)" in \
      aarch64|arm64) DARCH=aarch64 ;; \
      x86_64|amd64)  DARCH=x86_64 ;; \
      *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${DARCH}/docker-${DOCKER_CLI_VERSION}.tgz" \
      -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz
```

Prefer the static client -- it is ~50 MB against ~400 MB for `docker.io`, and
this image is already 7.41 GB.

- [ ] **Step 2: Mount the socket and pass the host path**

In `docker-compose.override.yml`, on the `worker` service, add to `volumes`:

```yaml
      # The host's Docker socket, so the worker can start sibling containers
      # for tools too large to vendor into this image (DeepVariant). This is a
      # real privilege increase -- a container that can reach the daemon can
      # start any container -- accepted here because this app is single-user
      # and local-only. See the sidecar design doc.
      - /var/run/docker.sock:/var/run/docker.sock
```

and to `environment`:

```yaml
      # The *host* path behind /data. BIOINFO_HOME is already overloaded --
      # the host path in the bind mount above, /data inside the container --
      # so a sibling container's mounts need this under a distinct name.
      BIOINFO_HOME_HOST: ${BIOINFO_HOME:-/Volumes/ModelExtension/BioinfoHelper}
```

- [ ] **Step 3: Rebuild and verify**

From the main repo root:

```bash
docker compose up -d --build worker
```

Then:

```bash
docker compose exec worker docker version --format '{{.Server.Version}}'
docker compose exec worker sh -c 'echo $BIOINFO_HOME_HOST'
```

Expected: the CLI reports the *host* daemon's version (proving the socket
works), and the env var prints the host path, not `/data`.

- [ ] **Step 4: Commit**

```bash
git add backend/Dockerfile docker-compose.override.yml
git commit -m "feat: give the worker a docker client and the host socket"
```

---

## Task 6: Probe DeepVariant by asking Docker, not PATH

**Files:**
- Modify: `backend/app/pipelines/tools.py`
- Test: `backend/tests/pipelines/test_tools.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_tools.py`:

```python
class TestDeepVariantProbe:
    def test_unavailable_when_there_is_no_docker_client(self, monkeypatch):
        """The direction that fails when the seam breaks. The image ships most
        tools as installed, so asserting availability passes whether or not a
        patch took effect -- assert the refusal instead."""
        monkeypatch.setattr(tools.shutil, "which", lambda _: None)
        tools.reset_cache()

        tool = tools.deepvariant()
        assert not tool.available
        assert "docker" in (tool.error or "").lower()

    def test_reports_the_image_reference_as_its_version(self, monkeypatch):
        """There is no binary to ask for a version, and the image tag is the
        provenance that matters -- it is what a methods section would cite."""
        monkeypatch.setattr(tools.shutil, "which", lambda _: "/usr/local/bin/docker")
        monkeypatch.setattr(
            tools.subprocess,
            "run",
            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": b"27.3.1", "stderr": b""})(),
        )
        tools.reset_cache()

        tool = tools.deepvariant()
        assert tool.available
        assert "deepvariant-arm64" in (tool.version or "")

    def test_unavailable_when_the_daemon_is_unreachable(self, monkeypatch):
        """A mounted socket that answers nothing is the compose-misconfigured
        case, and must not read as installed."""
        monkeypatch.setattr(tools.shutil, "which", lambda _: "/usr/local/bin/docker")
        monkeypatch.setattr(
            tools.subprocess,
            "run",
            lambda *a, **k: type("R", (), {"returncode": 1, "stdout": b"", "stderr": b"Cannot connect to the Docker daemon"})(),
        )
        tools.reset_cache()

        tool = tools.deepvariant()
        assert not tool.available
        assert "daemon" in (tool.error or "").lower()
```

- [ ] **Step 2: Run it, expect failure**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools.py::TestDeepVariantProbe -v
```

Expected: FAIL, no attribute `deepvariant`

- [ ] **Step 3: Write the implementation**

In `backend/app/pipelines/tools.py`, beside the other probes:

```python
@lru_cache(maxsize=1)
def deepvariant() -> Tool:
    """Whether DeepVariant can be run, which is a question about Docker.

    Unlike every other tool here there is no binary to find: DeepVariant runs
    from its own image as a sibling container, because vendoring 2.3GB of
    TensorFlow on a second Python runtime into this image to gain one caller is
    a bad trade. So the probe asks whether we can reach a Docker daemon, and
    reports the image reference as the version -- the tag is the provenance a
    methods section would cite, and there is no `--version` to ask.

    Note this result is not persisted by the probe cache: that is keyed by a
    binary's fingerprint, and there is no binary. Correct here, since image
    availability can change between runs.
    """
    client = shutil.which("docker")
    if client is None:
        return Tool(
            name="deepvariant",
            path=None,
            version=None,
            error=(
                "No docker client in this container, so DeepVariant's image "
                "cannot be run. It runs as a sibling container rather than "
                "being installed here."
            ),
        )

    try:
        proc = subprocess.run(
            [client, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return Tool(name="deepvariant", path=client, version=None, error=str(e))

    if proc.returncode != 0:
        detail = _decode(proc.stderr) or _decode(proc.stdout) or "unknown error"
        return Tool(
            name="deepvariant",
            path=client,
            version=None,
            error=f"Docker daemon is not reachable: {detail.splitlines()[0]}",
        )

    return Tool(
        name="deepvariant",
        path=client,
        version=settings.deepvariant_image.rsplit("/", 1)[-1],
    )
```

Add `deepvariant()` to the list returned by `all_tools()`, and
`deepvariant.cache_clear()` to `reset_cache()`.

- [ ] **Step 4: Run the tests**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools.py -v
```

Expected: PASS. Run the whole file -- `all_tools()` and `reset_cache()` both
changed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat: probe DeepVariant through Docker rather than PATH"
```

---

## Task 7: Run DeepVariant from the handler

**Files:**
- Modify: `backend/app/queue/variant_handlers.py`
- Modify: `backend/app/services/pipeline_service.py`

- [ ] **Step 1: Remove the handler's refusal**

In `backend/app/queue/variant_handlers.py` (~line 52), delete:

```python
    if caller is VariantCaller.DEEPVARIANT:
        raise PermanentError(
            "DeepVariant is not available in this installation: it has no "
            "arm64 Linux build. Use Clair3 for long reads, or bcftools for "
            "short reads."
        )
```

- [ ] **Step 2: Add the runner**

Below `_run_bcftools`, following its shape exactly:

```python
def _run_deepvariant(
    ctx: JobContext, bam: Path, reference: Path, out_dir: Path, log_path: Path
) -> Path:
    tool = tools.require(tools.deepvariant())

    params = variant_runner.DeepVariantParams.from_dict(
        ctx.payload.get("deepvariant_params")
    )
    vcf = out_dir / variant_runner.output_name(bam.name, "deepvariant")

    # Checked before the run, in the spirit of _model_path: a missing image is
    # a 3GB download, and discovering that from a docker error mid-job is worse
    # than being told before anything starts.
    _require_image(ctx, tool.path, settings.deepvariant_image)

    ctx.progress(phase="starting", pct=None, message="starting DeepVariant")
    cmd = variant_runner.build_deepvariant_command(
        image=settings.deepvariant_image,
        bam=bam,
        reference=reference,
        output_vcf=vcf,
        params=params,
    )
    log.info("deepvariant_started", job_id=ctx.job_id, model=params.model_type)

    code = run_subprocess(
        ctx, cmd, log_path=str(log_path), on_line=_progress_reporter(ctx)
    )
    if code != 0:
        raise _failure(code, log_path, "deepvariant")

    if not vcf.exists():
        raise RetryableError("DeepVariant exited 0 but produced no VCF")

    return vcf


def _require_image(ctx: JobContext, docker_path: str, image: str) -> None:
    """Fail early and actionably when the image is absent.

    Pulled on demand rather than baked in -- the image is 8.83GB, larger than
    the rest of this stack -- so absence is expected on a first run and must
    read as an instruction rather than a crash. When `pull_image` exists as its
    own job this becomes a dependency instead of a message.
    """
    import subprocess as _sp

    probe = _sp.run(
        [docker_path, "image", "inspect", image],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        raise PermanentError(
            f"The DeepVariant image {image} is not present. Pull it once with "
            f"`docker pull {image}` (about 3 GB), then run this again.",
            details={"image": image},
        )
```

- [ ] **Step 3: Add the dispatch**

At ~line 182, extend the branch:

```python
    if caller is VariantCaller.CLAIR3:
        vcf = _run_clair3(ctx, bam, materialized.reference, out_dir, log_path)
    elif caller is VariantCaller.DEEPVARIANT:
        vcf = _run_deepvariant(ctx, bam, materialized.reference, out_dir, log_path)
    else:
        vcf = _run_bcftools(ctx, bam, materialized.reference, out_dir, log_path)
```

And at ~line 212, where the tool is resolved for provenance:

```python
    if caller is VariantCaller.CLAIR3:
        tool = tools.clair3()
    elif caller is VariantCaller.DEEPVARIANT:
        tool = tools.deepvariant()
    else:
        tool = tools.bcftools()
```

- [ ] **Step 4: Remove the launch-path refusal**

In `backend/app/services/pipeline_service.py` (~line 1533), delete:

```python
    if merged.caller is variant_runner.VariantCaller.DEEPVARIANT:
        raise ValidationError(
            "DeepVariant is not available in this installation: it has no "
            "arm64 Linux build. Use Clair3 for long reads, or bcftools for "
            "short reads."
        )
```

and extend the `tools.require(...)` below it to resolve `tools.deepvariant()`
for the DeepVariant case, mirroring the Clair3/bcftools ternary already there.

- [ ] **Step 5: Run the full suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass. Two refusals were removed, so any test asserting them will
fail -- update those tests to assert the new behaviour rather than deleting
them.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/variant_handlers.py backend/app/services/pipeline_service.py
git commit -m "feat: run DeepVariant as a sidecar container"
```

---

## Task 8: Document the tool and let a rule pick it

**Files:**
- Modify: `backend/app/pipelines/tools.py` (`TOOL_META`)
- Modify: `backend/app/services/suggestion_service.py`

Per CLAUDE.md, registering a tool is only half the change: a tool no rule can
pick will never be suggested, and `test_every_tool_is_documented` fails until
the metadata is complete.

- [ ] **Step 1: Run the test that is now failing**

```bash
docker compose exec api python -m pytest tests/ -q -k documented
```

Expected: FAIL -- `deepvariant` has no `TOOL_META` entry.

- [ ] **Step 2: Add the metadata**

In `TOOL_META`, beside the `clair3` entry. **Verify the license against the
port's own repository rather than assuming it inherits upstream's** -- CLAUDE.md
is explicit that a wrong license claim on a page that reads as authoritative is
worse than a blank field:

```python
    "deepvariant": ToolMeta(
        summary=(
            "A deep-learning variant caller from Google. Turns the pileup at "
            "each position into an image and classifies it with a "
            "convolutional network, rather than applying a statistical model."
        ),
        pipelines=["variant"],
        homepage="https://github.com/google/deepvariant",
        repository="https://github.com/antomicblitz/deepvariant-linux-arm64",
        citation=(
            "Poplin R, et al. A universal SNP and small-indel variant caller "
            "using deep neural networks. Nat Biotechnol. 2018."
        ),
        citation_url="https://doi.org/10.1038/nbt.4235",
        license="<VERIFY against the port's repository>",
        usage=(
            "Runs as a separate container image rather than being installed "
            "in the BioFlow image, and is downloaded the first time it is "
            "used. BioFlow picks the model from the reads' inferred "
            "chemistry. This is a community port built for arm64; the "
            "upstream project publishes x86-64 only."
        ),
        strengths=[
            "Consistently high accuracy on short-read SNVs and small indels",
            "Models trained per sequencing chemistry",
        ],
        runnable=True,
    ),
```

- [ ] **Step 3: Add a suggestion rule**

In `backend/app/services/suggestion_service.py`, find the rule that recommends
a variant caller and extend it so DeepVariant can be picked when available.
Read the surrounding rules first -- this file is a hand-maintained mapping and
the shape differs between cards.

- [ ] **Step 4: Add the rule's test**

In `backend/tests/services/test_suggestion_service.py`, add a case. Per
CLAUDE.md's warning, assert the direction that *fails* when the seam breaks --
that the card goes unavailable when the probe is patched off, not that it is
available when it happens to be installed.

- [ ] **Step 5: Run both suites**

```bash
docker compose exec api python -m pytest tests/services/test_suggestion_service.py tests/ -q -k "documented or suggestion"
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/tools.py backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "docs: document DeepVariant and let a rule suggest it"
```

---

## Task 9: Verify end to end against the running app

**Files:** none, unless something fails.

- [ ] **Step 1: Full suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

- [ ] **Step 2: Rebuild and confirm the tree**

```bash
docker compose up -d --build api web worker
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

Expected: no path contains `.claude/worktrees/`.

- [ ] **Step 3: Confirm the probe reports available**

```bash
docker compose exec api python -c "
from app.pipelines.tools import deepvariant
t = deepvariant()
print(t.name, t.available, t.version or t.error)"
```

Expected: `deepvariant True deepvariant-arm64:v1.9.0-arm64.6`

- [ ] **Step 4: Confirm the Software page lists it**

Open http://localhost:5173/help/software and find the DeepVariant entry under
Variant calling. Check the version chip says the image tag and the "How BioFlow
uses it" text mentions the download.

- [ ] **Step 5: Launch a real DeepVariant run from the UI**

Select `DRR1066343.bam`, launch variant calling, choose DeepVariant, and watch
the job. This is the verification that matters -- everything before it is unit
tests over strings.

Confirm: the job starts, progress advances, a VCF lands in the library with a
`.tbi` sidecar, and its provenance names DeepVariant and the image tag.

- [ ] **Step 6: Confirm the failure path is actionable**

```bash
docker compose exec worker sh -c 'BIOINFO_HOME_HOST= python -c "
from app.pipelines.variant_runner import host_path_for
try:
    host_path_for(\"/data/x.bam\", host_root=\"\")
except Exception as e:
    print(type(e).__name__, e)"'
```

Expected: a `PermanentError` naming `BIOINFO_HOME_HOST`, not a traceback.

- [ ] **Step 7: Record the outcome in docs/TODO.md**

Per CLAUDE.md's "Closing out a TODO entry": append ` — FIXED` to the
DeepVariant heading, note what shipped, and say where the implementation
departed from this plan. In particular record that `pull_image` was deferred,
so the first run currently requires a manual `docker pull` -- and open a new
entry for it, describing the job-with-progress shape the spec argues for.

```bash
git add docs/TODO.md
git commit -m "docs: mark DeepVariant shipped, open the pull_image follow-up"
```

---

## Self-review notes

Checked against the spec:

- Sidecar rather than vendored, with the reasoning → Tasks 4, 5, 7.
- Host-path translation raising on untranslatable paths → Task 2, the
  highest-value test here.
- `BIOINFO_HOME_HOST` named distinctly to avoid the collision → Tasks 2, 5.
- Three-state availability via a Docker probe → Task 6.
- Chemistry → model mapping, CLR still refused, DeepVariant not made an
  automatic default (`caller_for_chemistry` is deliberately untouched) →
  Task 3.
- Validation before wiring → Task 1, first, with an explicit stop condition.
- `TOOL_META` completeness and a suggestion rule → Task 8.
- Both refusal messages removed → Task 7.

**One deliberate deviation from the spec:** `pull_image` as its own
`depends_on`-gated job is *not* built here. It needs a job type, a launch path
and UI, which would roughly double this plan. Instead `_require_image` fails
with an actionable message naming the exact `docker pull` command, and Task 9
opens a follow-up entry. The spec's reasoning for the job shape still stands and
is unchanged; this only defers it.

Naming is consistent across tasks: `host_path_for`, `build_deepvariant_command`,
`model_type_for_chemistry`, `DeepVariantParams`, `_run_deepvariant`,
`_require_image`, `deepvariant()`, `bioinfo_home_host`, `deepvariant_image`,
`BIOINFO_HOME_HOST`.
