# Variant Consequence Annotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Annotate called variants with gene, consequence type, and amino-acid change using `bcftools csq`, surfaced as filterable columns in the variants table.

**Architecture:** A pure parser (`csq_parse.py`) holds every BCSQ shape and is exhaustively unit-tested; a thin runner (`csq_runner.py`) builds the command and mirrors `variant_runner.py`; `variant_db.py` gains four columns and a filter; `vcf_stats_runner.py` extracts `%INFO/BCSQ`; a `suggestion_service.py` rule makes the Actions card reachable. Frontend adds three columns and a dropdown.

**Tech Stack:** Python 3.12 + FastAPI + Motor/Mongo, SQLite for the variant index, pytest (run inside the `api` container). React 18 + TypeScript + vitest on the frontend.

**Spec:** `docs/superpowers/specs/2026-07-31-variant-annotation-design.md`

**Run backend tests with:** `docker compose exec -T api python -m pytest tests/ -q` from the main repo root — the host venv hits Mongo replica-set errors the container doesn't have.

**After changing anything under `app/queue/` or a handler's imports:** `docker compose restart worker`, or the job silently runs the old in-memory code.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `backend/app/pipelines/csq_parse.py` | Create | Pure BCSQ string → `Consequence`. All edge cases live here. |
| `backend/tests/pipelines/test_csq_parse.py` | Create | Exhaustive parser tests, using real strings from the yeast run |
| `backend/app/pipelines/tools.py` | Modify | `bcftools_csq()` capability probe |
| `backend/tests/pipelines/test_tools.py` | Modify | Probe tests (version floor) |
| `backend/app/pipelines/csq_runner.py` | Create | Command builder for `bcftools csq` |
| `backend/tests/pipelines/test_csq_runner.py` | Create | Asserts `-p a` and required flags |
| `backend/app/pipelines/variant_db.py` | Modify | Four columns, consequence filter, index |
| `backend/tests/pipelines/test_variant_db.py` | Modify | Round-trip and filter tests |
| `backend/app/pipelines/vcf_stats_runner.py` | Modify | `%INFO/BCSQ` in `QUERY_FORMAT` |

**Scope note:** this plan covers the backend through the variant index. The run kind, queue handler, Actions card, and frontend columns are deliberately *not* here — they depend on these pieces being correct, and this is already a full plan's worth of work. A follow-on plan covers them.

---

### Task 1: BCSQ parser — the shapes

**Files:**
- Create: `backend/app/pipelines/csq_parse.py`
- Test: `backend/tests/pipelines/test_csq_parse.py`

Every string below was taken from a real `bcftools csq` run on the yeast VCF. Do not invent additional shapes.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_csq_parse.py`:

```python
"""BCSQ parsing.

Every fixture here is a real string from `bcftools csq -p a` run against
DRR1066343.bcftools.vcf.gz with the GCF_000146045.2 GFF3. The format is not
one fixed shape -- records carry 7, 5, 4 or 1 fields -- and the parser exists
because a `split("|")[5]` would be wrong on three of those four.
"""

from app.pipelines import csq_parse


class TestSingleConsequence:
    def test_parses_a_full_missense_record(self):
        c = csq_parse.parse_bcsq(
            "missense|CYS3|rna-NM_001178157.1|protein_coding|+|160K>160M|131277A>T"
        )
        assert c is not None
        assert c.consequence == "missense"
        assert c.gene == "CYS3"
        assert c.transcript == "rna-NM_001178157.1"
        assert c.aa_pos == 160
        assert c.aa_change == "160K>160M"

    # Synonymous records carry position and residue with no ">" -- there is no
    # change to describe.
    def test_parses_a_synonymous_record_with_no_arrow(self):
        c = csq_parse.parse_bcsq(
            "synonymous|SSA1|rna-NM_001178151.1|protein_coding|-|99P|141135A>T"
        )
        assert c.consequence == "synonymous"
        assert c.aa_pos == 99
        assert c.aa_change == "99P"

    # 5 fields: no amino-acid column at all.
    def test_parses_a_five_field_start_lost(self):
        c = csq_parse.parse_bcsq(
            "start_lost|SNU23|rna-NM_001180157.1|protein_coding|-"
        )
        assert c.consequence == "start_lost"
        assert c.gene == "SNU23"
        assert c.aa_pos is None
        assert c.aa_change is None

    # 4 fields, and the transcript column is empty.
    def test_parses_a_four_field_intron(self):
        c = csq_parse.parse_bcsq("intron|RPL19B||protein_coding")
        assert c.consequence == "intron"
        assert c.gene == "RPL19B"
        assert c.transcript is None
        assert c.aa_pos is None

    # A "*" prefix marks a compound/haplotype-modified prediction.
    def test_strips_the_compound_marker(self):
        c = csq_parse.parse_bcsq(
            "*missense|ADH1|rna-NM_001183340.1|protein_coding|+|48T>48A|15000A>G"
        )
        assert c.consequence == "missense"
        assert c.compound is True


class TestPointersAndLists:
    # "@position" is a pointer to another record sharing a haplotype, not a
    # consequence. Alone, it yields nothing.
    def test_a_bare_pointer_yields_nothing(self):
        assert csq_parse.parse_bcsq("@286153") is None

    # THE case that motivated this parser: a pointer can sit inside a comma
    # list beside a real consequence. Rejecting the whole record on seeing "@"
    # would discard a real annotation.
    def test_a_pointer_beside_a_real_consequence_keeps_the_real_one(self):
        c = csq_parse.parse_bcsq(
            "missense|CHS3|rna-NM_001178371.1|protein_coding|-|1163F>1163L|284437G>T,@286153"
        )
        assert c is not None
        assert c.gene == "CHS3"
        assert c.consequence == "missense"

    # The same variant against two overlapping transcripts.
    def test_counts_additional_consequences(self):
        c = csq_parse.parse_bcsq(
            "synonymous|YBL100W-A|rna-NM_001180049.1|protein_coding|+|26V|30012C>T,"
            "synonymous|YBL100W-B|rna-NM_001180050.2|protein_coding|+|26V|30012C>T"
        )
        assert c.gene == "YBL100W-A"
        assert c.additional == 1

    # Severity ranking, not file order: a frameshift beside a synonymous call
    # is the one worth showing in a single column.
    def test_keeps_the_most_severe_consequence(self):
        c = csq_parse.parse_bcsq(
            "synonymous|GENE1|rna-1|protein_coding|+|10A|100A>G,"
            "frameshift|GENE2|rna-2|protein_coding|+|20B|100A>G"
        )
        assert c.consequence == "frameshift"
        assert c.gene == "GENE2"


class TestAbsentAndMalformed:
    # bcftools query emits "." for a missing tag. Every un-annotated VCF is
    # this case, so it must be ordinary rather than an error.
    def test_a_dot_yields_nothing(self):
        assert csq_parse.parse_bcsq(".") is None

    def test_empty_yields_nothing(self):
        assert csq_parse.parse_bcsq("") is None
        assert csq_parse.parse_bcsq("   ") is None

    # Truncated to fewer fields than even the 4-field form.
    def test_too_few_fields_yields_nothing(self):
        assert csq_parse.parse_bcsq("missense|CYS3") is None

    # A non-numeric amino-acid position must not raise -- the consequence and
    # gene are still worth keeping.
    def test_unparseable_aa_position_keeps_the_rest(self):
        c = csq_parse.parse_bcsq(
            "missense|CYS3|rna-1|protein_coding|+|??>??|131277A>T"
        )
        assert c is not None
        assert c.gene == "CYS3"
        assert c.aa_pos is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec -T api python -m pytest tests/pipelines/test_csq_parse.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'app.pipelines.csq_parse'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/csq_parse.py`:

```python
"""Parsing bcftools' BCSQ consequence field.

Separate from the runner because this is where the edge cases are. The format
is documented as pipe-delimited, but real output carries four different field
counts and two kinds of entry that are not consequences at all, and each of
those was found by running it rather than by reading about it:

    missense|CYS3|rna-NM_...|protein_coding|+|160K>160M|131277A>T   7 fields
    start_lost|SNU23|rna-NM_...|protein_coding|-                    5 fields
    intron|RPL19B||protein_coding                                   4 fields
    @286153                                                         1 field

Measured on 4,152 annotated yeast variants: 4,027 seven-field, 104 four-field,
5 five-field, 141 bare pointers.
"""

import re
from dataclasses import dataclass

# Most severe first. A variant can carry several consequences across
# overlapping transcripts, and the table shows one column -- so which one wins
# has to be a decision rather than whichever bcftools happened to list first.
_SEVERITY = (
    "frameshift",
    "stop_gained",
    "stop_lost",
    "start_lost",
    "splice_acceptor",
    "splice_donor",
    "inframe_deletion",
    "inframe_insertion",
    "missense",
    "splice_region",
    "synonymous",
    "stop_retained",
    "5_prime_utr",
    "3_prime_utr",
    "non_coding",
    "intron",
    "intergenic",
)
_RANK = {name: i for i, name in enumerate(_SEVERITY)}

# Fewer fields than this is not a consequence -- the shortest real form
# (intron) carries four.
_MIN_FIELDS = 4

# "160K>160M" and "99P" both start with the residue number.
_AA_POS = re.compile(r"^(\d+)")


@dataclass(frozen=True)
class Consequence:
    """One variant's effect, already reduced to what a table column shows."""

    consequence: str
    gene: str | None
    transcript: str | None
    aa_change: str | None
    aa_pos: int | None
    #: bcftools prefixed the type with "*", meaning the prediction accounts for
    #: another variant on the same haplotype.
    compound: bool
    #: How many further consequences this variant had, beyond the one kept.
    additional: int


def _parse_one(item: str) -> Consequence | None:
    fields = item.split("|")
    if len(fields) < _MIN_FIELDS:
        return None

    kind = fields[0]
    compound = kind.startswith("*")
    if compound:
        kind = kind[1:]

    aa_change = fields[5] if len(fields) > 5 and fields[5] else None
    aa_pos = None
    if aa_change:
        match = _AA_POS.match(aa_change)
        if match:
            aa_pos = int(match.group(1))

    return Consequence(
        consequence=kind,
        gene=fields[1] or None,
        transcript=fields[2] or None,
        aa_change=aa_change,
        aa_pos=aa_pos,
        compound=compound,
        additional=0,
    )


def parse_bcsq(value: str | None) -> Consequence | None:
    """The most severe consequence in a BCSQ value, or None if it holds none.

    None is an ordinary answer, not a failure: `bcftools query` emits "." for
    every variant in an un-annotated VCF, and for annotated ones only 63% of
    records carried a consequence in the measured run.
    """
    if not value:
        return None
    text = value.strip()
    if not text or text == ".":
        return None

    parsed: list[Consequence] = []
    for item in text.split(","):
        item = item.strip()
        # "@1234" points at another record sharing this haplotype. Skipped per
        # item rather than rejecting the whole value -- a pointer can sit in a
        # list beside a real consequence, and dropping the record would lose a
        # real annotation.
        if not item or item.startswith("@"):
            continue
        one = _parse_one(item)
        if one is not None:
            parsed.append(one)

    if not parsed:
        return None

    best = min(parsed, key=lambda c: _RANK.get(c.consequence, len(_SEVERITY)))
    if len(parsed) == 1:
        return best
    return Consequence(
        consequence=best.consequence,
        gene=best.gene,
        transcript=best.transcript,
        aa_change=best.aa_change,
        aa_pos=best.aa_pos,
        compound=best.compound,
        additional=len(parsed) - 1,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec -T api python -m pytest tests/pipelines/test_csq_parse.py -q`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/csq_parse.py backend/tests/pipelines/test_csq_parse.py
git commit -m "feat: parse bcftools BCSQ consequence annotations"
```

---

### Task 2: `bcftools csq` capability probe

**Files:**
- Modify: `backend/app/pipelines/tools.py`
- Test: `backend/tests/pipelines/test_tools.py`

`csq` is a subcommand of an already-probed binary, so this is a *capability* probe rather than a new tool: it answers "is this bcftools new enough", which is what the Actions card needs to say.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_tools.py`:

```python
class TestBcftoolsCsq:
    """`csq` ships inside bcftools rather than as its own binary, so the
    question is the version, not the path. Asserting the unavailable direction
    matters most: the image ships bcftools 1.21, so an "available" assertion
    would pass whether or not the patch worked."""

    def setup_method(self):
        tools.reset_cache()

    def teardown_method(self):
        tools.reset_cache()

    def test_unavailable_when_bcftools_is_missing(self, monkeypatch):
        monkeypatch.setattr(
            tools,
            "bcftools",
            lambda: tools.Tool(
                name="bcftools", path=None, version=None, error="not found"
            ),
        )
        t = tools.bcftools_csq()
        assert not t.available
        assert "bcftools" in (t.error or "")

    def test_unavailable_when_bcftools_is_too_old(self, monkeypatch):
        monkeypatch.setattr(
            tools,
            "bcftools",
            lambda: tools.Tool(name="bcftools", path="/usr/bin/bcftools", version="1.6"),
        )
        t = tools.bcftools_csq()
        assert not t.available
        assert "1.7" in (t.error or "")

    def test_available_on_a_new_enough_bcftools(self, monkeypatch):
        monkeypatch.setattr(
            tools,
            "bcftools",
            lambda: tools.Tool(name="bcftools", path="/usr/bin/bcftools", version="1.21"),
        )
        t = tools.bcftools_csq()
        assert t.available
        assert t.version == "1.21"

    # An unparseable version must not be read as "too old" -- that would
    # disable a working tool over a cosmetic parse failure.
    def test_unknown_version_is_allowed(self, monkeypatch):
        monkeypatch.setattr(
            tools,
            "bcftools",
            lambda: tools.Tool(name="bcftools", path="/usr/bin/bcftools", version=None),
        )
        assert tools.bcftools_csq().available
```

Check the existing imports at the top of that file; add `from app.pipelines import tools` only if it is not already imported.

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec -T api python -m pytest tests/pipelines/test_tools.py -k Csq -q`
Expected: FAIL — `AttributeError: module 'app.pipelines.tools' has no attribute 'bcftools_csq'`.

- [ ] **Step 3: Write the implementation**

Add to `backend/app/pipelines/tools.py`, directly below the `bcftools()` function:

```python
# `bcftools csq` landed in 1.7. Older builds have the binary and not the
# subcommand, which fails at run time rather than at probe time.
CSQ_MIN_VERSION = (1, 7)


@lru_cache(maxsize=1)
def bcftools_csq() -> Tool:
    """The consequence caller, as a capability of an already-probed binary.

    Not a `_probe` call: `csq` is a subcommand, so there is no separate
    executable to find and no `--version` of its own. What can go wrong is a
    bcftools too old to have it, and the Actions card needs to say that rather
    than "bcftools is missing" -- which would be false and would send the user
    looking for an install that is already there.
    """
    base = bcftools()
    if not base.available:
        return Tool(
            name="bcftools csq",
            path=None,
            version=None,
            error=f"bcftools is unavailable, so csq cannot run: {base.error}",
        )

    # An unparseable version is not evidence of being too old. Treating it as
    # such would disable a working tool over a cosmetic parse failure, so the
    # check only fires when a real version was read and it is below the floor.
    if _looks_like_version(base.version):
        parts = tuple(int(p) for p in base.version.split(".")[:2])
        if parts < CSQ_MIN_VERSION:
            return Tool(
                name="bcftools csq",
                path=base.path,
                version=base.version,
                error=(
                    f"bcftools {base.version} has no `csq` subcommand; "
                    f"{CSQ_MIN_VERSION[0]}.{CSQ_MIN_VERSION[1]} or newer is required."
                ),
            )

    return Tool(name="bcftools csq", path=base.path, version=base.version)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec -T api python -m pytest tests/pipelines/test_tools.py -k Csq -q`
Expected: 4 passed.

- [ ] **Step 5: Confirm the real image satisfies the floor**

Run: `docker compose exec -T api python -c "from app.pipelines import tools; t = tools.bcftools_csq(); print(t.available, t.version, t.error)"`
Expected: `True 1.21 None`

- [ ] **Step 6: Commit**

```bash
git add backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat: probe bcftools csq as a versioned capability"
```

---

### Task 3: The `csq` command builder

**Files:**
- Create: `backend/app/pipelines/csq_runner.py`
- Test: `backend/tests/pipelines/test_csq_runner.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_csq_runner.py`:

```python
"""The bcftools csq command.

The property worth testing is the phase flag. `csq` defaults to `-p r`, which
*requires* phased genotypes and exits 255 on the first unphased heterozygous
site -- and `bcftools call`, which produces every VCF this app annotates,
emits unphased genotypes. Measured on the real yeast VCF: the default aborts,
`-p a` annotates 4,152 of 6,641.
"""

from pathlib import Path

from app.pipelines import csq_runner


class TestBuildCsqCommand:
    def _cmd(self):
        return csq_runner.build_csq_command(
            bcftools_path="/usr/bin/bcftools",
            vcf=Path("/tmp/in.vcf.gz"),
            reference=Path("/tmp/ref.fa"),
            annotation=Path("/tmp/genes.gff3"),
            out=Path("/tmp/out.vcf.gz"),
        )

    # The regression this file exists for. Without it the job dies at runtime
    # with exit 255 on any heterozygous call.
    def test_passes_phase_a(self):
        cmd = self._cmd()
        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "a"

    def test_passes_reference_and_annotation(self):
        cmd = self._cmd()
        assert cmd[cmd.index("-f") + 1] == "/tmp/ref.fa"
        assert cmd[cmd.index("-g") + 1] == "/tmp/genes.gff3"

    def test_writes_compressed_output(self):
        cmd = self._cmd()
        assert cmd[cmd.index("-O") + 1] == "z"
        assert cmd[cmd.index("-o") + 1] == "/tmp/out.vcf.gz"

    def test_input_is_last_and_the_subcommand_is_csq(self):
        cmd = self._cmd()
        assert cmd[0] == "/usr/bin/bcftools"
        assert cmd[1] == "csq"
        assert cmd[-1] == "/tmp/in.vcf.gz"


class TestGffWarnings:
    """Real NCBI GFF3 files emit these on every run -- the T. brucei
    annotation produces all three. They are not failures."""

    def test_recognises_benign_parse_warnings(self):
        for line in (
            "Warning: Ignoring GFF feature with unknown phase .. NC_008409.1",
            "Warning: The GFF contains features with duplicate id .. NC_008409.1",
            "Warning: Ignoring transcript with unknown biotype .. NC_007276.1",
            "Note: truncated transcript rna-XM_842566.1 with incomplete CDS",
        ):
            assert csq_runner.is_benign_gff_warning(line)

    def test_does_not_swallow_a_real_error(self):
        assert not csq_runner.is_benign_gff_warning(
            "Unphased heterozygous genotype at NC_001133.9:88609"
        )
        assert not csq_runner.is_benign_gff_warning("[E::faidx] Failed to open ref.fa")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec -T api python -m pytest tests/pipelines/test_csq_runner.py -q`
Expected: collection error — no module named `app.pipelines.csq_runner`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/csq_runner.py`:

```python
"""Running `bcftools csq` to add consequence annotations to a VCF.

The command is small; the two decisions in it are not, and both were settled by
running the tool against the real yeast callset rather than from the manual.
"""

from pathlib import Path

# How to treat unphased heterozygous genotypes.
#
# `csq` defaults to "r", which *requires* phasing and exits 255 on the first
# unphased het -- and `bcftools call`, which produced every VCF this app
# annotates, emits unphased genotypes. So the default is not merely suboptimal
# here, it fails every run.
#
# Measured on DRR1066343.bcftools.vcf.gz (6,641 variants):
#     -p r  exit 255, aborts
#     -p a  4,152 annotated
#     -p m  4,149 annotated
#     -p s  3,355 annotated
#
# "a" takes genotypes as they are and creates haplotypes regardless of phase.
# That is an arbitrary phase, which is the honest choice for data carrying
# none, and it only affects haplotype-aware calls across adjacent variants.
# "s" was rejected for silently dropping ~800 heterozygous sites: a quietly
# incomplete table is worse than an approximate one.
PHASE_MODE = "a"

# Substrings of the lines real NCBI GFF3 files produce on a normal run. The
# T. brucei annotation emits all of these and still annotates correctly.
_BENIGN_GFF_MARKERS = (
    "unknown phase",
    "duplicate id",
    "unknown biotype",
    "incomplete CDS",
)


def build_csq_command(
    *,
    bcftools_path: str,
    vcf: Path,
    reference: Path,
    annotation: Path,
    out: Path,
) -> list[str]:
    """`bcftools csq` over one VCF, writing a bgzipped VCF.

    The reference needs an accompanying `.fai`; callers stage that the same way
    the alignment and variant runners already do.
    """
    return [
        bcftools_path,
        "csq",
        "-f",
        str(reference),
        "-g",
        str(annotation),
        "-p",
        PHASE_MODE,
        "-O",
        "z",
        "-o",
        str(out),
        str(vcf),
    ]


def is_benign_gff_warning(line: str) -> bool:
    """Whether a stderr line is ordinary GFF3 noise rather than a failure.

    Real NCBI annotations are not clean by bcftools' standards -- partial
    features, duplicate ids, tRNA biotypes it does not model. Every one of
    those is a warning about a record it skipped, not about the run. Logged at
    debug rather than surfaced, or a successful annotation would look alarming.
    """
    return any(marker in line for marker in _BENIGN_GFF_MARKERS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec -T api python -m pytest tests/pipelines/test_csq_runner.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/csq_runner.py backend/tests/pipelines/test_csq_runner.py
git commit -m "feat: build the bcftools csq command with -p a"
```

---

### Task 4: Consequence columns in the variant index

**Files:**
- Modify: `backend/app/pipelines/variant_db.py`
- Modify: `backend/app/pipelines/vcf_stats_runner.py`
- Test: `backend/tests/pipelines/test_variant_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/pipelines/test_variant_db.py`. Note the conventions already in that file, which this code follows: names are imported directly (`build_variant_db`, `query_variants`, `count_variants`, `VariantFilters`), **every function is keyword-only** (`build_variant_db(rows=..., db_path=...)`), and rows carry no trailing newline.

```python
class TestConsequenceColumns:
    """The annotated columns, which arrive as a 9th TSV field holding the raw
    BCSQ value. An un-annotated VCF sends "." there, which must round-trip as
    empty rather than as the string "."."""

    def test_stores_gene_consequence_and_aa_change(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(
                [
                    "NC_001133.9\t22639\tA\tT\t10.8\t.\t1\t1/1\t"
                    "missense|YAL063C-A|rna-NM_001184642.1|protein_coding|-|16F>16Y|22639A>T"
                ]
            ),
            db_path=path,
        )
        row = query_variants(
            db_path=path, filters=VariantFilters(), offset=0, limit=10
        )[0]
        assert row["gene"] == "YAL063C-A"
        assert row["consequence"] == "missense"
        assert row["aa_change"] == "16F>16Y"
        assert row["aa_pos"] == 16

    def test_an_unannotated_row_has_empty_consequence_columns(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(["NC_001133.9\t12690\tA\tT\t30.0\t.\t5\t0/1\t."]),
            db_path=path,
        )
        row = query_variants(
            db_path=path, filters=VariantFilters(), offset=0, limit=10
        )[0]
        assert row["gene"] is None
        assert row["consequence"] is None

    # A VCF indexed before this change has only 8 fields per line.
    def test_a_row_with_no_consequence_field_still_loads(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(["NC_001133.9\t12690\tA\tT\t30.0\t.\t5\t0/1"]),
            db_path=path,
        )
        rows = query_variants(
            db_path=path, filters=VariantFilters(), offset=0, limit=10
        )
        assert len(rows) == 1
        assert rows[0]["consequence"] is None

    # Two samples plus BCSQ: the genotypes must not absorb the consequence,
    # and the consequence must not absorb a genotype.
    def test_multi_sample_genotypes_survive_the_appended_consequence(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(
                [
                    "c1\t1\tA\tT\t10\t.\t5\t0/1\t1/1\t"
                    "missense|G1|r1|protein_coding|+|1A>1B|x"
                ]
            ),
            db_path=path,
        )
        row = query_variants(
            db_path=path, filters=VariantFilters(), offset=0, limit=10
        )[0]
        assert row["gt"] == "0/1\t1/1"
        assert row["consequence"] == "missense"

    def _two_rows(self):
        return iter(
            [
                "c1\t1\tA\tT\t10\t.\t1\t1/1\tmissense|G1|r1|protein_coding|+|1A>1B|x",
                "c1\t2\tA\tT\t10\t.\t1\t1/1\tsynonymous|G2|r2|protein_coding|+|2C|x",
            ]
        )

    def test_filters_by_consequence(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(rows=self._two_rows(), db_path=path)
        rows = query_variants(
            db_path=path,
            filters=VariantFilters(consequence="missense"),
            offset=0,
            limit=10,
        )
        assert len(rows) == 1
        assert rows[0]["gene"] == "G1"

    def test_counts_respect_the_consequence_filter(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(rows=self._two_rows(), db_path=path)
        assert (
            count_variants(
                db_path=path, filters=VariantFilters(consequence="missense")
            )
            == 1
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec -T api python -m pytest tests/pipelines/test_variant_db.py -k Consequence -q`
Expected: FAIL — `KeyError: 'gene'` or a `TypeError` about an unexpected `consequence` argument.

- [ ] **Step 3: Extend the schema and the row builder**

In `backend/app/pipelines/variant_db.py`, change the `CREATE TABLE` to:

```python
        con.execute(
            """
            CREATE TABLE variants (
              chrom  TEXT,
              pos    INTEGER,
              ref    TEXT,
              alt    TEXT,
              qual   REAL,
              filter TEXT,
              dp     INTEGER,
              gt     TEXT,
              gene        TEXT,
              consequence TEXT,
              aa_change   TEXT,
              aa_pos      INTEGER
            )
            """
        )
```

Add the import at the top of the file:

```python
from app.pipelines import csq_parse
```

The genotype column currently absorbs `parts[7:]`, because `[\t%GT]` repeats per sample. BCSQ is appended *after* those, so the consequence is the **last** field and the genotypes are everything between. Replace the row-building block:

```python
            batch.append(
                (
                    parts[0],
                    pos,
                    parts[2],
                    parts[3],
                    qual,
                    parts[5],
                    int(dp) if dp is not None else None,
                    "\t".join(parts[7:]),
                )
            )
```

with:

```python
            # BCSQ is appended after the repeating per-sample genotypes, so it
            # is the last field and the genotypes are everything between.
            # Older indexes were built with no 9th field at all; those keep
            # working and simply carry no consequence.
            if len(parts) >= 9:
                gt_text = "\t".join(parts[7:-1])
                csq = csq_parse.parse_bcsq(parts[-1])
            else:
                gt_text = "\t".join(parts[7:])
                csq = None

            batch.append(
                (
                    parts[0],
                    pos,
                    parts[2],
                    parts[3],
                    qual,
                    parts[5],
                    int(dp) if dp is not None else None,
                    gt_text,
                    csq.gene if csq else None,
                    csq.consequence if csq else None,
                    csq.aa_change if csq else None,
                    csq.aa_pos if csq else None,
                )
            )
```

Update both `INSERT INTO variants VALUES (?,?,?,?,?,?,?,?)` occurrences to twelve placeholders:

```python
"INSERT INTO variants VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
```

Add the index beside the existing two:

```python
        con.execute("CREATE INDEX ix_variants_consequence ON variants(consequence)")
```

- [ ] **Step 4: Add the filter**

Add to `VariantFilters`:

```python
    consequence: str | None = None
```

`_where()` is the shared clause builder used by both `query_variants` and `count_variants` — they must stay in agreement, as its docstring says. Add this beside the existing `filter_value` block, using the same `clauses` / `args` names that function already uses:

```python
    if filters.consequence:
        clauses.append("consequence = ?")
        args.append(filters.consequence)
```

Then widen the explicit column list in `query_variants` — it selects named columns, not `*`, so new columns are invisible until listed. Change:

```python
            f"SELECT chrom,pos,ref,alt,qual,filter,dp,gt FROM variants{where} "
```

to:

```python
            f"SELECT chrom,pos,ref,alt,qual,filter,dp,gt,"
            f"gene,consequence,aa_change,aa_pos FROM variants{where} "
```

The `row_factory = sqlite3.Row` and `dict(r)` mapping below it need no change — they pick up whatever was selected.

- [ ] **Step 5: Add BCSQ to the query format**

In `backend/app/pipelines/vcf_stats_runner.py`, change:

```python
QUERY_FORMAT = "%CHROM\t%POS\t%REF\t%ALT\t%QUAL\t%FILTER[\t%DP][\t%GT]\n"
```

to:

```python
# BCSQ last, after the repeating per-sample genotypes, so the consequence is
# always the final field however many samples the file has. `-u` in
# build_query_command already makes an undefined tag emit "." rather than
# failing the job, which is what every un-annotated VCF does here.
QUERY_FORMAT = "%CHROM\t%POS\t%REF\t%ALT\t%QUAL\t%FILTER[\t%DP][\t%GT]\t%INFO/BCSQ\n"
```

- [ ] **Step 6: Run the tests**

Run: `docker compose exec -T api python -m pytest tests/pipelines/test_variant_db.py tests/pipelines/test_vcf_stats_runner.py -q`
Expected: all pass, including the pre-existing tests in both files.

- [ ] **Step 7: Run the whole backend suite**

Run: `docker compose exec -T api python -m pytest tests/ -q`
Expected: no failures. The genotype-splitting change touches a path shared with multi-sample VCFs, so a green full suite is the check that matters here.

- [ ] **Step 8: Commit**

```bash
git add backend/app/pipelines/variant_db.py backend/app/pipelines/vcf_stats_runner.py backend/tests/pipelines/test_variant_db.py
git commit -m "feat: index gene, consequence and amino-acid change per variant"
```

---

### Task 5: Verify against the real database

**Files:** none — this is the check CLAUDE.md asks for, and the reason it exists is that the suggestion rules once passed a full green suite while being wrong about real files.

- [ ] **Step 1: Confirm the parser agrees with bcftools on real output**

The annotated yeast VCF from the design work may still be at `/tmp/y_a.vcf` in the `api` container. If it is gone, regenerate it first (see the spec's Findings section for the command).

Run:

```bash
docker compose exec -T api python -c "
import subprocess
from app.pipelines import csq_parse
out = subprocess.run(['bcftools','query','-u','-f','%INFO/BCSQ\n','/tmp/y_a.vcf'], capture_output=True, text=True).stdout
parsed = [csq_parse.parse_bcsq(l) for l in out.splitlines()]
kept = [c for c in parsed if c]
print('lines', len(parsed), 'parsed', len(kept))
from collections import Counter
print(Counter(c.consequence for c in kept).most_common(6))
"
```

Expected: `parsed` is 4,152, and the top consequences are `synonymous` (~2,138) then `missense` (~1,633). A materially lower count means the parser is dropping shapes bcftools emitted.

- [ ] **Step 2: Confirm an un-annotated VCF still indexes**

Run:

```bash
docker compose exec -T api python -c "
from app.pipelines import vcf_stats_runner as v
print(v.QUERY_FORMAT.replace(chr(9),'<TAB>'))
"
```

Then re-run "recompute results" on a VCF from the UI at localhost:5173 and confirm the variants table still paginates with empty consequence columns rather than erroring.

- [ ] **Step 3: Commit any fixes**

If either check fails, fix and commit before moving on. If both pass, nothing to commit.

---

## Notes for the implementer

**Do not add the run kind, queue handler, Actions card, or frontend columns.** They are the follow-on plan. Stopping here keeps this reviewable and means the parser is proven before anything is built on it.

**`-p a` is not a tuning knob.** It is the difference between working and exit 255. If you find yourself changing it, re-read the measurement in the constant's comment first.

**Do not "clean up" the GFF warnings.** Real NCBI annotation files produce them on every successful run.

**The genotype column is the risky edit.** `parts[7:]` currently absorbs all remaining fields because `[\t%GT]` repeats per sample; with BCSQ appended, the genotypes become `parts[7:-1]`. Get this wrong on a multi-sample VCF and the last sample's genotype silently becomes the consequence string. The full-suite run in Task 4 Step 7 is what catches it.
