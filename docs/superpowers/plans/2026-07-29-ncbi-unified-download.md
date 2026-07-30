# Unified NCBI Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One NCBI accession box that accepts runs, experiments, samples, studies, BioProjects, BioSamples and GenBank/RefSeq assemblies -- downloading assembly components (genome FASTA, GFF3 annotation, protein FASTA, CDS FASTA) as identifiable, correctly-roled objects, with BioProject runs grouped by experiment.

**Architecture:** Assembly accessions are classified before the SRA `esearch` path and routed to a new resolver backed by the `datasets` CLI. A new `download_assembly` job (sibling to `download_sra_run`, not a branch inside it) fetches a zip, verifies it, extracts it, and labels each file from the zip's own `dataset_catalog.json`. Three new `ObjectRole` values make the non-genome components identifiable in the explorer and keep them out of the aligner's reference picker. Experiment grouping is derived on the frontend from data the resolver already returns.

**Tech Stack:** Python 3.12, FastAPI, Beanie/MongoDB, Redis, pytest; React + TypeScript, TanStack Query; NCBI `datasets` CLI v2 (Go binary); Docker Compose.

**Spec:** `docs/superpowers/specs/2026-07-29-ncbi-unified-download-design.md`

---

## Critical context for the implementer

Read this before starting. These facts were verified against `datasets` 18.30.1 and the live Datasets v2alpha API on 2026-07-29, and several are counter-intuitive.

**1. The `.fna` collision is the central trap.** A downloaded package contains *two* `.fna` files in the same directory:

```
ncbi_dataset/data/GCF_000002445.2/GCF_000002445.2_ASM244v1_genomic.fna   <- genome
ncbi_dataset/data/GCF_000002445.2/cds_from_genomic.fna                   <- CDS
```

If you label components by extension, or match `*_genomic.fna` before `cds_from_genomic.fna`, a CDS file becomes an `ObjectRole.REFERENCE` and appears in the aligner's reference dropdown. Selecting it would produce silently wrong alignments. Task 8 exists to prevent this and Task 9 tests it specifically.

**2. Prefer the zip's own catalog over filenames.** `ncbi_dataset/data/dataset_catalog.json` states each file's type:

```json
{"filePath": "GCF_000002445.2/cds_from_genomic.fna",
 "fileType": "CDS_NUCLEOTIDE_FASTA", "uncompressedLengthBytes": "15188456"}
```

Types seen: `GENOMIC_NUCLEOTIDE_FASTA`, `CDS_NUCLEOTIDE_FASTA`, `PROTEIN_FASTA`, `GFF3`, `DATA_REPORT`.

**3. `--no-progressbar` is mandatory.** Without it the CLI emits an ANSI cursor-up progress bar -- roughly 40 near-identical lines for a trivial download -- which floods the job log and makes `_log_tail` useless for diagnosing failures.

**4. GenBank assemblies usually have no annotation.** `annotation_info` is present for `GCF_000002445.2` and `GCF_000001405.40`, absent for `GCA_000001405.29`. Three of the four checkboxes being disabled is the *normal* GCA case, not an edge case.

**5. A missing assembly returns `{}`, not an HTTP error.** `GCA_000002445.2`'s `dataset_report` is an empty JSON object with no `reports` key. Distinguish "not found" from "found, genome only".

**6. `datasets` is not in the worker image.** Verified: `docker compose exec worker which datasets` finds nothing. Task 1 adds it.

**7. The worker does not hot-reload.** After any change under `backend/app/queue/`, run `docker compose restart worker` **from the main repo root** (`/Users/syntheticgio/Programming/local-bio-pipeliner`), never from a worktree -- see `CLAUDE.md`. Skipping this makes a working fix look broken.

**8. Run pytest inside the container**, not a host venv:

```bash
docker compose exec api python -m pytest tests/ -q
```

---

## File structure

**Backend -- create:**

| File | Responsibility |
|---|---|
| `backend/app/metadata/assembly_components.py` | Which components an assembly offers, and what each maps to. The component table lives here. |
| `backend/app/queue/assembly_handlers.py` | The `download_assembly` job: preview, disk check, download, verify, extract, label. |
| `backend/app/services/assembly_service.py` | Launch validation and job creation for an assembly download. |
| `backend/app/api/v1/ncbi.py` | The unified router. Absorbs `sra.py`. |
| `backend/app/queue/download_failures.py` | `_download_failure`, shared by the SRA and assembly handlers. |

**Backend -- modify:**

| File | Change |
|---|---|
| `backend/Dockerfile` | Install the `datasets` CLI. |
| `backend/app/config.py:65` | Add `datasets_path`. |
| `backend/app/pipelines/tools.py` | Add `datasets()` probe, `all_tools()` entry, `TOOL_META` entry. |
| `backend/app/models/object.py:50-74` | Add `ANNOTATION`, `PROTEIN`, `TRANSCRIPT` to `ObjectRole`. |
| `backend/app/models/run.py:24-28` | Add `RunKind.ASSEMBLY_DOWNLOAD`. |
| `backend/app/metadata/schemas.py` | Add `SEQUENCE_SET_FIELDS`, wire `ROLE_FIELDS` and `FORMAT_DERIVED_ROLES`. |
| `backend/app/metadata/sra_resolver.py:146-174` | Classify assemblies; short-circuit `resolve()`. |
| `backend/app/metadata/assembly.py` | Add `component_availability()` from `annotation_info`. |
| `backend/app/queue/sra_handlers.py:291-321` | Move `_download_failure` out to the shared module. |
| `backend/app/queue/results.py` | Add `_apply_assembly_download` + registry entry. |
| `backend/app/queue/handlers.py` | Import `assembly_handlers` for registration. |
| `backend/app/api/v1/__init__.py` | Register the `ncbi` router. |

**Frontend -- modify:**

| File | Change |
|---|---|
| `frontend/src/api/types.ts:43-47` | Extend `ObjectRole`; add assembly response types. |
| `frontend/src/api/client.ts` | Add `ncbiResolve`, `ncbiDownloadAssembly`. |
| `frontend/src/components/SraDownloadDialog.tsx` | Rename to `NcbiDownloadDialog.tsx`; add assembly branch + grouping. |
| `frontend/src/components/ProjectExplorer.tsx:138-190` | New categories and role bucketing. |
| `frontend/src/components/DetailPanel.tsx:373` | Role-aware labels. |
| `frontend/src/index.css` | Styles for group headers and the assembly card. |

**Build order rationale:** roles and identification (Tasks 2-4) land *before* anything can download a protein FASTA, so there is never a window where an unroled FASTA can reach the reference dropdown. The CLI dependency (Task 1) comes first because everything else can be tested against it.

---

## Task 1: Install the `datasets` CLI and probe it

**Files:**
- Modify: `backend/Dockerfile` (after the Clair3 layer, before the app copy)
- Modify: `backend/app/config.py:65`
- Modify: `backend/app/pipelines/tools.py`
- Test: `backend/tests/pipelines/test_tools_datasets.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_tools_datasets.py`:

```python
"""The datasets CLI probe.

Version parsing is the part worth testing: `datasets --version` prints
"datasets version: 18.30.1", which is a different shape from the bare
"1.2.3" most tools print, and the tools panel shows whatever this returns.
"""

from app.pipelines import tools


class TestDatasetsProbe:
    def test_datasets_is_in_all_tools(self):
        """A tool absent from all_tools() never appears in the tools panel,
        so a missing dependency would surface as a confusing job failure
        instead of a visible "not installed"."""
        names = [t.name for t in tools.all_tools()]
        assert "datasets" in names

    def test_datasets_has_tool_meta(self):
        """all_tools_with_meta joins on name; a missing entry means the tool
        renders with no description at all."""
        metas = {m["name"]: m for m in tools.all_tools_with_meta()}
        assert "datasets" in metas
        assert metas["datasets"]["pipelines"] == ["download"]

    def test_version_prefix_is_stripped(self):
        """`datasets --version` prints "datasets version: 18.30.1". Showing
        the whole line in a version column would be noise."""
        assert tools._clean_version("datasets version: 18.30.1") == "18.30.1"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools_datasets.py -v
```

Expected: FAIL — `assert 'datasets' in names`.

- [ ] **Step 3: Add the config setting**

In `backend/app/config.py`, after line 65 (`nanoplot_path`):

```python
    # The NCBI Datasets CLI: how assemblies (GCA/GCF) are downloaded, as
    # fasterq-dump is how runs are. Installed in the worker image by the
    # Dockerfile; overridable for a host that has it elsewhere.
    datasets_path: str = "datasets"
```

- [ ] **Step 4: Add the probe and metadata**

In `backend/app/pipelines/tools.py`, after `prefetch()` (line 213):

```python
@lru_cache(maxsize=1)
def datasets() -> Tool:
    return _probe("datasets", settings.datasets_path, ["--version"])
```

Add `datasets(),` to the `all_tools()` list after `prefetch(),`.

Add to `TOOL_META`, after the `"prefetch"` entry:

```python
    "datasets": ToolMeta(
        pipelines=(PipelineType.DOWNLOAD,),
        summary=(
            "NCBI's Datasets CLI. Downloads a published assembly -- genome "
            "FASTA, annotation, protein and CDS sequences -- from a GenBank "
            "(GCA) or RefSeq (GCF) accession, which is how reference genomes "
            "arrive without a manual trip to the NCBI website."
        ),
        strengths=(
            "One accession fetches genome, annotation, protein and CDS together",
            "Ships an md5 manifest, so a truncated transfer is detectable",
            "Reports package contents and size before downloading anything",
        ),
    ),
```

- [ ] **Step 5: Verify `_clean_version` handles the output**

```bash
docker compose exec api python -c "
from app.pipelines import tools
print(repr(tools._clean_version('datasets version: 18.30.1')))"
```

Expected: `'18.30.1'`.

If it prints anything else, add a `datasets version:` strip to `_clean_version` — do not change the shared version regex in a way that affects other tools.

- [ ] **Step 6: Install the binary in the image**

In `backend/Dockerfile`, after the Clair3 layer and before the application copy. NCBI ships prebuilt binaries per architecture — no build step, unlike bwa-mem2. Both URLs were verified to return 200 (amd64 ~19.9 MB, arm64 ~19.4 MB):

```dockerfile
# --- NCBI Datasets CLI ------------------------------------------------------
#
# How assemblies are downloaded. A single static Go binary, so unlike
# bwa-mem2 there is nothing to compile for arm64 -- only a different URL.
ARG TARGETARCH
RUN set -e \
    && case "$TARGETARCH" in \
         arm64) DATASETS_ARCH=linux-arm64 ;; \
         *)     DATASETS_ARCH=linux-amd64 ;; \
       esac \
    && curl -fsSL \
         "https://ftp.ncbi.nlm.nih.gov/pub/datasets/command-line/v2/${DATASETS_ARCH}/datasets" \
         -o /usr/local/bin/datasets \
    && chmod +x /usr/local/bin/datasets \
    && datasets --version
```

The trailing `datasets --version` makes a broken download fail the build rather than the first job.

- [ ] **Step 7: Rebuild and verify the tool is present**

From the **main repo root**:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api worker
```

Then:

```bash
docker compose exec api datasets --version
```

Expected: `datasets version: 18.30.1` or later.

- [ ] **Step 8: Run the tests**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools_datasets.py -v
```

Expected: 3 passed.

- [ ] **Step 9: Commit**

```bash
git add backend/Dockerfile backend/app/config.py backend/app/pipelines/tools.py backend/tests/pipelines/test_tools_datasets.py
git commit -m "feat: install and probe the NCBI datasets CLI"
```

---

## Task 2: Add the three new object roles

**Files:**
- Modify: `backend/app/models/object.py:50-74`
- Test: `backend/tests/storage/test_object_roles.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/storage/test_object_roles.py`:

```python
"""The roles that make downloaded assembly components identifiable.

These exist because format cannot tell them apart: a reference genome, a
protein FASTA and a CDS FASTA are all FormatKind.FASTA. Without a role, a
protein FASTA is indistinguishable from a genome to every consumer -- most
consequentially the aligner's reference picker, which gates on
`role is ObjectRole.REFERENCE`.
"""

from app.models import ObjectRole


class TestAssemblyComponentRoles:
    def test_annotation_role_exists(self):
        assert ObjectRole.ANNOTATION == "annotation"

    def test_protein_role_exists(self):
        assert ObjectRole.PROTEIN == "protein"

    def test_transcript_role_exists(self):
        assert ObjectRole.TRANSCRIPT == "transcript"

    def test_sequence_roles_are_distinct_from_reference(self):
        """The whole point: a protein FASTA must never satisfy a
        `role is ObjectRole.REFERENCE` check."""
        assert ObjectRole.PROTEIN is not ObjectRole.REFERENCE
        assert ObjectRole.TRANSCRIPT is not ObjectRole.REFERENCE
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/storage/test_object_roles.py -v
```

Expected: FAIL — `AttributeError: ANNOTATION`.

- [ ] **Step 3: Add the roles**

In `backend/app/models/object.py`, inside `ObjectRole` after `VARIANTS` (line 74):

```python
    # An assembly's authoritative annotation. Format says "intervals" and
    # cannot distinguish NCBI's published GFF3 from a user's peak calls or
    # blacklist, which are the same format used for a different purpose.
    ANNOTATION = "annotation"
    # Amino acid sequences. The role that matters most: a protein FASTA and a
    # reference genome are both FormatKind.FASTA, and only this keeps one out
    # of the aligner's reference picker.
    PROTEIN = "protein"
    # CDS / transcript nucleotide sequences. The same hazard as PROTEIN and
    # slightly worse: `cds_from_genomic.fna` is nucleotide FASTA that would
    # pass any "does this look like a genome" sniff test.
    TRANSCRIPT = "transcript"
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose exec api python -m pytest tests/storage/test_object_roles.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/object.py backend/tests/storage/test_object_roles.py
git commit -m "feat: add ANNOTATION, PROTEIN and TRANSCRIPT object roles"
```

---

## Task 3: Give the new roles field vocabularies

**Files:**
- Modify: `backend/app/metadata/schemas.py` (near `REFERENCE_FIELDS:249`, `ROLE_FIELDS:313`, `FORMAT_DERIVED_ROLES:333`)
- Test: `backend/tests/metadata/test_schemas_roles.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/metadata/test_schemas_roles.py`:

```python
"""Every role must be deliberately accounted for.

`FORMAT_DERIVED_ROLES` exists so that adding a role without thinking about
its questions fails a test rather than silently showing a protein FASTA the
reference-genome form.
"""

from app.metadata import schemas
from app.models import FormatKind, ObjectRole


class TestEveryRoleIsAccountedFor:
    def test_each_role_has_fields_or_defers_to_format(self):
        for role in ObjectRole:
            has_own = role in schemas.ROLE_FIELDS
            defers = role in schemas.FORMAT_DERIVED_ROLES
            assert has_own or defers, (
                f"{role} is in neither ROLE_FIELDS nor FORMAT_DERIVED_ROLES. "
                "Decide which questions it deserves."
            )

    def test_no_role_both_has_fields_and_defers(self):
        """Both would be ambiguous: fields_for prefers ROLE_FIELDS, so the
        FORMAT_DERIVED_ROLES membership would be a lie."""
        overlap = set(schemas.ROLE_FIELDS) & schemas.FORMAT_DERIVED_ROLES
        assert not overlap, f"contradictory: {overlap}"


class TestSequenceSetFields:
    def test_protein_is_not_asked_reference_questions(self):
        """A protein FASTA has no assembly level and no scaffold N50. Asking
        would imply it is a genome."""
        keys = {f.key for f in schemas.fields_for(FormatKind.FASTA, ObjectRole.PROTEIN)}
        assert "assembly_accession" in keys
        assert "scaffold_n50" not in keys
        assert "is_primary_assembly" not in keys

    def test_transcript_shares_the_protein_vocabulary(self):
        """Both are sequence sets derived from an assembly; two vocabularies
        for one question shape would be worse than one shared."""
        protein = {f.key for f in schemas.fields_for(FormatKind.FASTA, ObjectRole.PROTEIN)}
        transcript = {f.key for f in schemas.fields_for(FormatKind.FASTA, ObjectRole.TRANSCRIPT)}
        assert protein == transcript

    def test_annotation_gets_the_interval_questions(self):
        """A GFF3's questions already exist as INTERVAL_FIELDS. Deferring to
        format reuses them rather than inventing a second interval vocabulary."""
        keys = {f.key for f in schemas.fields_for(FormatKind.GFF, ObjectRole.ANNOTATION)}
        assert "source" in keys

    def test_sequence_set_fields_are_known_for_validation(self):
        """all_known_fields drives coercion of unscoped edits; a field missing
        from it is treated as an unknown key and skips coercion."""
        known = schemas.all_known_fields()
        assert "sequence_count" in known
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/metadata/test_schemas_roles.py -v
```

Expected: FAIL — `ObjectRole.ANNOTATION is in neither ROLE_FIELDS nor FORMAT_DERIVED_ROLES`.

- [ ] **Step 3: Add `SEQUENCE_SET_FIELDS`**

In `backend/app/metadata/schemas.py`, after `REFERENCE_FIELDS` ends (before `INTERVAL_FIELDS`):

```python
# Protein and CDS FASTA downloaded alongside an assembly. Deliberately not
# REFERENCE_FIELDS: a protein FASTA has no assembly level, no primary-assembly
# distinction and no scaffold N50, and asking about them would imply it is a
# genome -- the exact confusion the PROTEIN role exists to prevent.
SEQUENCE_SET_FIELDS: tuple[FieldDef, ...] = (
    FieldDef("organism", "Organism", group="Sequences", suggested=True),
    FieldDef("assembly_accession", "Assembly accession",
             help="The assembly these sequences were derived from, "
                  "e.g. GCF_000002445.2.",
             group="Sequences", suggested=True),
    FieldDef("sequence_count", "Sequences", type=FieldType.INTEGER,
             help="Number of records in the file.", group="Sequences"),
    FieldDef("source", "Source",
             help="e.g. NCBI RefSeq annotation release 104.",
             group="Sequences", suggested=True),
)
```

If `FieldType.INTEGER` does not exist, check the enum and use the integer member's actual name.

- [ ] **Step 4: Wire the roles in**

Add to `ROLE_FIELDS` (line ~313):

```python
ROLE_FIELDS: dict[ObjectRole, tuple[FieldDef, ...]] = {
    ObjectRole.REFERENCE: REFERENCE_FIELDS,
    # Both sequence sets share one vocabulary: they differ in what the
    # sequences *are*, which the role already records, not in what is worth
    # asking about them.
    ObjectRole.PROTEIN: SEQUENCE_SET_FIELDS,
    ObjectRole.TRANSCRIPT: SEQUENCE_SET_FIELDS,
}
```

Add `ANNOTATION` to `FORMAT_DERIVED_ROLES` (line ~333), extending the existing comment block:

```python
# ANNOTATION joins them for the same reason: a published GFF3 and a
# user-supplied BED both describe intervals on a reference, and
# INTERVAL_FIELDS already asks the right questions. The role records that
# these annotations are NCBI's rather than the user's, which is provenance
# rather than a different question.
FORMAT_DERIVED_ROLES: frozenset[ObjectRole] = frozenset(
    {
        ObjectRole.TRIMMED_READS,
        ObjectRole.ALIGNMENT,
        ObjectRole.VARIANTS,
        ObjectRole.ANNOTATION,
    }
)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/metadata/test_schemas_roles.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Check nothing else regressed**

```bash
docker compose exec api python -m pytest tests/metadata/ -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/app/metadata/schemas.py backend/tests/metadata/test_schemas_roles.py
git commit -m "feat: field vocabularies for annotation, protein and transcript roles"
```

---

## Task 4: Show the new roles in the explorer and detail panel

**Files:**
- Modify: `frontend/src/api/types.ts:43-47`
- Modify: `frontend/src/components/ProjectExplorer.tsx:138-190`
- Modify: `frontend/src/components/DetailPanel.tsx:373`

No automated test: this repo has no frontend component-testing setup and none is expected (`CLAUDE.md`). Verification is manual.

- [ ] **Step 1: Extend the `ObjectRole` type**

In `frontend/src/api/types.ts`, replace lines 43-47:

```typescript
/** How a file is used, when its format cannot say. Null = derive from format. */
export type ObjectRole =
  | "reference"
  | "trimmed_reads"
  | "alignment"
  | "variants"
  /** An assembly's published annotation (GFF3). */
  | "annotation"
  /** Amino acid FASTA. Distinct from "reference" so it never reaches an
   * aligner's reference picker -- both are FASTA. */
  | "protein"
  /** CDS / transcript nucleotide FASTA. Same hazard as "protein". */
  | "transcript";
```

- [ ] **Step 2: Fix the explorer's categorization**

`categorizeFile` currently sends **all FASTA to "reads"** (`ProjectExplorer.tsx:155`), so without this a downloaded protein FASTA would file under Reads.

Replace `categorizeFile` (lines 149-161):

```typescript
function categorizeFile(obj: DataObject): FileCategory {
  // Role is an override: when set it decides outright, because the format
  // cannot tell a reference genome from a pile of reads -- nor from a protein
  // or CDS FASTA, which are the same format as both.
  if (obj.role === "reference") return "references";
  if (obj.role === "annotation") return "annotations";
  if (obj.role === "protein" || obj.role === "transcript") return "sequences";

  const kind = obj.format.kind.toLowerCase();
  if (kind === "fastq" || kind === "fasta") return "reads";
  if (["bam", "sam", "cram"].includes(kind)) return "alignments";
  if (["vcf", "bcf"].includes(kind)) return "variants";
  if (["bed", "gff", "gtf"].includes(kind)) return "annotations";
  if (kind === "hic") return "hic";
  return "other";
}
```

Add `"sequences"` to the `FileCategory` union (line 138):

```typescript
type FileCategory =
  | "reads"
  | "references"
  | "alignments"
  | "variants"
  | "annotations"
  /** Protein and CDS FASTA: derived from an assembly, not reads and not a
   * reference. */
  | "sequences"
  | "hic"
  | "other";
```

Add the key to the `categorizeObjects` initializer (lines 164-172) — `sequences: [],` after `annotations: [],`. Missing it would throw on push.

Add to `CATEGORIES` (line 182), after the annotations entry:

```typescript
  { key: "sequences", label: "Protein & CDS" },
```

- [ ] **Step 3: Make the detail panel's reference check role-aware**

`DetailPanel.tsx:373` reads `const isReference = obj.role === "reference";`. Leave it as-is — it is already correct and the new roles must not satisfy it. Verify by reading the surrounding block that nothing else assumes "FASTA implies reference":

```bash
grep -n "isReference" frontend/src/components/DetailPanel.tsx
```

If any use treats a FASTA without a role as a reference, note it but do not change behavior in this task.

- [ ] **Step 4: Verify it compiles**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors. A missing `sequences` key in `CategorizedFiles` surfaces here.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/components/ProjectExplorer.tsx
git commit -m "feat: explorer categories for annotation, protein and CDS files"
```

---

## Task 5: Classify assembly accessions in the resolver

**Files:**
- Modify: `backend/app/metadata/sra_resolver.py:146-174`, `370-395`
- Test: `backend/tests/metadata/test_sra_resolver.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/metadata/test_sra_resolver.py`:

```python
class TestAssemblyClassification:
    def test_refseq_accession_classifies_as_assembly(self):
        assert sra_resolver.classify("GCF_000002445.2") == "assembly"

    def test_genbank_accession_classifies_as_assembly(self):
        assert sra_resolver.classify("GCA_000001405.29") == "assembly"

    def test_lowercase_is_accepted(self):
        """Users paste from papers and spreadsheets; case is not signal."""
        assert sra_resolver.classify("gcf_000002445.2") == "assembly"

    def test_an_assembly_accession_is_resolvable(self):
        assert sra_resolver.is_resolvable("GCF_000002445.2") is True

    def test_an_unversioned_assembly_is_not_resolvable(self):
        """NCBI assembly accessions always carry a version. Rejecting it here
        gives an immediate answer instead of a round trip that finds nothing."""
        assert sra_resolver.is_resolvable("GCF_000002445") is False

    def test_resolve_does_not_send_an_assembly_to_esearch(self, monkeypatch):
        """db=sra&term=GCF_... finds nothing, so the user would be told "no
        sequencing runs found" -- true, and actively misleading."""
        called = []
        monkeypatch.setattr(
            sra_resolver, "search_uids",
            lambda *a, **k: called.append(a) or ([], 0),
        )
        result = sra_resolver.resolve("GCF_000002445.2")
        assert called == []
        assert result.kind == "assembly"
        assert result.error is not None
        assert "assembly" in result.error.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/metadata/test_sra_resolver.py -k Assembly -v
```

Expected: FAIL — `classify` returns `None`.

- [ ] **Step 3: Classify assemblies**

In `backend/app/metadata/sra_resolver.py`, add the import at the top with the other `app.metadata` import:

```python
from app.metadata import assembly, sra
```

In `classify()` (line 146), after the empty check and `upper = ...`:

```python
    # Checked before the INSDC prefixes: an assembly lives in a different NCBI
    # service (Datasets, not E-utilities) and resolves down a different path.
    if assembly.is_valid_accession(upper):
        return "assembly"
```

In `is_resolvable()` (line 162), after `kind = classify(upper)`:

```python
    if kind == "assembly":
        # is_valid_accession already required the version suffix, which is what
        # separates a resolvable accession from a bare GCF_000002445.
        return True
```

- [ ] **Step 4: Short-circuit `resolve()`**

In `resolve()` (line 370), immediately after `kind = classify(accession) or "unknown"`:

```python
    if kind == "assembly":
        # This resolver answers "which runs can I download". An assembly has
        # none: it is a published genome, resolved through
        # assembly_components and downloaded by a different handler. Returning
        # an explanatory error beats an esearch that truthfully reports no
        # sequencing runs and reads as "this accession is broken".
        return SraResolution(
            accession=accession,
            kind=kind,
            error=(
                f"{accession} is a genome assembly, not sequencing data. "
                "Resolve it through the assembly endpoint."
            ),
        )
```

Note the `is_resolvable` guard below it stays where it is — the assembly branch returns before reaching it.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/metadata/test_sra_resolver.py -v
```

Expected: all pass, including the pre-existing tests.

- [ ] **Step 6: Commit**

```bash
git add backend/app/metadata/sra_resolver.py backend/tests/metadata/test_sra_resolver.py
git commit -m "feat: classify GCA/GCF accessions as assemblies"
```

---

## Task 6: Component availability

**Files:**
- Create: `backend/app/metadata/assembly_components.py`
- Modify: `backend/app/metadata/assembly.py`
- Test: `backend/tests/metadata/test_assembly_components.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/metadata/test_assembly_components.py`:

```python
"""Which components an assembly actually offers.

Offering an annotation checkbox for an assembly that has none produces a
download that succeeds and ingests nothing, which reads as a bug. The
`--preview` JSON below is verbatim from `datasets` 18.30.1.
"""

import json

from app.metadata import assembly_components as ac


# GCF_000002445.2 -- fully annotated RefSeq assembly.
ANNOTATED_PREVIEW = json.dumps({
    "resource_updated_on": "2026-07-29T14:33:00Z",
    "record_count": 1,
    "estimated_file_size_mb": 15,
    "included_data_files": {
        "all_genomic_fasta": {"file_count": 1, "size_mb": 7.599519},
        "cds_fasta": {"file_count": 1, "size_mb": 4.0221567},
        "genome_gff": {"file_count": 1, "size_mb": 1.3584499},
        "prot_fasta": {"file_count": 1, "size_mb": 2.4885273},
    },
})

# GCA_000001405.29 -- GenBank, genome only. The common GCA shape.
GENOME_ONLY_PREVIEW = json.dumps({
    "resource_updated_on": "2026-07-29T14:33:00Z",
    "record_count": 1,
    "estimated_file_size_mb": 927,
    "included_data_files": {
        "all_genomic_fasta": {"file_count": 1, "size_mb": 927.9705},
        "cds_fasta": {"file_count": 0, "size_mb": 0},
        "genome_gff": {"file_count": 0, "size_mb": 0},
        "prot_fasta": {"file_count": 0, "size_mb": 0},
    },
})


class TestParsePreview:
    def test_annotated_assembly_offers_everything(self):
        components = ac.parse_preview(ANNOTATED_PREVIEW)
        available = {c.key for c in components if c.available}
        assert available == {"genome", "gff3", "protein", "cds"}

    def test_sizes_come_through_in_bytes(self):
        """The disk pre-flight and the dialog both need bytes; size_mb is a
        float of megabytes and converting in two places would drift."""
        components = {c.key: c for c in ac.parse_preview(ANNOTATED_PREVIEW)}
        assert components["genome"].size_bytes == int(7.599519 * 1_000_000)

    def test_genome_only_assembly_offers_only_genome(self):
        """file_count 0 is how the CLI reports an unavailable component."""
        components = {c.key: c for c in ac.parse_preview(GENOME_ONLY_PREVIEW)}
        assert components["genome"].available is True
        assert components["gff3"].available is False
        assert components["protein"].available is False
        assert components["cds"].available is False

    def test_unavailable_components_carry_a_reason(self):
        """A disabled checkbox with no explanation reads as broken."""
        components = {c.key: c for c in ac.parse_preview(GENOME_ONLY_PREVIEW)}
        assert components["gff3"].reason

    def test_unparseable_preview_returns_none(self):
        """None means "could not determine", which the caller distinguishes
        from "determined that nothing is available"."""
        assert ac.parse_preview("not json") is None
        assert ac.parse_preview("") is None


class TestFallbackFromReport:
    # Note: from_report returns a dict keyed by component, unlike
    # parse_preview which returns a list. Iterating it directly would yield
    # the string keys, not the ComponentAvailability values.

    def test_annotation_info_present_offers_all_components(self):
        """The API fallback is coarser than --preview: it says the assembly
        has annotation without saying which files exist, so all three
        non-genome components are offered together."""
        components = ac.from_report({"annotation_info": {"name": "x"}})
        assert all(c.available for c in components.values())

    def test_annotation_info_absent_offers_genome_only(self):
        components = ac.from_report({"assembly_info": {}})
        assert components["genome"].available is True
        assert components["gff3"].available is False

    def test_genbank_reason_points_at_the_refseq_twin(self):
        """A GCA usually has no annotation while its GCF twin does. Naming the
        paired accession saves the user learning that themselves."""
        components = ac.from_report({"paired_accession": "GCF_000001405.40"})
        assert "GCF_000001405.40" in components["gff3"].reason


class TestComponentRoles:
    def test_each_component_maps_to_its_role(self):
        assert ac.COMPONENTS["genome"].role == "reference"
        assert ac.COMPONENTS["gff3"].role == "annotation"
        assert ac.COMPONENTS["protein"].role == "protein"
        assert ac.COMPONENTS["cds"].role == "transcript"

    def test_genome_is_mandatory(self):
        """Every other component describes coordinates or products of the
        genome sequence and is close to uninterpretable without it."""
        assert ac.COMPONENTS["genome"].mandatory is True
        assert ac.COMPONENTS["gff3"].mandatory is False
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/metadata/test_assembly_components.py -v
```

Expected: FAIL — `ModuleNotFoundError: app.metadata.assembly_components`.

- [ ] **Step 3: Write the module**

Create `backend/app/metadata/assembly_components.py`:

```python
"""What an assembly offers for download, and what each component becomes.

The component table is the single place that maps an NCBI `--include` name to
the object role its file lands as. It is deliberately one table rather than
knowledge spread across the handler, the applier and the dialog: the
consequence of disagreement is a CDS FASTA roled as a reference genome,
sitting in the aligner's reference picker.

Availability has two sources, in order of preference:

1. `datasets ... --preview`, which reports per-component file counts and exact
   sizes without transferring anything. Preferred because it is the same tool
   that will perform the download and it answers per-component.
2. The presence of `annotation_info` in a Datasets API report. Coarser -- it
   says "this assembly has annotation" without distinguishing GFF3 from
   protein from CDS -- and used only when the CLI is unavailable.

Both degrade to "genome only", which is always true: every assembly has a
genome sequence.
"""

import json
from dataclasses import dataclass

from app.logging import get_logger

log = get_logger(__name__)

# Megabytes as NCBI reports them: decimal, not binary. Converted here once so
# the dialog and the disk pre-flight cannot disagree about a factor of 1.05.
_MB = 1_000_000


@dataclass(frozen=True)
class ComponentSpec:
    """One downloadable part of an assembly, and what it becomes on ingest."""

    key: str  # the `--include` name
    label: str
    role: str  # the ObjectRole value its file lands as
    # The `included_data_files` key `--preview` reports it under. These names
    # do not match the --include names (`gff3` is reported as `genome_gff`),
    # which is exactly why this mapping is written down.
    preview_key: str
    # `dataset_catalog.json`'s fileType for this component -- the primary
    # labeling source after extraction.
    file_type: str
    mandatory: bool = False


COMPONENTS: dict[str, ComponentSpec] = {
    "genome": ComponentSpec(
        key="genome",
        label="Genome FASTA",
        role="reference",
        preview_key="all_genomic_fasta",
        file_type="GENOMIC_NUCLEOTIDE_FASTA",
        # Not selectable-off: every other component describes coordinates or
        # products of this sequence.
        mandatory=True,
    ),
    "gff3": ComponentSpec(
        key="gff3",
        label="Annotation (GFF3)",
        role="annotation",
        preview_key="genome_gff",
        file_type="GFF3",
    ),
    "protein": ComponentSpec(
        key="protein",
        label="Protein FASTA",
        role="protein",
        preview_key="prot_fasta",
        file_type="PROTEIN_FASTA",
    ),
    "cds": ComponentSpec(
        key="cds",
        label="CDS FASTA",
        role="transcript",
        preview_key="cds_fasta",
        file_type="CDS_NUCLEOTIDE_FASTA",
    ),
}

# Ordered for display: genome first because it is mandatory, then annotation
# (the most-wanted extra), then the two sequence sets.
COMPONENT_ORDER = ("genome", "gff3", "protein", "cds")


@dataclass
class ComponentAvailability:
    key: str
    label: str
    role: str
    available: bool
    size_bytes: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "role": self.role,
            "available": self.available,
            "size_bytes": self.size_bytes,
            "reason": self.reason,
        }


def parse_preview(body: str) -> list[ComponentAvailability] | None:
    """Component availability from `datasets ... --preview` output.

    Returns None when the output cannot be parsed at all, which the caller
    must distinguish from "parsed, and nothing is available": the first means
    fall back to the API report, the second is a real answer about a
    genome-only assembly.
    """
    try:
        payload = json.loads(body)
        files = payload["included_data_files"]
    except (ValueError, KeyError, TypeError):
        log.debug("assembly_preview_unparseable")
        return None
    if not isinstance(files, dict):
        return None

    out: list[ComponentAvailability] = []
    for key in COMPONENT_ORDER:
        spec = COMPONENTS[key]
        entry = files.get(spec.preview_key)
        entry = entry if isinstance(entry, dict) else {}
        count = entry.get("file_count") or 0
        size_mb = entry.get("size_mb") or 0
        available = bool(count)
        out.append(
            ComponentAvailability(
                key=spec.key,
                label=spec.label,
                role=spec.role,
                available=available,
                size_bytes=int(size_mb * _MB) if size_mb else None,
                reason=None if available else _unavailable_reason(spec, None),
            )
        )
    return out


def from_report(report: dict) -> dict[str, ComponentAvailability]:
    """Availability inferred from a Datasets API report.

    The fallback path. `annotation_info` presence is the only signal, so all
    three non-genome components share one answer -- coarser than `--preview`,
    but enough to avoid offering annotation for an assembly that has none.
    """
    report = report if isinstance(report, dict) else {}
    annotated = isinstance(report.get("annotation_info"), dict)
    paired = report.get("paired_accession")
    paired = paired if isinstance(paired, str) and paired.strip() else None

    out: dict[str, ComponentAvailability] = {}
    for key in COMPONENT_ORDER:
        spec = COMPONENTS[key]
        available = spec.mandatory or annotated
        out[key] = ComponentAvailability(
            key=spec.key,
            label=spec.label,
            role=spec.role,
            available=available,
            reason=None if available else _unavailable_reason(spec, paired),
        )
    return out


def _unavailable_reason(spec: ComponentSpec, paired: str | None) -> str:
    """Why a component is greyed out, in terms the user can act on.

    A GenBank assembly usually has no annotation while its RefSeq twin does,
    so naming the paired accession turns a dead end into a next step.
    """
    if paired and paired.upper().startswith("GCF_"):
        return (
            f"Not available for this assembly. The RefSeq version "
            f"({paired}) has annotation."
        )
    return "Not available for this assembly."


def include_argument(keys: list[str]) -> str:
    """The `--include` value for the selected components.

    Genome is forced in: it is mandatory, and a request that omits it is a
    frontend bug rather than an intent worth honoring.
    """
    selected = [k for k in COMPONENT_ORDER if k in set(keys)]
    if "genome" not in selected:
        selected.insert(0, "genome")
    return ",".join(selected)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/metadata/test_assembly_components.py -v
```

Expected: 12 passed.

- [ ] **Step 5: Add the live availability lookup**

Append to `backend/app/metadata/assembly.py`:

```python
def component_availability(accession: str) -> list | None:
    """What this assembly offers, preferring the CLI's own preview.

    Best-effort in the same way `lookup` is: a failure here costs accurate
    checkboxes, not the ability to download a genome.
    """
    import subprocess

    from app.config import settings
    from app.metadata import assembly_components

    if not is_valid_accession(accession):
        return None
    accession = accession.strip().upper()

    try:
        completed = subprocess.run(
            [
                settings.datasets_path,
                "download",
                "genome",
                "accession",
                accession,
                "--include",
                "genome,gff3,protein,cds",
                "--preview",
                # Without this the CLI writes an ANSI progress bar that buries
                # the JSON.
                "--no-progressbar",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("assembly_preview_failed", accession=accession, error=str(e))
        return None

    if completed.returncode == 0:
        parsed = assembly_components.parse_preview(completed.stdout)
        if parsed is not None:
            return parsed

    log.info("assembly_preview_unusable", accession=accession, code=completed.returncode)
    return None
```

- [ ] **Step 6: Verify against the live CLI**

```bash
docker compose exec api python -c "
from app.metadata import assembly
for acc in ('GCF_000002445.2', 'GCA_000001405.29'):
    got = assembly.component_availability(acc)
    print(acc, [(c.key, c.available) for c in (got or [])])"
```

Expected: `GCF_000002445.2` all four `True`; `GCA_000001405.29` genome `True` and the rest `False`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/metadata/assembly_components.py backend/app/metadata/assembly.py backend/tests/metadata/test_assembly_components.py
git commit -m "feat: detect which components an assembly offers"
```

---

## Task 7: Share the download failure classifier

**Files:**
- Create: `backend/app/queue/download_failures.py`
- Modify: `backend/app/queue/sra_handlers.py:291-321`
- Test: `backend/tests/queue/test_sra_download.py` (verify unchanged behavior)

A pure move so both handlers use one classifier. No behavior change.

- [ ] **Step 1: Create the shared module**

Create `backend/app/queue/download_failures.py`, moving the body of `sra_handlers._download_failure` verbatim and generalizing its wording:

```python
"""Sorting a failed download into "retry" and "never going to work".

Shared by the SRA and assembly download handlers, whose bias is the same and
is the opposite of the pipeline handlers': a fastp failure is almost always
the input, while a download failure is almost always the network. Retryable
is therefore the default, and a genuinely permanent failure still stops after
the handler's attempt budget.
"""

from pathlib import Path

from app.errors import PermanentError, RetryableError
from app.queue.pipeline_handlers import _log_tail

# Errors that mean "ask again later" rather than "this will never work". NCBI
# is rate-limited and intermittently unavailable, and burning the attempt
# budget on a transient 503 would fail a download a retry would complete.
RETRYABLE_PATTERNS = (
    "connection",
    "timeout",
    "timed out",
    "network",
    "temporarily",
    "503",
    "502",
    "429",
    "try again",
)


def classify_failure(
    code: int, log_path: Path, accession: str, *, tool: str
) -> Exception:
    """The exception a non-zero download exit deserves."""
    tail = _log_tail(log_path)
    detail = f"{tool} exited {code} for {accession}"
    if tail:
        detail = f"{detail}: {tail}"

    lowered = tail.lower()

    # A retracted or mistyped accession will fail identically forever, so it
    # must not consume the whole attempt budget.
    if "not found" in lowered or "does not exist" in lowered or "invalid" in lowered:
        return PermanentError(detail, details={"accession": accession})

    if "disk" in lowered and ("full" in lowered or "space" in lowered):
        return PermanentError(detail, details={"accession": accession})

    if code == 137:
        return RetryableError(f"{detail} (killed, most likely out of memory)")

    if any(pattern in lowered for pattern in RETRYABLE_PATTERNS):
        return RetryableError(detail)

    return RetryableError(detail)
```

- [ ] **Step 2: Delegate from the SRA handler**

In `backend/app/queue/sra_handlers.py`, replace the `_download_failure` body (lines 291-321) with a delegation, keeping the name so its call site at line 262 is untouched:

```python
def _download_failure(code: int, log_path: Path, accession: str) -> Exception:
    """Classify a non-zero exit from the SRA toolkit.

    Kept as a named wrapper so the call site reads the same; the logic is
    shared with the assembly handler in `download_failures`.
    """
    return download_failures.classify_failure(
        code, log_path, accession, tool="fasterq-dump"
    )
```

Add the import and delete the now-unused `_RETRYABLE_PATTERNS` constant (lines 41-51):

```python
from app.queue import download_failures
```

- [ ] **Step 3: Run the existing tests to verify nothing changed**

```bash
docker compose exec api python -m pytest tests/queue/test_sra_download.py -v
```

Expected: all pass. This is a refactor — any failure means the move changed behavior.

- [ ] **Step 4: Commit**

```bash
git add backend/app/queue/download_failures.py backend/app/queue/sra_handlers.py
git commit -m "refactor: share the download failure classifier"
```

---

## Task 8: The assembly download handler

**Files:**
- Create: `backend/app/queue/assembly_handlers.py`
- Modify: `backend/app/queue/handlers.py`
- Test: `backend/tests/queue/test_assembly_download.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_assembly_download.py`:

```python
"""Assembly download: component labeling and the disk pre-flight.

The pure decisions, without the network or a worker. The zip layout below is
verbatim from `datasets download genome accession GCF_000002445.2 --include
genome,gff3,protein,cds` (v18.30.1).

The test that matters most is that `cds_from_genomic.fna` is labeled CDS and
not genome. Both files are `.fna` in the same directory, and getting it wrong
puts a CDS file in the aligner's reference picker, where selecting it produces
silently wrong alignments rather than an error.
"""

import json
from pathlib import Path

import pytest

from app.errors import PermanentError
from app.queue import assembly_handlers

CATALOG = {
    "apiVersion": "V2",
    "assemblies": [
        {"files": [{"filePath": "assembly_data_report.jsonl",
                    "fileType": "DATA_REPORT",
                    "uncompressedLengthBytes": "3725"}]},
        {"accession": "GCF_000002445.2",
         "files": [
             {"filePath": "GCF_000002445.2/cds_from_genomic.fna",
              "fileType": "CDS_NUCLEOTIDE_FASTA",
              "uncompressedLengthBytes": "15188456"},
             {"filePath": "GCF_000002445.2/GCF_000002445.2_ASM244v1_genomic.fna",
              "fileType": "GENOMIC_NUCLEOTIDE_FASTA",
              "uncompressedLengthBytes": "26402511"},
             {"filePath": "GCF_000002445.2/genomic.gff",
              "fileType": "GFF3",
              "uncompressedLengthBytes": "12545174"},
             {"filePath": "GCF_000002445.2/protein.faa",
              "fileType": "PROTEIN_FASTA",
              "uncompressedLengthBytes": "5173742"},
         ]},
    ],
}


@pytest.fixture
def extracted(tmp_path: Path) -> Path:
    """A work dir shaped like a real extracted package."""
    data = tmp_path / "ncbi_dataset" / "data"
    acc = data / "GCF_000002445.2"
    acc.mkdir(parents=True)
    (data / "dataset_catalog.json").write_text(json.dumps(CATALOG))
    (data / "assembly_data_report.jsonl").write_text("{}\n")
    (acc / "cds_from_genomic.fna").write_text(">cds\nACGT\n")
    (acc / "GCF_000002445.2_ASM244v1_genomic.fna").write_text(">chr1\nACGT\n")
    (acc / "genomic.gff").write_text("##gff-version 3\n")
    (acc / "protein.faa").write_text(">prot\nMKV\n")
    return tmp_path


class TestLabelFromCatalog:
    def test_cds_is_not_labeled_as_the_genome(self, extracted: Path):
        """THE regression test. Both are .fna in one directory; labeling by
        extension or by matching *_genomic.fna first roles the CDS file as a
        reference genome."""
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        by_name = {s["name"]: s for s in staged}
        assert by_name["cds_from_genomic.fna"]["component"] == "cds"
        assert by_name["cds_from_genomic.fna"]["role"] == "transcript"

    def test_the_genome_fasta_is_the_reference(self, extracted: Path):
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        genome = next(
            s for s in staged if s["name"] == "GCF_000002445.2_ASM244v1_genomic.fna"
        )
        assert genome["component"] == "genome"
        assert genome["role"] == "reference"

    def test_every_component_is_labeled(self, extracted: Path):
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        assert {s["component"] for s in staged} == {"genome", "gff3", "protein", "cds"}

    def test_the_data_report_is_not_staged(self, extracted: Path):
        """assembly_data_report.jsonl is metadata about the package, not a
        file the user asked for. Ingesting it would put a stray .jsonl in the
        project."""
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        assert "assembly_data_report.jsonl" not in {s["name"] for s in staged}

    def test_paths_are_absolute(self, extracted: Path):
        """The applier consumes these from a different process; a relative
        path would resolve against the wrong cwd."""
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        assert all(Path(s["path"]).is_absolute() for s in staged)


class TestLabelWithoutCatalog:
    def test_falls_back_to_filenames(self, extracted: Path):
        """A catalog that NCBI stops shipping must not lose the download."""
        (extracted / "ncbi_dataset" / "data" / "dataset_catalog.json").unlink()
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        assert {s["component"] for s in staged} == {"genome", "gff3", "protein", "cds"}

    def test_the_fallback_also_gets_cds_right(self, extracted: Path):
        """The filename fallback is where the .fna collision actually bites:
        `cds_from_genomic.fna` must be matched before `*_genomic.fna`."""
        (extracted / "ncbi_dataset" / "data" / "dataset_catalog.json").unlink()
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        by_name = {s["name"]: s["component"] for s in staged}
        assert by_name["cds_from_genomic.fna"] == "cds"
        assert by_name["GCF_000002445.2_ASM244v1_genomic.fna"] == "genome"


class TestDiskPreflight:
    def test_a_download_that_cannot_fit_is_refused_up_front(self, tmp_path: Path):
        """Discovering the disk is full after an hour of transfer is too late:
        the space is already spent and the partial output has to be reaped."""
        with pytest.raises(PermanentError, match="disk space"):
            assembly_handlers._check_disk_space(
                tmp_path, 10**15, "GCF_000002445.2"
            )

    def test_no_estimate_means_no_refusal(self, tmp_path: Path):
        """A missing figure is not evidence of a problem, and refusing on it
        would block downloads NCBI has no size for."""
        assembly_handlers._check_disk_space(tmp_path, None, "GCF_000002445.2")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/queue/test_assembly_download.py -v
```

Expected: FAIL — `ModuleNotFoundError: app.queue.assembly_handlers`.

- [ ] **Step 3: Write the handler**

Create `backend/app/queue/assembly_handlers.py`:

```python
"""Downloading a published assembly from NCBI.

Sibling to `sra_handlers` rather than a branch inside it, for the reason that
module gives for its own existence: the operational shape differs. One
accession here yields one job producing up to four files with no QC chained,
where a run yields FASTQ pairs that always chain QC. What they share --
shelling out, log capture, failure classification -- is factored into
`run_subprocess` and `download_failures`.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import json
import shutil
import zipfile
from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.metadata import assembly_components
from app.models import IoClass, JobClass, JobResources
from app.pipelines import tools
from app.queue import download_failures
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# The zip holds already-compressed text and is extracted beside itself, so the
# peak requirement is roughly download + extraction. Deliberately not SRA's
# 4.0, which exists to guess at a compressed archive expanding into plain
# FASTQ; here the post-extraction size is known exactly from the catalog, but
# only after the download, so this stands in for it beforehand.
EXTRACTION_FACTOR = 2.5

# What the CLI names the package. Fixed rather than derived from the accession
# so the extraction step has one path to look for.
PACKAGE_NAME = "package.zip"


@handler(
    "download_assembly",
    mode=HandlerMode.SUBPROCESS,
    # USER_INTERACTIVE for the same reason as the SRA download: someone
    # clicked and is watching for the file, and the work waits on NCBI rather
    # than competing with alignments for CPU.
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
    # Matches download_sra_run: a failed download is usually the network, and
    # the third attempt genuinely succeeds often enough to be worth it.
    max_attempts=3,
)
def download_assembly(ctx: JobContext) -> dict:
    """Fetch one assembly's components. The ingest happens in the applier.

    Synchronous: SUBPROCESS runs this off the event loop, so the body must not
    await and cannot touch the database. It stages files under tmp/ and
    returns a description for `_apply_assembly_download` to persist.

    Idempotent by construction -- each attempt gets a fresh scratch directory,
    so a retry after a partial transfer starts clean rather than extracting a
    truncated zip.
    """
    datasets = tools.require(tools.datasets())

    accession = (ctx.payload.get("accession") or "").strip().upper()
    if not accession:
        raise PermanentError("download_assembly requires an 'accession'")

    project_id = ctx.payload.get("project_id")
    if not project_id:
        raise PermanentError("download_assembly requires a 'project_id'")

    components = ctx.payload.get("components") or ["genome"]
    include = assembly_components.include_argument(components)

    work = _prepare_workdir(ctx, kind="assembly_download")

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Checked before the transfer: discovering the disk is full once the files
    # exist is too late, and the partial output has to be reaped anyway.
    ctx.progress(phase="preview", pct=0.0, message=f"checking {accession}")
    _check_disk_space(work, ctx.payload.get("bytes_estimate"), accession)

    ctx.check_cancel()

    # A large assembly is a long transfer with no output for minutes at a
    # time, which would otherwise let the lease expire and the reaper
    # double-run the job.
    ctx.extend_lease(3600)

    ctx.progress(phase="downloading", pct=0.05, message=f"downloading {accession}")
    package = work / PACKAGE_NAME
    _download(ctx, datasets.path, accession, include, package, log_path)

    ctx.check_cancel()

    ctx.progress(phase="verifying", pct=0.7, message="verifying checksums")
    _verify(package, accession)

    ctx.progress(phase="extracting", pct=0.8, message="extracting")
    _extract(package, work, accession)

    staged = _label_components(work, accession)
    if not staged:
        # A zero exit whose package held nothing we recognize. Better caught
        # here than as an ingest of nothing several steps later.
        raise RetryableError(
            f"{accession} downloaded but contained no recognizable components"
        )

    # The zip is large and already extracted; keeping it would double the
    # footprint until the scratch reaper runs.
    package.unlink(missing_ok=True)

    ctx.progress(phase="done", pct=1.0, message=f"downloaded {accession}")
    log.info(
        "assembly_download_finished",
        job_id=ctx.job_id,
        accession=accession,
        components=[s["component"] for s in staged],
    )

    return {
        "accession": accession,
        "staged": staged,
        "metadata": ctx.payload.get("metadata") or {},
        "facts": ctx.payload.get("facts") or {},
        "project_id": project_id,
        "job_id": ctx.job_id,
        "staging_dir": str(work),
    }


def _check_disk_space(work: Path, estimate: int | None, accession: str) -> None:
    """Refuse a download that cannot fit before spending an hour on it.

    Silent when no estimate was supplied: a missing figure is not evidence of
    a problem, and refusing on it would block downloads NCBI has no size for.
    """
    if not estimate:
        return

    free = shutil.disk_usage(work).free
    needed = estimate * EXTRACTION_FACTOR

    if needed > free * 0.9:
        raise PermanentError(
            f"Not enough disk space for {accession}: needs roughly "
            f"{needed / 1e9:.1f} GB (package {estimate / 1e9:.1f} GB plus "
            f"extraction), only {free / 1e9:.1f} GB free.",
            details={
                "accession": accession,
                "needed_bytes": int(needed),
                "free_bytes": free,
            },
        )


def _download(
    ctx: JobContext,
    datasets_path: str,
    accession: str,
    include: str,
    package: Path,
    log_path: Path,
) -> None:
    """Fetch the package zip.

    No progress parsing: `--no-progressbar` suppresses the CLI's own bar, and
    its ANSI cursor-up output is not worth reconstructing a percentage from.
    Phases are reported instead, which is honest about a job that is mostly
    one opaque transfer.
    """
    cmd = [
        datasets_path,
        "download",
        "genome",
        "accession",
        accession,
        "--include",
        include,
        # Mandatory: without it the CLI emits an ANSI progress bar that floods
        # the log and makes the tail useless for diagnosing a failure.
        "--no-progressbar",
        "--filename",
        str(package),
    ]

    log.info(
        "assembly_download_started",
        job_id=ctx.job_id,
        accession=accession,
        include=include,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise download_failures.classify_failure(
            code, log_path, accession, tool="datasets"
        )
    if not package.exists():
        raise RetryableError(
            f"datasets exited 0 but wrote no package for {accession}"
        )


def _verify(package: Path, accession: str) -> None:
    """Check the package against its own md5 manifest.

    Cheap, and worth it: a truncated transfer that exits 0 is otherwise
    indistinguishable from a good one until an aligner fails on a malformed
    FASTA hours later. A missing manifest is not fatal -- it is a bonus NCBI
    provides, not a guarantee.
    """
    import hashlib

    try:
        with zipfile.ZipFile(package) as zf:
            names = set(zf.namelist())
            if "md5sum.txt" not in names:
                log.info("assembly_no_manifest", accession=accession)
                return
            manifest = zf.read("md5sum.txt").decode("utf-8", "replace")
            for line in manifest.splitlines():
                parts = line.split()
                if len(parts) != 2:
                    continue
                expected, rel = parts
                member = f"ncbi_dataset/data/{rel}" if rel not in names else rel
                if member not in names:
                    continue
                digest = hashlib.md5(zf.read(member)).hexdigest()
                if digest != expected:
                    raise RetryableError(
                        f"Checksum mismatch for {rel} in {accession}: the "
                        "download was corrupted or truncated."
                    )
    except zipfile.BadZipFile as e:
        raise RetryableError(
            f"{accession} downloaded a corrupt zip: {e}"
        ) from e


def _extract(package: Path, work: Path, accession: str) -> None:
    """Unpack the zip beside itself.

    Members are checked for path traversal before extraction: the archive is
    from NCBI and trustworthy, but a zip that writes outside its target
    directory is the kind of thing that must fail loudly rather than
    silently overwrite something on the host.
    """
    try:
        with zipfile.ZipFile(package) as zf:
            for member in zf.namelist():
                target = (work / member).resolve()
                if not str(target).startswith(str(work.resolve())):
                    raise PermanentError(
                        f"{accession}'s package contains an unsafe path: {member}"
                    )
            zf.extractall(work)
    except zipfile.BadZipFile as e:
        raise RetryableError(f"{accession} downloaded a corrupt zip: {e}") from e


def _label_components(work: Path, accession: str) -> list[dict]:
    """Which file is which component, and what role each becomes.

    Reads `dataset_catalog.json`'s explicit `fileType` rather than matching
    filenames, because the genome FASTA and the CDS FASTA are *both* `.fna` in
    the same directory:

        GCF_000002445.2_ASM244v1_genomic.fna   <- genome
        cds_from_genomic.fna                   <- CDS

    Labeling those by extension roles a CDS file as a reference genome, which
    puts it in the aligner's reference picker where selecting it produces
    silently wrong alignments rather than an error. The filename fallback
    below exists for a catalog NCBI stops shipping, and matches
    `cds_from_genomic` *first* for exactly this reason.
    """
    data_dir = work / "ncbi_dataset" / "data"
    if not data_dir.is_dir():
        log.warning("assembly_no_data_dir", accession=accession)
        return []

    by_type = {spec.file_type: spec for spec in assembly_components.COMPONENTS.values()}

    staged: list[dict] = []
    catalog = data_dir / "dataset_catalog.json"
    if catalog.is_file():
        try:
            payload = json.loads(catalog.read_text())
            for group in payload.get("assemblies") or []:
                for entry in group.get("files") or []:
                    spec = by_type.get(entry.get("fileType"))
                    if spec is None:
                        # DATA_REPORT and anything new: metadata about the
                        # package, not a file the user asked for.
                        continue
                    path = data_dir / entry.get("filePath", "")
                    if not path.is_file():
                        continue
                    staged.append(_entry(path, spec))
        except (ValueError, OSError, TypeError) as e:
            log.warning("assembly_catalog_unreadable", accession=accession, error=str(e))
            staged = []

    if staged:
        return staged

    return _label_by_filename(data_dir, accession)


def _label_by_filename(data_dir: Path, accession: str) -> list[dict]:
    """The fallback when the catalog is missing or unreadable.

    Order is load-bearing: `cds_from_genomic.fna` also ends with
    `_genomic.fna`, so the CDS test must come first or a CDS file is labeled
    as the genome.
    """
    staged: list[dict] = []
    for path in sorted(data_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name == "cds_from_genomic.fna":
            key = "cds"
        elif name == "protein.faa":
            key = "protein"
        elif name.endswith(".gff") or name.endswith(".gff3"):
            key = "gff3"
        elif name.endswith("_genomic.fna"):
            key = "genome"
        else:
            continue
        staged.append(_entry(path, assembly_components.COMPONENTS[key]))

    if not staged:
        log.warning("assembly_nothing_labeled", accession=accession)
    return staged


def _entry(path: Path, spec: assembly_components.ComponentSpec) -> dict:
    return {
        "path": str(path.resolve()),
        "name": path.name,
        "component": spec.key,
        "role": spec.role,
    }
```

- [ ] **Step 4: Register the handler**

In `backend/app/queue/handlers.py`, add the import alongside `sra_handlers`:

```python
from app.queue import assembly_handlers  # noqa: F401 - registration side effects
```

Match the existing import's style and `noqa` comment exactly.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/queue/test_assembly_download.py -v
```

Expected: 9 passed.

- [ ] **Step 6: Confirm the handler registers**

```bash
docker compose restart worker && sleep 5 && docker compose logs worker --tail 30 | grep handlers_loaded
```

Expected: the list includes `download_assembly`. If it does not, the import in Step 4 did not take effect.

- [ ] **Step 7: Commit**

```bash
git add backend/app/queue/assembly_handlers.py backend/app/queue/handlers.py backend/tests/queue/test_assembly_download.py
git commit -m "feat: download_assembly job handler"
```

---

## Task 9: Ingest downloaded components

**Files:**
- Modify: `backend/app/queue/results.py` (add near `_apply_sra_download:367`; registry at `:933`)
- Test: `backend/tests/queue/test_assembly_apply.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_assembly_apply.py`:

```python
"""What the assembly applier decides before touching the database.

The applier itself needs Mongo, so what is tested here is the pure mapping:
which role each staged component becomes, and what metadata ties the four
files to one assembly. That mapping is where a mistake is invisible until an
aligner offers a protein FASTA as a reference.
"""

from app.queue import results


class TestComponentRoleMapping:
    def test_each_component_becomes_its_role(self):
        staged = [
            {"name": "g.fna", "component": "genome", "role": "reference"},
            {"name": "genomic.gff", "component": "gff3", "role": "annotation"},
            {"name": "protein.faa", "component": "protein", "role": "protein"},
            {"name": "cds_from_genomic.fna", "component": "cds", "role": "transcript"},
        ]
        roles = {s["name"]: results._role_for_component(s) for s in staged}
        assert roles["g.fna"] == "reference"
        assert roles["genomic.gff"] == "annotation"
        assert roles["protein.faa"] == "protein"
        assert roles["cds_from_genomic.fna"] == "transcript"

    def test_an_unknown_component_gets_no_role(self):
        """None rather than a guess: an unroled file is merely uncategorized,
        while a wrongly-roled one is actively misleading."""
        assert results._role_for_component({"component": "mystery"}) is None


class TestComponentMetadata:
    def test_every_component_carries_the_assembly_accession(self):
        """The key that makes the four files find each other -- and what
        `already_downloaded` matches on."""
        meta = results._component_metadata(
            {"organism": "Trypanosoma brucei"}, "GCF_000002445.2", "protein"
        )
        assert meta["assembly_accession"] == "GCF_000002445.2"
        assert meta["organism"] == "Trypanosoma brucei"

    def test_reference_build_is_not_claimed_for_a_protein_set(self):
        """reference_build describes a genome. On a protein FASTA it would
        assert the file is an assembly, which is what the roles exist to deny."""
        meta = results._component_metadata(
            {"reference_build": "ASM244v1"}, "GCF_000002445.2", "protein"
        )
        assert "reference_build" not in meta

    def test_the_genome_keeps_its_build(self):
        meta = results._component_metadata(
            {"reference_build": "ASM244v1"}, "GCF_000002445.2", "genome"
        )
        assert meta["reference_build"] == "ASM244v1"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/queue/test_assembly_apply.py -v
```

Expected: FAIL — `AttributeError: _role_for_component`.

- [ ] **Step 3: Write the helpers and the applier**

In `backend/app/queue/results.py`, after `_apply_sra_download` ends (line ~470):

```python
def _role_for_component(entry: dict) -> str | None:
    """The ObjectRole value a staged component becomes.

    Taken from the handler's own labeling, which read the package catalog.
    Returns None for anything unrecognized: an unroled file is merely
    uncategorized in the explorer, while a wrongly-roled one is actively
    misleading -- a CDS FASTA offered as a reference genome.
    """
    from app.metadata import assembly_components

    spec = assembly_components.COMPONENTS.get(entry.get("component") or "")
    return spec.role if spec else None


def _component_metadata(base: dict, accession: str, component: str) -> dict:
    """Metadata for one component, from the assembly's shared record.

    `assembly_accession` goes on every component: it is what makes the four
    files recognize each other in search and in the explorer, and what
    "do I already have this genome?" matches on.

    Genome-specific keys are withheld from the others. `reference_build` on a
    protein FASTA would assert that the file is an assembly, which is exactly
    the confusion the PROTEIN role exists to prevent.
    """
    genome_only = {"reference_build", "assembly_level", "is_primary_assembly"}

    out = {
        k: v
        for k, v in (base or {}).items()
        if component == "genome" or k not in genome_only
    }
    out["assembly_accession"] = accession
    return out


async def _apply_assembly_download(result: dict) -> None:
    """Take a finished assembly download into the project.

    Mirrors `_apply_sra_download`: the handler ran in a worker thread and
    could not touch the database, so the ingest happens here. One failed
    component does not lose the rest -- the transfer is the expensive part and
    it already succeeded.

    No QC and no mate linking: a reference genome has no reads to QC and no
    pair.
    """
    from app.services import object_service, run_service

    staged = result.get("staged") or []
    project_id = result.get("project_id")
    if not staged or not project_id:
        return

    project_id = PydanticObjectId(project_id)
    accession = result.get("accession") or ""
    job_id = result.get("job_id")
    base_metadata = dict(result.get("metadata") or {})
    base_facts = dict(result.get("facts") or {})

    created = []
    for entry in staged:
        component = entry.get("component") or ""
        # Provenance, distinct from the biology: where these bytes came from,
        # which is not a searchable property of the organism.
        facts = dict(base_facts)
        facts.update(
            {
                "assembly_downloaded_from": accession,
                "assembly_download_source": "ncbi_datasets",
                "assembly_component": component,
            }
        )
        try:
            obj = await object_service.ingest_local_file(
                project_id=project_id,
                path=Path(entry["path"]),
                name=entry["name"],
                role=_role_for_component(entry),
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                facts=facts,
                metadata=_component_metadata(base_metadata, accession, component),
            )
        except Exception as e:  # noqa: BLE001 - one bad file must not lose the rest
            log.error(
                "assembly_ingest_failed",
                accession=accession,
                name=entry.get("name"),
                error=str(e),
            )
            continue
        created.append(obj)

    if not created:
        log.error("assembly_download_ingested_nothing", accession=accession)
        return

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, [o.id for o in created])

    log.info(
        "assembly_download_applied",
        accession=accession,
        objects=[str(o.id) for o in created],
        components=[s.get("component") for s in staged],
    )
```

Register it in the applier map (line ~933), beside `"download_sra_run"`:

```python
    "download_assembly": _apply_assembly_download,
```

- [ ] **Step 4: Confirm `ingest_local_file` accepts a `role`**

```bash
grep -n "def ingest_local_file" -A22 backend/app/services/object_service.py
```

If there is no `role` parameter, set it after ingest instead — replacing the `role=` argument with:

```python
        if role := _role_for_component(entry):
            await obj.set({DataObject.role: role})
```

Use whichever the existing signature supports; do not add a parameter to `ingest_local_file` in this task.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/queue/test_assembly_apply.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/results.py backend/tests/queue/test_assembly_apply.py
git commit -m "feat: ingest downloaded assembly components with their roles"
```

---

## Task 10: The assembly launch service

**Files:**
- Create: `backend/app/services/assembly_service.py`
- Modify: `backend/app/models/run.py:24-28`
- Test: `backend/tests/services/test_assembly_service.py`

- [ ] **Step 1: Add the run kind**

In `backend/app/models/run.py`, inside `RunKind` after `SRA_DOWNLOAD` (line 27):

```python
    # Separate from SRA_DOWNLOAD because RunKind is a display and grouping
    # vocabulary, and "downloaded a genome" reads differently from "downloaded
    # sequencing runs" in the activity view.
    ASSEMBLY_DOWNLOAD = "assembly_download"
```

- [ ] **Step 2: Write the failing test**

Create `backend/tests/services/test_assembly_service.py`:

```python
"""Launch validation for an assembly download.

The rules that must hold before any job is queued, tested without HTTP.
"""

import pytest

from app.errors import ValidationError
from app.services import assembly_service


class TestSelectionValidation:
    def test_a_non_assembly_accession_is_rejected(self):
        """An SRR here means the caller routed the wrong way; queueing a
        genome download for it would fail confusingly an hour later."""
        with pytest.raises(ValidationError, match="assembly accession"):
            assembly_service.validate_selection("SRR11768093", ["genome"])

    def test_an_unversioned_accession_is_rejected(self):
        with pytest.raises(ValidationError, match="assembly accession"):
            assembly_service.validate_selection("GCF_000002445", ["genome"])

    def test_genome_is_forced_in(self):
        """Every other component describes coordinates or products of the
        genome sequence. A request without it is a frontend bug, not an
        intent to honor."""
        assert assembly_service.validate_selection(
            "GCF_000002445.2", ["gff3"]
        ) == ["genome", "gff3"]

    def test_unknown_components_are_dropped(self):
        """Silently, because the alternative is failing a download over a
        component name the frontend sent by mistake."""
        assert assembly_service.validate_selection(
            "GCF_000002445.2", ["genome", "nonsense"]
        ) == ["genome"]

    def test_components_come_back_in_display_order(self):
        """So the label and the log read consistently regardless of the order
        checkboxes were clicked."""
        assert assembly_service.validate_selection(
            "GCF_000002445.2", ["cds", "genome", "gff3"]
        ) == ["genome", "gff3", "cds"]


class TestLabel:
    def test_a_genome_only_download_says_so(self):
        label = assembly_service.download_label("GCF_000002445.2", ["genome"])
        assert "GCF_000002445.2" in label

    def test_a_multi_component_download_counts_them(self):
        label = assembly_service.download_label(
            "GCF_000002445.2", ["genome", "gff3", "protein"]
        )
        assert "GCF_000002445.2" in label
        assert "3" in label
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/services/test_assembly_service.py -v
```

Expected: FAIL — `ModuleNotFoundError`. Create `backend/tests/services/__init__.py` if the directory does not exist.

- [ ] **Step 4: Write the service**

Create `backend/app/services/assembly_service.py`:

```python
"""Launching an assembly download.

The same shape as `sra_service`: validate the request, build the payload, and
create the run that groups the resulting job. Kept out of the router so the
launch rules are testable without HTTP.

One job rather than one per component: the CLI fetches them in a single
package, so splitting would mean four downloads of overlapping data.
"""

from beanie import PydanticObjectId

from app.errors import ConflictError, NotFoundError, ValidationError
from app.logging import get_logger
from app.metadata import assembly, assembly_components
from app.models import (
    DataObject,
    IoClass,
    JobClass,
    JobResources,
    ObjectRole,
    Project,
    RunJobRole,
    RunKind,
)
from app.pipelines import tools
from app.services import run_service

log = get_logger(__name__)


def validate_selection(accession: str, components: list[str]) -> list[str]:
    """The components to fetch, normalized and ordered.

    Genome is forced in and unknown names are dropped: both are frontend bugs
    rather than intents, and failing a download over either would be a worse
    answer than quietly doing the sensible thing.
    """
    if not assembly.is_valid_accession(accession or ""):
        raise ValidationError(
            f"{accession!r} is not an assembly accession. Expected a GenBank "
            "(GCA_000000000.0) or RefSeq (GCF_000000000.0) accession, "
            "including the version suffix.",
            details={"accession": accession},
        )

    requested = {c.strip().lower() for c in components or []}
    selected = [k for k in assembly_components.COMPONENT_ORDER if k in requested]
    if "genome" not in selected:
        selected.insert(0, "genome")
    return selected


def download_label(accession: str, components: list[str]) -> str:
    """A one-line description, built at launch.

    Stored rather than derived so the run stays describable after its jobs are
    TTL-pruned -- the same reason `PipelineRun.params` is denormalized.
    """
    if len(components) == 1:
        return f"Download {accession} from NCBI"
    return f"Download {accession} from NCBI ({len(components)} components)"


async def already_downloaded(
    project_id: PydanticObjectId, accession: str
) -> bool:
    """Whether this project already holds this assembly's genome.

    Narrowed to the reference role on purpose: a project holding only the
    protein FASTA from this assembly does not have the genome, and answering
    yes would hide the download the user actually wants.

    Matched on `assembly_accession`, which ingest enrichment also writes, so a
    hand-uploaded reference counts too.
    """
    existing = await DataObject.find_one(
        DataObject.project_id == project_id,
        DataObject.role == ObjectRole.REFERENCE,
        {"metadata.assembly_accession": accession},
    )
    return existing is not None


async def launch_download(
    *,
    project_id: PydanticObjectId,
    accession: str,
    components: list[str],
):
    """Queue the download and the run that groups it."""
    from app.queue import queue

    tools.require(tools.datasets())

    accession = (accession or "").strip().upper()
    selected = validate_selection(accession, components)

    project = await Project.get(project_id)
    if project is None:
        raise NotFoundError(f"Project not found: {project_id}")

    # Fetched once here so the handler's disk pre-flight and the ingest
    # metadata are both available: the handler runs in a worker thread and can
    # reach neither the database nor an await.
    meta = assembly.lookup(accession)
    availability = assembly.component_availability(accession) or []
    estimate = sum(
        c.size_bytes or 0
        for c in availability
        if c.key in set(selected) and c.size_bytes
    )

    run = await run_service.create_run(
        kind=RunKind.ASSEMBLY_DOWNLOAD,
        project_id=project_id,
        label=download_label(accession, selected),
        inputs=[],  # Nothing in the project is an input; the source is NCBI.
        params={
            "accession": accession,
            "components": selected,
            "source": "ncbi_datasets",
        },
    )

    payload = {
        "accession": accession,
        "project_id": str(project_id),
        "components": selected,
        "metadata": meta.to_metadata() if meta else {},
        "facts": meta.to_facts() if meta else {},
    }
    if estimate:
        payload["bytes_estimate"] = estimate

    job = await queue.enqueue(
        "download_assembly",
        payload=payload,
        job_class=JobClass.USER_INTERACTIVE,
        resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
        max_attempts=3,
        # Keyed on (accession, project) so a double-click collapses, while the
        # same assembly stays downloadable into a second project.
        dedup_key=f"assembly_download:{accession}:{project_id}",
        project_id=project_id,
    )

    if job is None:
        # Already queued or running from an earlier click, so this run
        # describes no work and must not linger in the activity view.
        await run_service.discard_run(run.id)
        raise ConflictError(
            f"{accession} is already downloading",
            details={"accession": accession},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.DOWNLOAD)

    log.info(
        "assembly_download_launched",
        run_id=str(run.id),
        project_id=str(project_id),
        accession=accession,
        components=selected,
    )
    return run, [str(job.id)]
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/services/test_assembly_service.py -v
```

Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/assembly_service.py backend/app/models/run.py backend/tests/services/test_assembly_service.py
git commit -m "feat: assembly download launch service"
```

---

## Task 11: The unified NCBI router

**Files:**
- Create: `backend/app/api/v1/ncbi.py`
- Modify: `backend/app/api/v1/__init__.py`
- Test: `backend/tests/api/test_ncbi_resolve.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_ncbi_resolve.py`:

```python
"""The unified resolve endpoint's dispatch.

What matters is that one accession box routes to the right resolver and says
which branch it took, so the dialog can render a run table or an assembly card
without guessing from the shape of the response.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.metadata import assembly, assembly_components, sra_resolver


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


class TestResolveDispatch:
    async def test_a_run_accession_returns_the_sra_branch(self, client, monkeypatch):
        monkeypatch.setattr(
            sra_resolver,
            "resolve_cached",
            _async(sra_resolver.SraResolution(accession="SRR1", kind="run")),
        )
        r = await client.post("/api/v1/ncbi/resolve", json={"accession": "SRR1"})
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "run"
        assert body["sra"] is not None
        assert body["assembly"] is None

    async def test_an_assembly_accession_returns_the_assembly_branch(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            assembly,
            "lookup",
            lambda a: assembly.AssemblyMetadata(
                accession="GCF_000002445.2", organism="Trypanosoma brucei"
            ),
        )
        monkeypatch.setattr(
            assembly,
            "component_availability",
            lambda a: list(assembly_components.from_report(
                {"annotation_info": {"name": "x"}}
            ).values()),
        )
        r = await client.post(
            "/api/v1/ncbi/resolve", json={"accession": "GCF_000002445.2"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "assembly"
        assert body["assembly"] is not None
        assert body["sra"] is None
        assert body["assembly"]["organism"] == "Trypanosoma brucei"
        assert len(body["assembly"]["components"]) == 4

    async def test_an_unknown_assembly_is_a_200_with_an_error(
        self, client, monkeypatch
    ):
        """A resolution that finds nothing is a result the dialog renders, not
        a failed request -- the same rule the SRA endpoint follows."""
        monkeypatch.setattr(assembly, "lookup", lambda a: None)
        monkeypatch.setattr(assembly, "component_availability", lambda a: None)
        r = await client.post(
            "/api/v1/ncbi/resolve", json={"accession": "GCF_999999999.1"}
        )
        assert r.status_code == 200
        assert r.json()["assembly"]["error"]

    async def test_gibberish_is_a_200_with_an_error(self, client):
        r = await client.post("/api/v1/ncbi/resolve", json={"accession": "hello"})
        assert r.status_code == 200
        body = r.json()
        assert body["sra"] is not None
        assert body["sra"]["error"]


def _async(value):
    async def fake(*args, **kwargs):
        return value
    return fake
```

Check `backend/tests/api/` for the existing async-client fixture convention and reuse it rather than redefining `client` if one exists in a conftest.

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose exec api python -m pytest tests/api/test_ncbi_resolve.py -v
```

Expected: FAIL — 404, the route does not exist.

- [ ] **Step 3: Write the router**

Create `backend/app/api/v1/ncbi.py`. Move the entire contents of `backend/app/api/v1/sra.py` into it unchanged (all the request/response models and both endpoints), change the router prefix, then add the new pieces:

```python
router = APIRouter(prefix="/ncbi", tags=["ncbi"])
```

Add after the existing SRA models:

```python
class ComponentOut(BaseModel):
    key: str
    label: str
    role: str
    available: bool
    size_bytes: int | None = None
    reason: str | None = None


class AssemblyResolveResponse(BaseModel):
    accession: str
    organism: str | None = None
    tax_id: int | None = None
    strain: str | None = None
    assembly_name: str | None = None
    assembly_level: str | None = None
    submitter: str | None = None
    release_date: str | None = None
    bioproject: str | None = None
    paired_accession: str | None = None
    total_length: int | None = None
    scaffold_count: int | None = None
    contig_count: int | None = None
    gc_percent: float | None = None
    scaffold_n50: int | None = None
    components: list[ComponentOut] = Field(default_factory=list)
    # Whether this project already holds this assembly's genome.
    already_downloaded: bool = False
    error: str | None = None


class NcbiResolveResponse(BaseModel):
    """One accession, two possible answers.

    Two nullable branches with an explicit `kind` rather than one merged
    model: merging would make most fields nullable and the frontend would
    branch on the shape anyway. This way the branch is named.
    """

    kind: str
    sra: SraResolveResponse | None = None
    assembly: AssemblyResolveResponse | None = None


class AssemblyDownloadRequest(BaseModel):
    project_id: PydanticObjectId
    accession: str
    components: list[str] = Field(default_factory=lambda: ["genome"])


class AssemblyAccepted(BaseModel):
    run_id: str
    download_job_ids: list[str]
```

Add the two endpoints:

```python
@router.post("/resolve", response_model=NcbiResolveResponse)
async def ncbi_resolve(body: SraResolveRequest) -> NcbiResolveResponse:
    """Resolve any NCBI accession -- sequencing data or a published assembly.

    Read-only and starts nothing. A resolution that finds nothing is a 200
    with `error` set rather than a 404: "nothing found for this accession" is
    a result the dialog renders, not a failed request.
    """
    kind = sra_resolver.classify(body.accession) or "unknown"

    if kind == "assembly":
        return NcbiResolveResponse(
            kind=kind,
            assembly=await _resolve_assembly(body.accession, body.project_id),
        )

    return NcbiResolveResponse(kind=kind, sra=await sra_resolve(body))


async def _resolve_assembly(
    accession: str, project_id: PydanticObjectId | None
) -> AssemblyResolveResponse:
    """The assembly branch: one record plus what it offers for download.

    Both lookups are synchronous network calls, so they run in a worker thread
    rather than blocking the event loop -- `component_availability` shells out
    to the CLI, which is the slower of the two.
    """
    import asyncio

    accession = accession.strip().upper()

    meta = await asyncio.to_thread(assembly.lookup, accession)
    if meta is None:
        return AssemblyResolveResponse(
            accession=accession,
            error=(
                f"No assembly record found for {accession} at NCBI. Check the "
                "accession, including its version suffix."
            ),
        )

    availability = await asyncio.to_thread(
        assembly.component_availability, accession
    )
    if availability is None:
        # The CLI could not answer, so fall back to what the API report says.
        # Coarser, but better than offering every component blindly.
        availability = list(
            assembly_components.from_report(
                {
                    "annotation_info": {"name": meta.assembly_name}
                    if meta.assembly_name
                    else None,
                    "paired_accession": meta.paired_accession,
                }
            ).values()
        )

    present = False
    if project_id is not None:
        present = await assembly_service.already_downloaded(project_id, accession)

    return AssemblyResolveResponse(
        accession=meta.accession or accession,
        organism=meta.organism,
        tax_id=meta.tax_id,
        strain=meta.strain,
        assembly_name=meta.assembly_name,
        assembly_level=meta.assembly_level,
        submitter=meta.submitter,
        release_date=meta.release_date,
        bioproject=meta.bioproject,
        paired_accession=meta.paired_accession,
        total_length=meta.total_length,
        scaffold_count=meta.scaffold_count,
        contig_count=meta.contig_count,
        gc_percent=meta.gc_percent,
        scaffold_n50=meta.scaffold_n50,
        components=[ComponentOut(**c.as_dict()) for c in availability],
        already_downloaded=present,
    )


@router.post(
    "/download-assembly",
    response_model=AssemblyAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def download_assembly(body: AssemblyDownloadRequest) -> AssemblyAccepted:
    """Download an assembly's selected components.

    202 rather than 201: this accepts the work and returns immediately.
    """
    run, job_ids = await assembly_service.launch_download(
        project_id=body.project_id,
        accession=body.accession,
        components=body.components,
    )
    return AssemblyAccepted(run_id=str(run.id), download_job_ids=job_ids)
```

Add the imports at the top:

```python
from app.metadata import assembly, assembly_components, sra_resolver
from app.services import assembly_service, sra_service
```

- [ ] **Step 4: Keep the old paths working and register the router**

In `backend/app/api/v1/__init__.py`, register `ncbi` and keep `sra` registered so existing `/sra/*` callers are unaffected. Reduce `sra.py` to an alias module that re-exports the router built in `ncbi.py` under the old prefix:

```python
"""Backwards-compatible `/sra/*` paths.

The implementation moved to `ncbi.py` when assemblies joined it. These paths
are kept because the frontend and any bookmarked request still use them.
"""

from fastapi import APIRouter

from app.api.v1.ncbi import sra_download, sra_resolve

router = APIRouter(prefix="/sra", tags=["sra"])
router.post("/resolve")(sra_resolve)
router.post("/download", status_code=202)(sra_download)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/api/test_ncbi_resolve.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Verify both paths respond**

```bash
docker compose exec api python -c "
from app.main import app
paths = sorted(r.path for r in app.routes if 'sra' in r.path or 'ncbi' in r.path)
print('\n'.join(paths))"
```

Expected: `/api/v1/ncbi/resolve`, `/api/v1/ncbi/download-assembly`, `/api/v1/sra/resolve`, `/api/v1/sra/download`.

- [ ] **Step 7: Run the whole backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/v1/ncbi.py backend/app/api/v1/sra.py backend/app/api/v1/__init__.py backend/tests/api/test_ncbi_resolve.py
git commit -m "feat: unified /ncbi/resolve endpoint"
```

---

## Task 12: Frontend API client and types

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add the response types**

In `frontend/src/api/types.ts`, near the existing `SraResolveResponse`:

```typescript
/** One downloadable part of an assembly. */
export interface AssemblyComponent {
  key: "genome" | "gff3" | "protein" | "cds";
  label: string;
  role: ObjectRole;
  available: boolean;
  size_bytes: number | null;
  /** Why it is unavailable. Present only when `available` is false. */
  reason: string | null;
}

export interface AssemblyResolveResponse {
  accession: string;
  organism: string | null;
  tax_id: number | null;
  strain: string | null;
  assembly_name: string | null;
  assembly_level: string | null;
  submitter: string | null;
  release_date: string | null;
  bioproject: string | null;
  paired_accession: string | null;
  total_length: number | null;
  scaffold_count: number | null;
  contig_count: number | null;
  gc_percent: number | null;
  scaffold_n50: number | null;
  components: AssemblyComponent[];
  already_downloaded: boolean;
  error: string | null;
}

/**
 * One accession, two possible answers. `kind` says which branch is populated
 * so the dialog never has to infer it from the shape.
 */
export interface NcbiResolveResponse {
  kind: string;
  sra: SraResolveResponse | null;
  assembly: AssemblyResolveResponse | null;
}

export interface AssemblyAccepted {
  run_id: string;
  download_job_ids: string[];
}
```

- [ ] **Step 2: Add the client methods**

In `frontend/src/api/client.ts`, beside `sraResolve` and `sraDownload`, matching their exact style:

```typescript
  ncbiResolve: (body: {
    accession: string;
    platform_filter?: string | null;
    project_id?: string | null;
  }) => post<NcbiResolveResponse>("/ncbi/resolve", body),

  ncbiDownloadAssembly: (body: {
    project_id: string;
    accession: string;
    components: string[];
  }) => post<AssemblyAccepted>("/ncbi/download-assembly", body),
```

Read the existing `sraResolve` first and match the helper it uses — this repo's client may not use a bare `post`.

- [ ] **Step 3: Verify it compiles**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat: frontend types and client for unified NCBI resolve"
```

---

## Task 13: The unified dialog

**Files:**
- Create: `frontend/src/components/NcbiDownloadDialog.tsx` (via `git mv`)
- Delete: `frontend/src/components/SraDownloadDialog.tsx`
- Modify: `frontend/src/components/ProjectExplorer.tsx` (import and the `sraOpen` state at line 198)
- Modify: `frontend/src/index.css`

- [ ] **Step 1: Rename the file, preserving history**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner/.claude/worktrees/worktree-todos-plan-a8dfce
git mv frontend/src/components/SraDownloadDialog.tsx frontend/src/components/NcbiDownloadDialog.tsx
```

- [ ] **Step 2: Rename the component and switch to the unified endpoint**

In `NcbiDownloadDialog.tsx`, rename the export `SraDownloadDialog` → `NcbiDownloadDialog` and update its doc comment. Replace the `resolve` mutation (lines 54-75) with one that handles both branches:

```typescript
  const [assembly, setAssembly] = useState<AssemblyResolveResponse | null>(null);
  const [components, setComponents] = useState<Set<string>>(new Set(["genome"]));

  const resolve = useMutation({
    mutationFn: () =>
      api.ncbiResolve({
        accession: accession.trim(),
        platform_filter: platform || null,
        project_id: projectId,
      }),
    onSuccess: (data) => {
      setPage(0);
      // Only one branch is ever populated, and `kind` says which. Clearing
      // the other matters: leaving a stale run table beside a new assembly
      // card would show two answers for one lookup.
      if (data.assembly) {
        setAssembly(data.assembly);
        setResolved(null);
        setSelected(new Set());
        // Genome plus everything available: the common case is "give me this
        // genome and its annotation", and unchecking is cheaper than hunting
        // for the boxes to check.
        setComponents(
          new Set(
            data.assembly.components.filter((c) => c.available).map((c) => c.key),
          ),
        );
        return;
      }
      setAssembly(null);
      setResolved(data.sra);
      setSelected(
        new Set(
          (data.sra?.runs ?? [])
            .filter((r) => !r.already_downloaded)
            .map((r) => r.accession),
        ),
      );
    },
    onError: (e: Error) => notify.error(e.message),
  });
```

- [ ] **Step 3: Add the assembly download mutation**

Beside the existing `download` mutation:

```typescript
  const downloadAssembly = useMutation({
    mutationFn: () =>
      api.ncbiDownloadAssembly({
        project_id: projectId,
        accession: assembly!.accession,
        components: [...components],
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["objects"] });
      notify.success(`Downloading ${assembly!.accession} from NCBI`);
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });
```

Check the query keys the existing `download` mutation invalidates and match them; `["objects"]` here is a guess at the explorer's key and must be verified against `ProjectExplorer.tsx`.

- [ ] **Step 4: Retitle and rehint the search form**

Replace the `<h2>` (line 161):

```tsx
        <h2>Download from NCBI</h2>
```

Replace the accession input's placeholder (line 175):

```tsx
              placeholder="SRR11768093, PRJNA1495534, GCF_000002445.2…"
```

Replace the hint (lines 200-202):

```tsx
        <small className="sra-search-hint">
          A run, experiment, sample, study, BioProject, BioSample, or a
          GenBank/RefSeq assembly (GCA/GCF).
        </small>
```

Hide the platform filter for an assembly — it has no meaning there, and a disabled control invites the question of what it would do. Wrap the platform `<label>` (lines 180-189):

```tsx
          {!assembly && (
            <label className="sra-search-platform">
              {/* ...existing contents unchanged... */}
            </label>
          )}
```

- [ ] **Step 5: Render the assembly card**

Add before the `{resolved && runs.length > 0 && (` block:

```tsx
        {assembly?.error && (
          <div className="warn-box" style={{ fontSize: 12 }}>
            {assembly.error}
          </div>
        )}

        {assembly && !assembly.error && (
          <AssemblyCard
            assembly={assembly}
            selected={components}
            onToggle={(key) =>
              setComponents((prev) => {
                const next = new Set(prev);
                // Genome is mandatory: everything else describes coordinates
                // or products of it and is close to uninterpretable alone.
                if (key === "genome") return next;
                if (next.has(key)) next.delete(key);
                else next.add(key);
                return next;
              })
            }
          />
        )}
```

Add the component at the bottom of the file:

```tsx
/** A resolved assembly: what it is, and which parts to fetch. */
function AssemblyCard({
  assembly,
  selected,
  onToggle,
}: {
  assembly: AssemblyResolveResponse;
  selected: Set<string>;
  onToggle: (key: string) => void;
}) {
  const totalBytes = assembly.components
    .filter((c) => selected.has(c.key))
    .reduce((sum, c) => sum + (c.size_bytes ?? 0), 0);

  return (
    <>
      <div className="sra-summary">
        <div>
          <strong className="mono">{assembly.accession}</strong>
          {assembly.organism && (
            <>
              {" · "}
              <span style={{ fontStyle: "italic" }}>{assembly.organism}</span>
            </>
          )}
          {assembly.strain && <> · {assembly.strain}</>}
          {assembly.already_downloaded && (
            <span className="sra-have-tag" title="Already in this project">
              have
            </span>
          )}
        </div>
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          {[
            assembly.assembly_name,
            assembly.assembly_level,
            assembly.submitter,
            assembly.release_date,
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          {[
            assembly.total_length != null &&
              `${formatBytes(assembly.total_length)} of sequence`,
            assembly.scaffold_count != null &&
              `${assembly.scaffold_count.toLocaleString()} scaffolds`,
            assembly.scaffold_n50 != null &&
              `N50 ${formatBytes(assembly.scaffold_n50)}`,
            assembly.gc_percent != null && `${assembly.gc_percent}% GC`,
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
      </div>

      <div className="assembly-components">
        {assembly.components.map((c) => (
          <label
            key={c.key}
            className={`assembly-component${c.available ? "" : " disabled"}`}
          >
            <input
              type="checkbox"
              checked={selected.has(c.key)}
              disabled={!c.available || c.key === "genome"}
              onChange={() => onToggle(c.key)}
            />
            <span>
              {c.label}
              {c.key === "genome" && (
                <small className="assembly-component-note">always included</small>
              )}
              {c.size_bytes != null && c.available && (
                <small className="assembly-component-note">
                  {formatBytes(c.size_bytes)}
                </small>
              )}
              {!c.available && c.reason && (
                <small className="assembly-component-note">{c.reason}</small>
              )}
            </span>
          </label>
        ))}
      </div>

      {totalBytes > 0 && (
        <small className="sra-search-hint">
          About {formatBytes(totalBytes)} to download.
        </small>
      )}
    </>
  );
}
```

- [ ] **Step 6: Make the footer button mode-aware**

Replace the primary action button (lines 339-348):

```tsx
          {assembly && !assembly.error ? (
            <button
              type="button"
              className="btn primary"
              disabled={downloadAssembly.isPending}
              onClick={() => downloadAssembly.mutate()}
            >
              {downloadAssembly.isPending
                ? "Queueing…"
                : `Download ${components.size} ${
                    components.size === 1 ? "file" : "files"
                  }`}
            </button>
          ) : (
            <button
              type="button"
              className="btn primary"
              disabled={selected.size === 0 || overLimit || download.isPending}
              onClick={() => download.mutate()}
            >
              {download.isPending
                ? "Queueing…"
                : `Download ${selected.size || ""}`.trim()}
            </button>
          )}
```

Also guard the QC checkbox and the selection counter so they only render in the runs branch — they are inside the `{resolved && runs.length > 0 && ...}` block already, so verify by reading rather than assuming.

- [ ] **Step 7: Update the call site**

In `ProjectExplorer.tsx`, update the import, and rename `sraOpen`/`setSraOpen` (line 198) to `ncbiOpen`/`setNcbiOpen` along with the menu label that opens it:

```bash
grep -n "sraOpen\|SraDownloadDialog\|Download from NCBI\|from SRA" frontend/src/components/ProjectExplorer.tsx
```

Update every hit. The menu item's label should read "Download from NCBI".

- [ ] **Step 8: Add the styles**

In `frontend/src/index.css`, beside the existing `.sra-*` rules:

```css
/* Assembly component checkboxes: one row each, reason text beneath. */
.assembly-components {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 10px 0;
}

.assembly-component {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
}

.assembly-component.disabled {
  color: var(--text-faint);
}

.assembly-component-note {
  display: block;
  color: var(--text-faint);
  font-size: 11px;
}
```

- [ ] **Step 9: Verify it compiles**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 10: Manual verification**

Open http://localhost:5173, pick a project, and open the download dialog:

1. `GCF_000002445.2` — assembly card, all four components available and checked, genome's box disabled, a total size shown.
2. `GCA_000001405.29` — genome only; the other three disabled, their reason naming `GCF_000001405.40`.
3. `SRR11768093` — the run table, unchanged, platform filter visible.
4. `GCF_999999999.1` — an error message, no card.
5. Download the T. brucei assembly with all four components. Watch `/activity`, then check the explorer: the genome under **References**, the GFF3 under **Annotations**, protein and CDS under **Protein & CDS**.
6. Open an aligner dialog and confirm the reference picker offers the genome but **not** the protein or CDS FASTA. This is the hazard the roles exist to close.

- [ ] **Step 11: Commit**

```bash
git add frontend/src/components/NcbiDownloadDialog.tsx frontend/src/components/ProjectExplorer.tsx frontend/src/index.css
git commit -m "feat: unified NCBI download dialog with assembly components"
```

---

## Task 14: Collapsible experiment grouping

**Files:**
- Modify: `frontend/src/components/NcbiDownloadDialog.tsx`
- Modify: `frontend/src/index.css`

- [ ] **Step 1: Add the grouping helper**

At the bottom of `NcbiDownloadDialog.tsx`:

```tsx
/** Runs grouped by the experiment they belong to. */
type RunGroup = {
  experiment: string;
  title: string | null;
  runs: SraRunInfo[];
  bytes: number;
};

/**
 * Group runs by experiment, preserving the incoming sort within each group.
 *
 * Derived here rather than from the resolver's `hierarchy`, which groups by
 * *sample*: every run already names its experiment, so this needs no extra
 * request and no cache invalidation.
 */
function groupByExperiment(runs: SraRunInfo[]): RunGroup[] {
  const groups = new Map<string, RunGroup>();
  for (const run of runs) {
    // A run with no recorded experiment still has to appear somewhere;
    // its own accession is the least surprising bucket.
    const key = run.experiment ?? run.accession;
    let group = groups.get(key);
    if (!group) {
      group = { experiment: key, title: run.title, runs: [], bytes: 0 };
      groups.set(key, group);
    }
    group.runs.push(run);
    group.bytes += run.bytes ?? 0;
  }
  return [...groups.values()].sort((a, b) =>
    a.experiment.localeCompare(b.experiment),
  );
}
```

- [ ] **Step 2: Decide when grouping applies**

After the existing `sorted` memo:

```tsx
  // Grouping earns its complexity only for a multi-experiment container. A
  // single run, or a sample with one experiment, would get a collapse control
  // around every row for no benefit.
  const groups = useMemo(() => groupByExperiment(sorted), [sorted]);
  const grouped =
    (resolved?.kind === "bioproject" || resolved?.kind === "study") &&
    groups.length > 1;

  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  // Whole experiments per page when grouped: a group split across a page
  // boundary is the confusing case, and the experiment is the unit the user
  // is now reasoning in.
  const GROUPS_PER_PAGE = 5;
  const pageCount = grouped
    ? Math.max(1, Math.ceil(groups.length / GROUPS_PER_PAGE))
    : Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const visibleGroups = grouped
    ? groups.slice(page * GROUPS_PER_PAGE, (page + 1) * GROUPS_PER_PAGE)
    : [];
  const visible = grouped
    ? []
    : sorted.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
```

Replace the existing `pageCount` and `visible` declarations (lines 125-126) with the above rather than adding a second pair.

- [ ] **Step 3: Add group-level selection**

Beside the existing `toggle` and `toggleAll`:

```tsx
  const toggleGroup = (group: RunGroup) => {
    const selectable = group.runs.filter((r) => !r.already_downloaded);
    const allOn = selectable.every((r) => selected.has(r.accession));
    setSelected((prev) => {
      const next = new Set(prev);
      for (const run of selectable) {
        if (allOn) next.delete(run.accession);
        else next.add(run.accession);
      }
      return next;
    });
  };

  const groupState = (group: RunGroup): "all" | "none" | "some" => {
    const selectable = group.runs.filter((r) => !r.already_downloaded);
    if (selectable.length === 0) return "none";
    const on = selectable.filter((r) => selected.has(r.accession)).length;
    if (on === 0) return "none";
    return on === selectable.length ? "all" : "some";
  };
```

- [ ] **Step 4: Render the groups**

Replace the `<tbody>` contents so it renders either flat rows or group headers:

```tsx
                <tbody>
                  {!grouped &&
                    visible.map((run) => (
                      <RunRow
                        key={run.accession}
                        run={run}
                        checked={selected.has(run.accession)}
                        onToggle={() => toggle(run.accession)}
                      />
                    ))}

                  {grouped &&
                    visibleGroups.map((group) => {
                      const state = groupState(group);
                      const isCollapsed = collapsed.has(group.experiment);
                      return (
                        <Fragment key={group.experiment}>
                          <tr className="sra-group-row">
                            <td>
                              <input
                                type="checkbox"
                                checked={state === "all"}
                                ref={(el) => {
                                  // Tri-state has no HTML attribute; it is a
                                  // DOM property, so it must be set here.
                                  if (el) el.indeterminate = state === "some";
                                }}
                                onChange={() => toggleGroup(group)}
                                title="Select every run in this experiment"
                              />
                            </td>
                            <td colSpan={7}>
                              <button
                                type="button"
                                className="sra-group-toggle"
                                onClick={() =>
                                  setCollapsed((prev) => {
                                    const next = new Set(prev);
                                    if (next.has(group.experiment))
                                      next.delete(group.experiment);
                                    else next.add(group.experiment);
                                    return next;
                                  })
                                }
                              >
                                {isCollapsed ? "▸" : "▾"}
                                <span className="mono">{group.experiment}</span>
                                <span className="sra-dim">
                                  {group.runs.length}{" "}
                                  {group.runs.length === 1 ? "run" : "runs"}
                                  {group.bytes > 0 && ` · ${formatBytes(group.bytes)}`}
                                </span>
                                {group.title && (
                                  <span className="sra-dim">{group.title}</span>
                                )}
                              </button>
                            </td>
                          </tr>
                          {!isCollapsed &&
                            group.runs.map((run) => (
                              <RunRow
                                key={run.accession}
                                run={run}
                                checked={selected.has(run.accession)}
                                onToggle={() => toggle(run.accession)}
                              />
                            ))}
                        </Fragment>
                      );
                    })}
                </tbody>
```

Add `Fragment` to the React import at the top of the file.

- [ ] **Step 5: Add the styles**

In `frontend/src/index.css`:

```css
/* Experiment group header inside the run table. */
.sra-group-row {
  background: var(--bg-subtle, rgba(127, 127, 127, 0.08));
}

.sra-group-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 2px 0;
  background: none;
  border: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
```

If `--bg-subtle` is not defined in this stylesheet, use an existing variable — check what the other `.sra-*` rules use.

- [ ] **Step 6: Verify it compiles**

```bash
docker compose exec web npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 7: Manual verification**

At http://localhost:5173, open the download dialog:

1. `PRJNA1495534` — six experiment groups (`SRX34322662`, `SRX34322661`, …), all expanded, every run selected.
2. Uncheck four group headers. The footer count reflects only the two remaining groups' runs.
3. Check one run inside an unchecked group — that group's checkbox shows the indeterminate dash, not a tick.
4. Collapse a group — its runs hide, its selection is unchanged, the footer count does not move.
5. Sort by Size — rows reorder *within* each group; the groups themselves stay put.
6. `SRR11768093` — a flat single row, no group header.
7. Download two runs from two different experiments and confirm both land.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/NcbiDownloadDialog.tsx frontend/src/index.css
git commit -m "feat: collapsible experiment grouping for BioProject runs"
```

---

## Task 15: Full verification

**Files:** none modified — verification only.

- [ ] **Step 1: Rebuild the stack from the main repo root**

The bind mounts are relative paths, so Compose must be run from the main repo, never a worktree — otherwise port 5173 silently serves this branch and a later merge appears to lose the feature:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

- [ ] **Step 2: Confirm the stack is on the right tree**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

Expected: no path contains `.claude/worktrees/`.

- [ ] **Step 3: Run the whole backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass, no errors.

- [ ] **Step 4: Confirm both download handlers registered**

```bash
docker compose logs worker --tail 40 | grep handlers_loaded
```

Expected: both `download_sra_run` and `download_assembly`.

- [ ] **Step 5: End-to-end assembly download**

In the UI, download `GCF_000002445.2` (T. brucei — ~16 MB, small enough to be quick) with all four components. Then verify roles landed correctly in the database rather than trusting the UI:

```bash
docker compose exec api python -c "
import asyncio
from app.db.mongo import init_db
from app.models import DataObject

async def main():
    await init_db()
    objs = await DataObject.find(
        {'metadata.assembly_accession': 'GCF_000002445.2'}
    ).to_list()
    for o in objs:
        print(f'{o.role or \"(none)\":12} {o.name}')

asyncio.run(main())"
```

Expected exactly four rows: `reference` for `*_genomic.fna`, `annotation` for `genomic.gff`, `protein` for `protein.faa`, `transcript` for `cds_from_genomic.fna`.

**If `cds_from_genomic.fna` shows `reference`, stop.** That is the `.fna` collision, and it means `_label_components` is matching filenames in the wrong order or the catalog was not read.

Check the init helper's real name first — `app.db.mongo.init_db` is a guess:

```bash
grep -rn "async def init" backend/app/db/*.py
```

- [ ] **Step 6: Confirm the reference picker excludes the sequence sets**

Open an alignment dialog in the project from Step 5. The reference dropdown must offer the genome FASTA and must **not** list `protein.faa` or `cds_from_genomic.fna`. This is the concrete hazard the roles close.

- [ ] **Step 7: End-to-end BioProject grouping**

Resolve `PRJNA1495534`, select runs from two of the six experiments, and download. Confirm both arrive as FASTQ in the project with QC chained if the box was checked.

- [ ] **Step 8: Confirm the old SRA paths still respond**

```bash
docker compose exec api python -c "
import json, urllib.request
req = urllib.request.Request(
    'http://localhost:8000/api/v1/sra/resolve',
    data=json.dumps({'accession': 'SRR11768093'}).encode(),
    headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=60) as r:
    print(r.status, json.load(r)['kind'])"
```

Expected: `200 run`. Adjust the port if the API listens elsewhere in the container.

- [ ] **Step 9: Commit anything outstanding**

```bash
git status --short
```

Expected: clean. If not, review and commit deliberately.

---

## Self-review notes

**Spec coverage.** Every spec section maps to a task: classification → 5; API surface → 11; assembly resolution → 6; assembly download → 8; ingest and identification → 9; three new roles → 2, 3, 4; the dialog → 13; collapsible grouping → 14; tools and image → 1; testing → each task plus 15.

**One deliberate deviation from the spec.** The spec described `available_components()` as living in `assembly.py`. The plan splits it: the component *table* and parsing go in a new `assembly_components.py`, with only the CLI-shelling `component_availability()` added to `assembly.py`. Reason — the table is imported by the handler, the applier, the service and the router, and putting it in `assembly.py` would make every one of those import the NCBI HTTP client to read a static mapping.

**One thing the spec understated**, found while reading the code: `categorizeFile` in `ProjectExplorer.tsx:155` sends *all* FASTA to "reads", so without Task 4 a downloaded protein FASTA would file under Reads rather than merely being uncategorized. Task 4 adds a `sequences` category rather than relying on the existing ones.

**Unverified assumptions the implementer must check**, each flagged inline at the step that depends on it: `FieldType.INTEGER`'s member name (Task 3); whether `ingest_local_file` takes a `role` parameter (Task 9, Step 4); the frontend client's `post` helper name and the explorer's query key (Tasks 12, 13); `app.db.mongo.init_db`'s real path (Task 15, Step 5); `--bg-subtle`'s existence (Task 14).
