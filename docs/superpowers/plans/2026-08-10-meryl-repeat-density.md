# Meryl k-mer Repeat Density and Frequency Spectra — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose meryl as a first-class pipeline tool producing per-window repeat-density facts and k-mer frequency spectra on assemblies, via an Actions tab card.

**Architecture:** A new `meryl_runner.py` (pure functions) builds meryl commands and computes repeat density / parses k-mer histograms. A new handler in `assembly_qc_handlers.py` runs `meryl count` on the assembly, caches the DB as a `MERYL_ASSEMBLY_DB` sidecar, then calls the runner's compute functions. The result applier merges facts onto the assembly; a suggestion card in `suggestion_service.py` makes it invokable from the Actions tab. The windowing scheme and fact shape reuse `gc_tracks.py`'s pattern.

**Tech Stack:** Python, meryl 1.4.2 (already installed at `/opt/meryl/bin/meryl`), MongoDB (facts + sidecars)

## Global Constraints

- Window scheme from `gc_tracks.py`: `WINDOW_COUNT=500`, `MIN_WINDOW_BASES=100`, `MAX_STORED_CONTIGS=50`
- `None` in density arrays means unassessed, never `0`
- `repeat_density_partial` flag when >50 contigs dropped
- `k` is user-configurable, default `k=21`
- k-mer canonicalization: lexicographically smaller of k-mer and its reverse complement (matches meryl's internal canonicalization)
- `docker compose restart worker` after touching any handler
- Follow `quast_runner.py` / `gc_tracks.py` pattern: pure functions, testable without a binary
- `HandlerMode.SUBPROCESS` for the handler (runs meryl shell commands)

---

### Task 1: Add `MERYL_ASSEMBLY_DB` sidecar role

**Files:**
- Modify: `backend/app/models/object.py:169` — one line after the existing `MERYL_DB`

**Interfaces:**
- Produces: `SidecarRole.MERYL_ASSEMBLY_DB = "meryl-assembly-db"`

- [ ] **Step 1: Add the new role**

```python
# In object.py, after line 169:
MERYL_ASSEMBLY_DB = "meryl-assembly-db"
```

- [ ] **Step 2: Verify it is importable**

Run: `docker compose exec api python -c "from app.models.object import SidecarRole; print(SidecarRole.MERYL_ASSEMBLY_DB)"`
Expected: `SidecarRole.MERYL_ASSEMBLY_DB` prints without error

- [ ] **Step 3: Commit**

```bash
git add backend/app/models/object.py
git commit -m "feat(models): add MERYL_ASSEMBLY_DB sidecar role for assembly-side meryl databases"
```

---

### Task 2: Create `meryl_runner.py` with command builders and parsers

**Files:**
- Create: `backend/app/pipelines/meryl_runner.py`

**Interfaces:**
- Produces:
  - `build_meryl_count_command(*, meryl_path: str, k: int, assembly: Path, output: Path, threads: int = 4) -> list[str]`
  - `build_meryl_histogram_command(*, meryl_path: str, database: Path) -> list[str]`
  - `build_meryl_print_repetitive_command(*, meryl_path: str, database: Path, min_count: int) -> list[str]`
  - `build_meryl_statistics_command(*, meryl_path: str, database: Path) -> list[str]`
  - `compute_kmer_spectrum(histogram_text: str, stats_text: str, k: int) -> dict`
  - `compute_repeat_density(path: Path, compression: Compression, repetitive_kmers: set[str], k: int, *, cancel_event: threading.Event | None = None) -> dict`

- [ ] **Step 1: Write the module**

```python
"""Command builders and output parsers for meryl k-mer repeat density and
k-mer frequency spectra.

Pure functions only: no I/O, no subprocess, no database. The handler in
`app.queue.assembly_qc_handlers` does the running; this module decides what
to run and what the output means, which is what makes both testable without
a tool installed.
"""

from __future__ import annotations

import threading
from pathlib import Path

from app.errors import JobCancelled
from app.logging import get_logger
from app.models import Compression
from app.storage.parsers import MAX_STORED_CONTIGS

log = get_logger(__name__)

WINDOW_COUNT = 500
MIN_WINDOW_BASES = 100


def build_meryl_count_command(
    *,
    meryl_path: str,
    k: int,
    assembly: Path,
    output: Path,
    threads: int = 4,
) -> list[str]:
    """`meryl count` over an assembly FASTA into a k-mer database.

    The assembly file is the only input — this is what distinguishes
    assembly-side counting from `merqury_runner.build_meryl_count_command`,
    which counts a read set for QV scoring.
    """
    return [
        meryl_path,
        "count",
        f"k={k}",
        f"threads={threads}",
        "output",
        str(output),
        str(assembly),
    ]


def build_meryl_histogram_command(
    *,
    meryl_path: str,
    database: Path,
) -> list[str]:
    """`meryl histogram` over a meryl database.

    Output is TSV: `frequency<TAB>count`.
    """
    return [
        meryl_path,
        "histogram",
        str(database),
    ]


def build_meryl_print_repetitive_command(
    *,
    meryl_path: str,
    database: Path,
    min_count: int,
) -> list[str]:
    """`meryl print greater-than {min_count}` to extract the repetitive
    k-mer set. Output is a FASTA-like stream of canonical k-mer sequences.
    """
    return [
        meryl_path,
        "print",
        f"greater-than={min_count}",
        str(database),
    ]


def build_meryl_statistics_command(
    *,
    meryl_path: str,
    database: Path,
) -> list[str]:
    """`meryl statistics` — prints distinct-kmer and total-kmer counts."""
    return [
        meryl_path,
        "statistics",
        str(database),
    ]


def _canonical(kmer: str) -> str:
    """Return the canonical representation of a k-mer.

    meryl canonicalizes by taking the lexicographically smaller of a k-mer
    and its reverse complement. We must match this during the FASTA scan,
    or set lookups will miss every k-mer on the opposite strand.
    """
    complement = str.maketrans("ACGTacgt", "TGCAtgca")
    revcomp = kmer[::-1].translate(complement)
    return kmer if kmer <= revcomp else revcomp


def _parse_repetitive_kmers(text: str, k: int) -> set[str]:
    """Parse `meryl print greater-than` output into a set of canonical
    k-mer strings.

    meryl print outputs one k-mer per line in FASTA-like format:

        >kmer1
        ACGTACGT...
        >kmer2
        TGCATGCA...

    We only collect the sequence lines (not the headers).
    """
    kmers: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(">"):
            continue
        # meryl print already outputs canonical k-mers, but we
        # canonicalize anyway for safety.
        canonical = _canonical(stripped)
        if len(canonical) == k:
            kmers.add(canonical)
    return kmers


def compute_kmer_spectrum(
    histogram_text: str,
    stats_text: str,
    k: int,
) -> dict:
    """Parse meryl histogram and statistics output into a spectrum fact.

    Returns:
        {
            "k": int,
            "distinct_kmers": int,
            "total_kmers": int,
            "histogram": [{"frequency": int, "count": int}, ...],
        }
    """
    histogram: list[dict] = []
    for line in histogram_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            try:
                freq = int(parts[0])
                count = int(parts[1])
                histogram.append({"frequency": freq, "count": count})
            except ValueError:
                continue

    distinct_kmers = 0
    total_kmers = 0
    for line in stats_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            try:
                val = int(parts[1])
                if "distinct" in parts[0].lower():
                    distinct_kmers = val
                elif "total" in parts[0].lower():
                    total_kmers = val
            except ValueError:
                continue

    return {
        "k": k,
        "distinct_kmers": distinct_kmers,
        "total_kmers": total_kmers,
        "histogram": histogram,
    }


def compute_repeat_density(
    path: Path,
    compression: Compression,
    repetitive_kmers: set[str],
    k: int,
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Scan a FASTA and compute per-contig, per-window repeat density.

    Reuses the gc_tracks.py scanning loop verbatim: same FASTA parser,
    same windowing math (500 windows per contig, floored at 100 bp minimum,
    longest 50 contigs), same cancel check pattern.

    Returns {} on an unreadable file and re-raises JobCancelled.

    `repetitive_kmers` is a set of canonical k-mer strings (from
    `_parse_repetitive_kmers`). `k` is the k-mer size — needed for
    sliding-window extraction from the assembly sequence.
    """
    import gzip

    contigs: list[tuple[str, int, list[str]]] = []
    current_name: str | None = None
    current_buf: list[str] = []
    chars_scanned = 0

    def _check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled("repeat_density cancelled")

    is_compressed = compression in (Compression.GZIP, Compression.BGZF)

    try:
        opener = gzip.open if is_compressed else open
        with opener(path, "rt", errors="replace") as fh:
            for line in fh:
                stripped = line.rstrip("\n\r")
                chars_scanned += len(stripped)
                if chars_scanned >= 1_000_000:
                    _check_cancel()
                    chars_scanned = 0

                if stripped.startswith(">"):
                    if current_name is not None:
                        contigs.append((current_name, len(current_buf), current_buf))
                    current_name = stripped[1:].split()[0]
                    current_buf = []
                elif current_name is not None:
                    current_buf.append(stripped)

        if current_name is not None:
            contigs.append((current_name, len(current_buf), current_buf))
    except JobCancelled:
        raise
    except (OSError, EOFError, UnicodeDecodeError) as e:
        log.warning("repeat_density_scan_failed", path=str(path), error=str(e))
        return {}

    if not contigs:
        return {}

    resolved = []
    for name, buf_len, buf in contigs:
        total_length = sum(len(line) for line in buf) if buf else buf_len
        window_count = min(WINDOW_COUNT, total_length // MIN_WINDOW_BASES)
        if window_count == 0:
            continue
        window_bases = total_length // window_count
        if window_bases == 0:
            continue

        seq = "".join(buf)

        density_list: list[float | None] = []

        for wi in range(window_count):
            start = wi * window_bases
            end = start + window_bases
            chunk = seq[start:end] if end <= total_length else seq[start:]

            if len(chunk) < k:
                density_list.append(None)
                continue

            total_kmers = 0
            repeat_kmers = 0
            for i in range(len(chunk) - k + 1):
                sub = chunk[i:i + k]
                canonical = _canonical(sub)
                total_kmers += 1
                if canonical in repetitive_kmers:
                    repeat_kmers += 1

            if total_kmers == 0:
                density_list.append(None)
            else:
                density_list.append(round(100.0 * repeat_kmers / total_kmers, 1))

        resolved.append({
            "name": name,
            "length": total_length,
            "window_bases": window_bases,
            "repeat_density": density_list,
        })

    resolved.sort(key=lambda c: c["length"], reverse=True)
    partial = len(resolved) > MAX_STORED_CONTIGS
    if partial:
        resolved = resolved[:MAX_STORED_CONTIGS]

    result: dict = {
        "window_count": WINDOW_COUNT,
        "k": k,
        "contigs": resolved,
    }
    if partial:
        result["repeat_density_partial"] = True
    return result
```

- [ ] **Step 2: Verify it imports**

Run: `docker compose exec api python -c "from app.pipelines import meryl_runner; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add backend/app/pipelines/meryl_runner.py
git commit -m "feat(pipelines): add meryl_runner with command builders and repeat-density/spectrum compute"
```

---

### Task 3: Write tests for `meryl_runner.py`

**Files:**
- Create: `backend/tests/pipelines/test_meryl_runner.py`

**Interfaces:**
- Consumes: everything from Task 2

- [ ] **Step 1: Write the tests**

```python
"""Tests for meryl_runner — pure functions, no binary needed."""

import textwrap
import tempfile
from pathlib import Path

from app.models import Compression
from app.pipelines import meryl_runner


def test_build_meryl_count_command_defaults():
    cmd = meryl_runner.build_meryl_count_command(
        meryl_path="/opt/meryl/bin/meryl",
        k=21,
        assembly=Path("/data/assembly.fasta"),
        output=Path("/tmp/test.meryl"),
    )
    assert cmd[0] == "/opt/meryl/bin/meryl"
    assert cmd[1] == "count"
    assert "k=21" in cmd
    assert "threads=4" in cmd
    assert "output" in cmd
    # output dir should be the next arg after "output"
    out_idx = cmd.index("output")
    assert cmd[out_idx + 1] == "/tmp/test.meryl"
    assert "/data/assembly.fasta" in cmd


def test_build_meryl_count_command_custom_k_and_threads():
    cmd = meryl_runner.build_meryl_count_command(
        meryl_path="/opt/meryl/bin/meryl",
        k=15,
        assembly=Path("/data/asm.fa"),
        output=Path("/tmp/db"),
        threads=2,
    )
    assert "k=15" in cmd
    assert "threads=2" in cmd


def test_build_meryl_histogram_command():
    cmd = meryl_runner.build_meryl_histogram_command(
        meryl_path="/opt/meryl/bin/meryl",
        database=Path("/tmp/test.meryl"),
    )
    assert cmd[0] == "/opt/meryl/bin/meryl"
    assert cmd[1] == "histogram"
    assert "/tmp/test.meryl" in cmd


def test_build_meryl_print_repetitive_command():
    cmd = meryl_runner.build_meryl_print_repetitive_command(
        meryl_path="/opt/meryl/bin/meryl",
        database=Path("/tmp/test.meryl"),
        min_count=5,
    )
    assert cmd[0] == "/opt/meryl/bin/meryl"
    assert cmd[1] == "print"
    assert "greater-than=5" in cmd


def test_build_meryl_statistics_command():
    cmd = meryl_runner.build_meryl_statistics_command(
        meryl_path="/opt/meryl/bin/meryl",
        database=Path("/tmp/test.meryl"),
    )
    assert cmd[0] == "/opt/meryl/bin/meryl"
    assert cmd[1] == "statistics"
    assert "/tmp/test.meryl" in cmd


def test_canonical_same_both_strands():
    # The canonical form should be the same from both strands
    c1 = meryl_runner._canonical("ACGT")
    c2 = meryl_runner._canonical("ACGT")
    assert c1 == c2

    # A k-mer and its reverse complement should canonicalize to the same string
    fwd = "AAAA"
    revcomp = "TTTT"
    assert meryl_runner._canonical(fwd) == meryl_runner._canonical(revcomp)


def test_parse_repetitive_kmers():
    text = """>kmer1
ACGTACGTACGTACGTACGTA
>kmer2
TGCATGCATGCATGCATGCA
"""
    result = meryl_runner._parse_repetitive_kmers(text, k=21)
    assert isinstance(result, set)
    assert len(result) == 2


def test_parse_repetitive_kmers_skips_short():
    text = """>kmer1
ACGT
"""
    result = meryl_runner._parse_repetitive_kmers(text, k=21)
    assert len(result) == 0  # 4-char k-mer ignored for k=21


def test_compute_kmer_spectrum():
    histogram = """1\t8200000
2\t3100000
3\t1500000
"""
    stats = """distinct_kmers\t14200000
total_kmers\t235000000
"""
    result = meryl_runner.compute_kmer_spectrum(histogram, stats, k=21)
    assert result["k"] == 21
    assert result["distinct_kmers"] == 14200000
    assert result["total_kmers"] == 235000000
    assert len(result["histogram"]) == 3
    assert result["histogram"][0] == {"frequency": 1, "count": 8200000}


def test_compute_repeat_density_basic():
    """A small FASTA with a known repeat — one k-mer repeated."""
    content = "">contig1\n" + ("ACGT" * 250) + "\n"
    # Build a FASTA with a 1000bp contig of repeating ACGT
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".fasta", delete=False
    ) as f:
        f.write(content)
        path = Path(f.name)

    try:
        # ACGT is 4bp, k=4: every sliding window is ACGT, CGTA, GTAC, or TACG
        # Make all of them repetitive
        repetitive = {"ACGT", "CGTA", "GTAC", "TACG"}
        result = meryl_runner.compute_repeat_density(
            path,
            compression=Compression.NONE,
            repetitive_kmers=repetitive,
            k=4,
        )
        assert "contigs" in result
        assert len(result["contigs"]) == 1
        densities = result["contigs"][0]["repeat_density"]
        # Every k-mer in a repeating ACGT sequence should be in the set
        assert all(d == 100.0 for d in densities if d is not None)
    finally:
        path.unlink(missing_ok=True)


def test_compute_repeat_density_no_repeats():
    """A FASTA with no repetitive k-mers in the set."""
    content = "">contig1\n" + ("ACGT" * 250) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".fasta", delete=False
    ) as f:
        f.write(content)
        path = Path(f.name)

    try:
        # Empty set — nothing is repetitive
        result = meryl_runner.compute_repeat_density(
            path,
            compression=Compression.NONE,
            repetitive_kmers=set(),
            k=4,
        )
        densities = result["contigs"][0]["repeat_density"]
        assert all(d == 0.0 for d in densities if d is not None)
    finally:
        path.unlink(missing_ok=True)


def test_compute_repeat_density_none_for_short_windows():
    """A contig too short for even one full k-mer gets None."""
    content = ">contig1\nACGT\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".fasta", delete=False
    ) as f:
        f.write(content)
        path = Path(f.name)

    try:
        # k=6 on a 4bp contig — no window has enough sequence
        result = meryl_runner.compute_repeat_density(
            path,
            compression=Compression.NONE,
            repetitive_kmers={"ACGTAC"},
            k=6,
        )
        # The contig is too short for the window minimum
        assert result == {}
    finally:
        path.unlink(missing_ok=True)
```

- [ ] **Step 2: Run the tests**

Run: `docker compose exec api python -m pytest tests/pipelines/test_meryl_runner.py -v`
Expected: all tests PASS

- [ ] **Step 3: Commit**

```bash
git add backend/tests/pipelines/test_meryl_runner.py
git commit -m "test(pipelines): add meryl_runner tests for command builders, parsers, and repeat density"
```

---

### Task 4: Add `characterize_kmers` handler

**Files:**
- Modify: `backend/app/queue/assembly_qc_handlers.py` — new handler at the end (before existing blank line before other handlers)

**Interfaces:**
- Consumes: `SidecarRole.MERYL_ASSEMBLY_DB` (Task 1), `meryl_runner` (Task 2)
- Produces: `"characterize_kmers"` handler registered by the `@handler` decorator

- [ ] **Step 1: Add the handler**

In `backend/app/queue/assembly_qc_handlers.py`, after the existing `analyze_gc_tracks` handler (around line 1048), add:

```python

_KMER_LEASE_SECONDS = 7200  # 2h


@handler(
    "characterize_kmers",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
    max_attempts=1,
)
def characterize_kmers(ctx: JobContext) -> dict:
    """Build an assembly-side meryl k-mer database and compute per-window
    repeat density and k-mer frequency spectrum.

    Read-only: produces facts on the assembly and caches the k-mer database
    as a MERYL_ASSEMBLY_DB sidecar.
    """
    from app.pipelines import meryl_runner

    meryl_tool = tools.require(tools.meryl())

    work = _prepare_workdir(ctx, "characterize_kmers")

    assembly = _resolve_input(ctx.payload, "assembly")
    assembly = _named_link(work, assembly, "assembly.fasta")

    k = int(ctx.payload.get("k") or 21)
    min_count = int(ctx.payload.get("min_count") or 5)
    threads = max(1, int(ctx.payload.get("threads") or 4))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    database = work / "assembly.meryl"

    # ── Step 1: meryl count ───────────────────────────────────────

    ctx.progress(phase="counting", pct=None, message="building k-mer database")
    ctx.extend_lease(_KMER_LEASE_SECONDS)

    count_cmd = meryl_runner.build_meryl_count_command(
        meryl_path=meryl_tool.path,
        k=k,
        assembly=assembly,
        output=database,
        threads=threads,
    )
    log.info("characterize_kmers_count_started", job_id=ctx.job_id, k=k,
             cmd=" ".join(count_cmd))
    code = run_subprocess(ctx, count_cmd, log_path=str(log_path))
    if code != 0:
        raise _failure(code, log_path, "meryl count")

    # ── Step 2: meryl statistics ──────────────────────────────────

    stats_lines: list[str] = []
    def _collect_stats(line: str) -> None:
        stats_lines.append(line)

    stats_cmd = meryl_runner.build_meryl_statistics_command(
        meryl_path=meryl_tool.path,
        database=database,
    )
    code = run_subprocess(
        ctx, stats_cmd, log_path=str(log_path), on_line=_collect_stats
    )
    if code != 0:
        raise _failure(code, log_path, "meryl statistics")
    stats_text = "\n".join(stats_lines)

    # ── Step 3: meryl histogram ───────────────────────────────────

    ctx.progress(phase="histogram", pct=None, message="computing k-mer spectrum")
    hist_lines: list[str] = []
    def _collect_hist(line: str) -> None:
        hist_lines.append(line)

    hist_cmd = meryl_runner.build_meryl_histogram_command(
        meryl_path=meryl_tool.path,
        database=database,
    )
    code = run_subprocess(
        ctx, hist_cmd, log_path=str(log_path), on_line=_collect_hist
    )
    if code != 0:
        raise _failure(code, log_path, "meryl histogram")
    hist_text = "\n".join(hist_lines)

    spectrum = meryl_runner.compute_kmer_spectrum(hist_text, stats_text, k)

    # ── Step 4: meryl print greater-than (repetitive k-mers) ──────

    ctx.progress(phase="density", pct=None, message="computing repeat density")
    kmers_lines: list[str] = []
    def _collect_kmers(line: str) -> None:
        kmers_lines.append(line)

    print_cmd = meryl_runner.build_meryl_print_repetitive_command(
        meryl_path=meryl_tool.path,
        database=database,
        min_count=min_count,
    )
    code = run_subprocess(
        ctx, print_cmd, log_path=str(log_path), on_line=_collect_kmers
    )
    if code != 0:
        raise _failure(code, log_path, "meryl print")

    kmers_text = "\n".join(kmers_lines)
    repetitive_kmers = meryl_runner._parse_repetitive_kmers(kmers_text, k)

    # ── Step 5: compute repeat density ────────────────────────────

    compression_raw = ctx.payload.get("compression") or "none"
    try:
        compression = Compression(compression_raw)
    except ValueError:
        compression = Compression.NONE

    density = meryl_runner.compute_repeat_density(
        Path(assembly),
        compression,
        repetitive_kmers,
        k,
        cancel_event=ctx.cancel_event,
    )

    # ── Step 6: return facts and sidecar path ────────────────────

    facts = {
        "repeat_density": density,
        "kmer_spectrum": spectrum,
    }

    ctx.progress(phase="done", pct=1.0, message="k-mer characterization complete")
    log.info(
        "characterize_kmers_finished",
        job_id=ctx.job_id,
        k=k,
        contigs=len(density.get("contigs") or []),
        distinct_kmers=spectrum.get("distinct_kmers"),
    )

    result = {
        "object_id": ctx.payload["object_id"],
        "job_id": ctx.job_id,
        "facts": facts,
        "k": k,
        "meryl_db_dir": str(database),
    }
    return result
```

- [ ] **Step 2: Register the handler as read-only in `provenance_walker.py`**

In `backend/app/services/provenance_walker.py`, after the `"analyze_gc_tracks"` entry (around line 184), add:

```python
        "characterize_kmers",
```

- [ ] **Step 3: Verify the handler imports and registers****

Run: `docker compose exec api python -c "from app.queue.assembly_qc_handlers import characterize_kmers; print('ok')"`
Expected: `ok` (no error)

- [ ] **Step 3: Commit**

```bash
git add backend/app/queue/assembly_qc_handlers.py backend/app/services/provenance_walker.py
git commit -m "feat(queue): add characterize_kmers handler for meryl repeat density and k-mer spectrum"
```

---

### Task 5: Add result applier for `characterize_kmers`

**Files:**
- Modify: `backend/app/queue/results.py` — new applier function and registry entry

**Interfaces:**
- Consumes: `"characterize_kmers"` handler name (Task 4), `SidecarRole.MERYL_ASSEMBLY_DB` (Task 1)
- Produces: `_apply_characterize_kmers` registered in the `_APPLIERS` dict

- [ ] **Step 1: Add the applier function**

In `backend/app/queue/results.py`, after `_apply_analyze_gc_tracks` (around the area of line 1712), add:

```python


async def _apply_characterize_kmers(result: dict, *, owner: str) -> None:
    """Record meryl repeat-density and k-mer-spectrum facts on the assembly,
    and cache the meryl database directory as an assembly-side sidecar.

    Follows the `_apply_assess_assembly_qv` pattern: facts half merges onto
    the assembly, sidecar half ingests each file inside the meryl database
    directory onto the assembly object (not a read object — this DB is built
    from the assembly, not the reads).
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("characterize_kmers_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    density = facts.get("repeat_density") or {}
    spectrum = facts.get("kmer_spectrum") or {}
    log.info(
        "characterize_kmers_applied",
        object_id=object_id,
        contigs=len(density.get("contigs") or []),
        distinct_kmers=spectrum.get("distinct_kmers"),
    )

    meryl_db_dir = result.get("meryl_db_dir")
    if not meryl_db_dir:
        return

    from app.services import object_service

    db_dir = Path(meryl_db_dir)
    job_id = result.get("job_id")
    k = result.get("k")
    members = sorted(p for p in db_dir.rglob("*") if p.is_file())
    for member in members:
        try:
            await object_service.ingest_local_file(
                owner=obj.owner,
                project_id=obj.project_id,
                path=member,
                name=f"{db_dir.name}__{member.relative_to(db_dir).as_posix().replace('/', '__')}",
                derived_from=[obj.id],
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                facts={
                    "meryl_db_k": k,
                    "meryl_db_name": db_dir.name,
                    "meryl_db_expected_count": len(members),
                },
                sidecar_of=obj.id,
                sidecar_role=SidecarRole.MERYL_ASSEMBLY_DB,
            )
        except Exception:  # noqa: BLE001
            log.warning("meryl_assembly_db_ingest_failed",
                        member=str(member), exc_info=True)
```

And in the `_APPLIERS` dict (around line 2539), add:

```python
    "characterize_kmers": _apply_characterize_kmers,
```

- [ ] **Step 2: Verify the applier imports**

Run: `docker compose exec api python -c "from app.queue.results import _apply_characterize_kmers; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add backend/app/queue/results.py
git commit -m "feat(queue): add characterize_kmers result applier with assembly-side meryl DB sidecars"
```

---

### Task 6: Add launch function in `pipeline_service.py`

**Files:**
- Modify: `backend/app/services/pipeline_service.py` — new `launch_characterize_kmers` function

**Interfaces:**
- Consumes: `"characterize_kmers"` handler (Task 4)
- Produces: `launch_characterize_kmers(*, object_id: PydanticObjectId, owner: str, k: int | None = None, min_count: int | None = None) -> Job`

- [ ] **Step 1: Add the launch function**

In `backend/app/services/pipeline_service.py`, after `launch_gc_tracks` (around line 3855), add:

```python


async def launch_characterize_kmers(
    *,
    object_id: PydanticObjectId,
    owner: str,
    k: int | None = None,
    min_count: int | None = None,
) -> Job:
    """Queue meryl k-mer repeat density and spectrum computation for one assembly.

    Modelled on `launch_gc_tracks`: single input, read-only, no run
    record. The result is facts merged onto the assembly plus a cached
    meryl database as an assembly sidecar.
    """
    from app.queue import queue
    from app.services import object_service

    obj = await object_service.get_object(object_id, owner=owner)
    _check_completeness_callable(obj)  # same gates: FASTA, not protein/transcript

    digest, path = await _resolve_readable(obj)
    if not digest and not path:
        raise ValidationError(
            f"{obj.name!r} has no stored content yet (status={obj.status.value})",
            details={"object_id": str(obj.id)},
        )

    k_val = k or 21
    min_count_val = min_count or 5

    payload: dict = {
        "object_id": str(obj.id),
        "assembly_name": obj.name,
        "k": k_val,
        "min_count": min_count_val,
    }
    if digest:
        payload["assembly_sha256"] = digest
    if path:
        payload["assembly_path"] = path
    payload["compression"] = (obj.format.compression or "none").lower()

    job = await queue.enqueue(
        "characterize_kmers",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=f"characterize_kmers:{obj.id}:k={k_val}",
        project_id=obj.project_id,
        object_id=obj.id,
    )
    if job is None:
        raise ConflictError(
            "K-mer characterization is already queued or running for this assembly",
            details={"object_id": str(obj.id)},
        )

    log.info(
        "characterize_kmers_launched",
        job_id=str(job.id),
        object_id=str(obj.id),
        k=k_val,
        min_count=min_count_val,
    )
    return job
```

- [ ] **Step 2: Register as read-only in `node_types.py`**

In `backend/app/pipelines/node_types.py`, after the `"pipeline_service.launch_gc_tracks"` entry (around line 787), add:

```python
        "pipeline_service.launch_characterize_kmers",
```

- [ ] **Step 4: Verify the function imports**

Run: `docker compose exec api python -c "from app.services.pipeline_service import launch_characterize_kmers; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/pipelines/node_types.py
git commit -m "feat(services): add launch_characterize_kmers for meryl repeat density and spectrum"
```

---

### Task 7: Add API route

**Files:**
- Modify: `backend/app/api/v1/pipelines.py` — new route and request model

**Interfaces:**
- Consumes: `pipeline_service.launch_characterize_kmers` (Task 6)
- Produces: `POST /characterize-kmers` endpoint

- [ ] **Step 1: Add request model and route**

In `backend/app/api/v1/pipelines.py`, after the `GcTracksRequest` / gc-tracks route block (around line 1123), add:

```python


class CharacterizeKmersRequest(BaseModel):
    object_id: PydanticObjectId
    k: int | None = None
    min_count: int | None = None


@router.post("/characterize-kmers", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_characterize_kmers_route(
    body: CharacterizeKmersRequest, owner: OwnerDep
) -> JobOut:
    """Queue meryl k-mer repeat density and spectrum computation for one
    assembly. Read-only: produces facts, no derived object."""
    job = await pipeline_service.launch_characterize_kmers(
        object_id=body.object_id,
        owner=owner,
        k=body.k,
        min_count=body.min_count,
    )
    return JobOut.of(job)
```

- [ ] **Step 2: Verify the route is registered**

Run: `docker compose exec api python -c "from app.api.v1.pipelines import launch_characterize_kmers_route; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add backend/app/api/v1/pipelines.py
git commit -m "feat(api): add POST /characterize-kmers endpoint for meryl k-mer analysis"
```

---

### Task 8: Add suggestion card

**Files:**
- Modify: `backend/app/services/suggestion_service.py` — new `build_kmer_characterization_card` function and registration in the orchestrator

**Interfaces:**
- Consumes: `tools.meryl()` (existing), `PipelineType` (existing)
- Produces: `build_kmer_characterization_card(obj) -> SuggestionCard | None`

- [ ] **Step 1: Add the card builder**

In `backend/app/services/suggestion_service.py`, after `build_gc_tracks_card` (around line 969), add:

```python


def build_kmer_characterization_card(obj) -> SuggestionCard | None:
    """Meryl: per-window repeat density and k-mer frequency spectrum for
    a finished assembly.

    Gated on shape (FASTA, not protein/transcript) and meryl availability,
    following `build_gc_tracks_card` for the shape gates and
    `build_qv_card` for the tool probe.
    """
    if obj.format.kind is not FormatKind.FASTA:
        return None
    if obj.role in pipeline_service.COMPLETENESS_EXCLUDED_ROLES:
        return None

    title = "Characterize k-mer repeats and spectrum"
    description = (
        "Compute per-window repeat density from high-frequency k-mers "
        "and a k-mer frequency histogram for genome characterization."
    )

    def unavailable(reason: str) -> SuggestionCard:
        return SuggestionCard(
            kind="characterize_kmers",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    meryl_tool = tools.meryl()
    if not meryl_tool.available:
        return unavailable(meryl_tool.error or "meryl is not installed.")

    return SuggestionCard(
        kind="characterize_kmers",
        category="ASSEMBLY_QC",
        title=title,
        description=description,
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/characterize-kmers",
            "body": {"object_id": str(obj.id)},
        },
    )
```

- [ ] **Step 2: Register the card in the orchestrator**

In the `build_bucket` or orchestrator function (around line 1853 where `("gc_tracks", ...)` is listed), add the new card to the appropriate list. Following the `ASSEMBLY_QC` category items:

```python
        ("characterize_kmers", lambda: build_kmer_characterization_card(obj)),
```

- [ ] **Step 3: Verify the card builder imports**

Run: `docker compose exec api python -c "from app.services.suggestion_service import build_kmer_characterization_card; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/suggestion_service.py
git commit -m "feat(services): add k-mer characterization suggestion card"
```

---

### Task 9: Test the suggestion card

**Files:**
- Modify: `backend/tests/services/test_suggestion_service.py` — new test for the card

**Interfaces:**
- Consumes: `build_kmer_characterization_card` (Task 8)

- [ ] **Step 1: Write the card test**

The key trap from CLAUDE.md: patch the probe to `available=False` and assert the card flips to `UNAVAILABLE` — that's the direction that fails when the seam breaks.

```python


def test_build_kmer_characterization_card_available(mock_tools_for_svc, sample_assembly):
    """The card is available on a ready FASTA assembly with meryl installed."""
    with patch("app.services.suggestion_service.tools.meryl") as mock_meryl:
        mock_meryl.return_value = MagicMock(available=True, error=None)
        card = suggestion_service.build_kmer_characterization_card(sample_assembly)
        assert card is not None
        assert card.status == CardStatus.AVAILABLE
        assert card.kind == "characterize_kmers"


def test_build_kmer_characterization_card_unavailable_no_meryl(
    mock_tools_for_svc, sample_assembly
):
    """The card flips to UNAVAILABLE when meryl is not installed."""
    with patch("app.services.suggestion_service.tools.meryl") as mock_meryl:
        mock_meryl.return_value = MagicMock(
            available=False, error="meryl not found"
        )
        card = suggestion_service.build_kmer_characterization_card(sample_assembly)
        assert card is not None
        assert card.status == CardStatus.UNAVAILABLE
        assert "meryl not found" in (card.reason or "")


def test_build_kmer_characterization_card_skips_non_fasta(
    mock_tools_for_svc,
):
    """The card is not shown for non-FASTA objects (e.g., BAMs)."""
    obj = MagicMock(format=MagicMock(kind=FormatKind.BAM), role=ObjectRole.ASSEMBLY)
    card = suggestion_service.build_kmer_characterization_card(obj)
    assert card is None
```

- [ ] **Step 2: Run the tests**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -k "kmer_characterization" -v`
Expected: all tests PASS

- [ ] **Step 3: Commit**

```bash
git add backend/tests/services/test_suggestion_service.py
git commit -m "test(services): add suggestion card tests for k-mer characterization"
```

---

### Task 10: Update TOOL_META for meryl

**Files:**
- Modify: `backend/app/pipelines/tools.py` — expand the existing `"meryl"` TOOL_META entry

- [ ] **Step 1: Expand the meryl TOOL_META entry**

At `backend/app/pipelines/tools.py:1722`, update:

**`pipelines`:** Change from `(PipelineType.ASSEMBLY_QC,)` to `(PipelineType.ASSEMBLY_QC, PipelineType.ASSEMBLY_QC)` — or if a new pipeline type is needed, check `PipelineType` enum. For now the same `ASSEMBLY_QC` type covers both uses (Merqury QV and standalone meryl). If the enum has no broader match, keeping the existing tuple is correct — meryl already has `runnable=True` and the suggestion card probes it directly.

**`usage`:** Append to the string, after the existing Merqury paragraph:

```
Also computes per-window repeat-density tracks and k-mer frequency
spectra for genome characterization when invoked by the
"Characterize k-mer repeats and spectrum" action card.
```

Actually, looking at the existing TOOL_META, it already covers meryl's role well. The key requirement from `test_every_tool_is_documented` is that all required fields (`homepage`, `citation`, `license`, `usage`) are filled — which they are. The `pipelines` tuple doesn't need to change since meryl's standalone use is still in the `ASSEMBLY_QC` pipeline. The `usage` update is the meaningful change.

- [ ] **Step 2: Verify the test_every_tool_is_documented test still passes**

Run: `docker compose exec api python -m pytest tests/pipelines/test_tools.py -k "every_tool_is_documented" -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add backend/app/pipelines/tools.py
git commit -m "docs(tools): update meryl TOOL_META usage for standalone repeat-density role"
```

---

### Task 11: Restart worker and smoke test

**No code changes.** Verify end-to-end manually.

- [ ] **Step 1: Restart worker**

```bash
docker compose restart worker
```

- [ ] **Step 2: Verify the card appears**

Open the app at `localhost:5173`, navigate to a project with a ready FASTA assembly, open its Actions tab. Confirm the "Characterize k-mer repeats and spectrum" card is visible.

- [ ] **Step 3: Launch the card**

Click Launch on the card. Watch the job in the Jobs panel — should progress through `counting → histogram → density → done`.

- [ ] **Step 4: Verify facts are stored**

After the job completes, check the assembly object in the API for `repeat_density` and `kmer_spectrum` facts.

- [ ] **Step 5: Run the full test suite**

```bash
docker compose exec api python -m pytest tests/ -q
```
Expected: all tests PASS

- [ ] **Step 6: Commit any fixes**

If any issues are found during smoke testing, fix and commit them.

---
