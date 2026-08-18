# Predicting a protein structure when none is deposited

**Issue:** [#533](https://github.com/syntheticgio/bioflow/issues/533)
**Date:** 2026-08-19
**Status:** design

## The request

> The Structure tab added in #477 shows a disabled "Predict structure" button.
> This ticket makes it real.

It covers two of that tab's four states:

- **A record whose header names no database accession** — annotation-tool output
  such as `>KLLIPMDF_00023 hypothetical protein`, where every record hits this.
  These have no identifier to resolve against UniProt, so structure prediction
  is the only path to a 3D view.
- **A record that resolves to a UniProt entry with no deposited structure** —
  roughly two thirds of resolved proteins have no PDB entry. These have a
  known sequence but no experimental structure.

## What already exists

The Structure tab from #477 (PR #539) ships:

- A paged, searchable record list for a protein FASTA's proteins.
- Identifier-first resolution through UniProt for headers naming `sp|...|` or
  `NP_`/`XP_`/`WP_` accessions.
- An iCn3D iframe that renders PDB structures.
- A **disabled** "Predict structure" button in every state.

The sequence bytes are addressable via `ProteinRecord.byte_offset`, recorded at
ingest specifically for this follow-up. The record's sequence can be read
without rescanning the file.

## What needs to be built

### The prediction engine decision

Three approaches exist, and the choice determines the architecture:

| Approach | Model | Speed | GPU | Pros | Cons |
|---|---|---|---|---|---|
| **Sidecar on genai** | ESMFold (MIT) | ~5-15 min | RTX 3090 | Persistent model, fits existing ds4 pattern, GPU-optional for CPU fallback | Must set up & maintain service |
| **Sidecar on genai** | Boltz-1 (MIT) | ~15-30 min | RTX 3090 | AlphaFold3-level accuracy, newer architecture | Slower, larger model |
| **Docker subprocess** | OpenFold (Apache 2) | ~30-60 min | Needs GPU passthrough | Fits existing job infrastructure | Cold start per job, Docker GPU setup needed |
| **Hosted API** | AlphaFold DB / ESM Atlas | Variable | None | No GPU needed | Only covers known proteins, rate-limited |

**Decision: ESMFold as a sidecar on the genai machine**, with these reasons:

1. **Fastest inference** — minutes rather than hours, critical for interactive use
   ("I clicked Predict, how long until I see something?")
2. **Lowest GPU requirement** — ~5 GB VRAM, fits the RTX 4060 Ti comfortably
3. **Open license** — MIT, no restrictions
4. **Fits your existing pattern** — ds4 runs as a sidecar on macOS via launchctl;
   this would run similarly on the genai machine
5. **CPU fallback** — ESMFold can run on CPU for small proteins (<400 aa) at
   ~2-3x slower, which is still acceptable for a local tool

**The sidecar runs a small FastAPI/Flask wrapper around `esm`** that:
- Loads the ESMFold model at startup (takes ~30s, done once)
- Exposes `POST /predict` accepting a protein sequence string
- Returns a PDB file (or CIF) with per-residue pLDDT confidence scores in the
  B-factor column
- Reports progress as a JSON stream or pollable status

### How prediction fits into the job system

Prediction is a pipeline job, not a request-time operation. The flow:

```
User clicks "Predict structure"
  → POST /objects/{id}/proteins/{ordinal}/predict
  → Creates a Job (type: "predict_structure", RunKind: PREDICTION)
  → Handler (HandlerMode.SUBPROCESS or THREAD) calls the sidecar HTTP API
  → Sidecar returns PDB bytes
  → PDB stored as an object artifact (a new file linked to the protein record)
  → Job completes
  → Frontend polls GET /objects/{id}/proteins/{ordinal}/prediction for status
  → When complete, renders the predicted structure in iCn3D
```

### Why a pipeline job rather than inline HTTP

- **Progress reporting** — prediction takes minutes; the user needs to see
  progress, not stare at a spinner
- **Cancellation** — the user can cancel a prediction like any other job
- **Retry** — transient failures (sidecar restarting, network blip) retry
  automatically
- **History** — the prediction appears in the activity view alongside alignments
  and QC runs
- **It matches every other long operation in this app** — consistency matters

### What about running it on the Mac Studio (no NVIDIA GPU)?

ESMFold can run on Apple Silicon via MPS (Metal Performance Shaders). The M3
Ultra has 256 GB of unified memory, and ESMFold's memory requirement (~5 GB) is
trivial. However, MPS support for ESMFold is experimental and slower than CUDA.

**The architecture supports either target transparently** — the sidecar is just
an HTTP endpoint. The user can run it on genai (CUDA, fast) or on the Mac Studio
(MPS, slower). The pipeline handler only needs a configurable URL.

### Sequence identity, not accession identity

A protein from an annotation tool has no accession, but it has a sequence. The
prediction is cached by **MD5 hash of the full protein sequence**, not by
accession. This means:

- The same sequence encountered in a different file reuses the cached prediction
- A sequence that was predicted once is never predicted again
- A record whose header names an accession but has no deposited structure also
  gets cached by sequence hash, so re-viewing it is instant

## Requirements

### Prediction initiation

**R1.** A user viewing a protein record with no deposited structure must be able
to click a button that initiates a structure prediction for that record's
sequence.

**R2.** A user viewing a protein record whose header names no accession must be
able to click a button that initiates a structure prediction for that record's
sequence.

**R3.** A user viewing a protein record that already has a predicted structure
must see "View prediction" instead of "Predict structure", and clicking it must
show the cached prediction immediately.

**R4.** The prediction button must be disabled while a prediction job is running
for that record.

### Prediction execution

**R5.** Starting a prediction must create a pipeline Job (type: `predict_structure`)
that can be tracked in the activity view and cancelled like any other job.

**R6.** The job payload must include the object ID, the record ordinal, and the
full protein sequence (read via byte_offset at job creation time).

**R7.** The job must call an HTTP endpoint on the prediction sidecar with the
protein sequence, with a per-request timeout of 30 minutes.

**R8.** The job must report progress (queued, running, percentage complete) so
the UI can show a progress indicator.

**R9.** The job must store the resulting PDB file as an artifact on the object,
keyed to the specific protein record (ordinal).

**R10.** The job must cache the result by sequence MD5 hash in a new
`ProteinPrediction` collection, so the same sequence is never predicted twice.

**R11.** A prediction that fails (timeout, sidecar unavailable, invalid response)
must fail the job with a descriptive error, and the user must be able to retry.

### Sidecar

**R12.** The sidecar must load the ESMFold model once at startup and keep it
resident, so that the first prediction after a cold start may be slow (~30s
model load + ~5min inference) but subsequent predictions skip the load.

**R13.** The sidecar must expose a `POST /predict` endpoint accepting a JSON body
with a `sequence` field (string of amino acid one-letter codes).

**R14.** The sidecar must validate the sequence (only valid amino acid characters,
length 20-2000 residues) and return a 400 for invalid input.

**R15.** The sidecar must return a PDB file with per-residue pLDDT scores in the
B-factor column (standard ESMFold output format).

**R16.** The sidecar must return a 503 with a retry-after hint if the model is
still loading or busy.

**R17.** The sidecar must log inference time, sequence length, and model version
for each prediction.

### Caching and storage

**R18.** A `ProteinPrediction` document must be created for each unique sequence
predicted, keyed by MD5 hash of the full sequence.

**R19.** Each `ProteinPrediction` must store: sequence hash, sequence length,
model name, model version, PDB file path, mean pLDDT, per-residue pLDDT array,
and creation timestamp.

**R20.** The PDB file must be stored on disk under the object's blob storage path
(`blob_path(object_id) / "predictions" / f"{ordinal}.pdb"`), so it survives
container restarts and is accessible via the existing file-serving infrastructure.

**R21.** A cached prediction must be discoverable from the protein record: the
record's ordinal and object_id map to a stored file path, and the sequence hash
looks up the cached metadata.

### Frontend

**R22.** The Predict button must transition through these states:

| State | Button text | Behavior |
|---|---|---|
| No prediction exists, no job running | "Predict structure" | Clickable, starts job |
| Prediction job running | "Predicting… (X%)" | Disabled, shows progress |
| Prediction job failed | "Retry prediction" | Clickable, re-starts job |
| Prediction complete | "View prediction" | Clickable, shows structure |
| Prediction cached (from another record) | "View prediction" | Clickable, shows structure |

**R23.** The structure viewer must render a predicted PDB the same way it renders
an experimental one — through the existing `Icn3dFrame` component.

**R24.** The predicted structure must be colored by pLDDT confidence:
- Blue (pLDDT > 90): Very high confidence
- Cyan (pLDDT > 70): Confident
- Yellow (pLDDT > 50): Low confidence
- Orange/Red (pLDDT < 50): Very low confidence

iCn3D supports per-residue coloring via the B-factor column, which is where
ESMFold writes pLDDT scores. No custom coloring code is needed — iCn3D
renders B-factor coloring by default.

**R25.** The frontend must poll the prediction status endpoint while a job is
running, updating the progress indicator.

**R26.** The frontend must stop polling when the job completes or fails.

### Non-functional

**R27.** Reading the protein sequence via byte_offset at job creation time must
complete in under 100 ms for any record.

**R28.** The PDB file for a typical protein (~400 aa) must be under 100 KB, so
storage and transfer costs are negligible.

**R29.** The sidecar must handle one prediction at a time (no batching). Multiple
concurrent requests queue and are processed sequentially.

**R30.** A sidecar outage must not affect any other app functionality. The
prediction button shows a "Service unavailable" state rather than crashing.

### Out of scope

- **Running prediction on the Mac Studio** — the sidecar architecture supports
  this, but the initial implementation targets genai only. Documentation covers
  how to run it on Apple Silicon if desired.
- **Batch prediction** — predicting every record of a proteome at once. The
  per-record interaction model is deliberate: prediction is expensive and the
  user should choose which proteins are worth the wait.
- **Confidence visualization beyond B-factor coloring** — iCnD3's default
  B-factor coloring is sufficient. A custom color legend is the only addition.
- **Structure relaxation/prediction refinement** — ESMFold's raw output is used
  as-is. No energy minimization or refinement step.

## Design

### Layer 0 — Sequence reading

The sequence for a protein record is read from the file at job creation time,
using the stored `byte_offset` and `length`. A new utility function in
`backend/app/storage/` reads exactly one record's bytes from a FASTA file:

```python
def read_protein_sequence(path: Path, byte_offset: int, length: int) -> str:
    """Read one protein record's sequence from a FASTA file.
    
    The byte_offset points at the '>' character. The record extends to
    the next '>' or EOF. Returns the concatenated sequence lines with
    newlines stripped.
    """
```

This is called from the API endpoint that creates the prediction job, not from
the handler — the handler receives the sequence string in its payload.

### Layer 1 — ProteinPrediction model

A new Beanie document in `backend/app/models/protein_prediction.py`:

```python
class ProteinPrediction(TimestampedDocument):
    sequence_hash: str  # MD5 of the full sequence
    sequence_length: int
    model_name: str  # "esmfold"
    model_version: str
    pdb_path: str  # Absolute path on disk
    mean_plddt: float
    plddt_per_residue: list[float]
    # Which record this prediction was created for (for display/reference)
    source_object_id: PydanticObjectId
    source_ordinal: int
    created_at: datetime
    
    class Settings:
        name = "protein_predictions"
        indexes = [
            IndexModel([("sequence_hash", ASCENDING)], unique=True, name="uniq_sequence_hash"),
        ]
```

### Layer 2 — Prediction endpoint and job creation

A new endpoint `POST /objects/{id}/proteins/{ordinal}/predict`:

1. Reads the object and verifies the record exists
2. Reads the sequence from the file via byte_offset
3. Computes the MD5 hash of the sequence
4. Checks `ProteinPrediction` for an existing prediction by hash
5. If found, returns immediately with the cached prediction metadata
6. If not found, creates a Job with type `predict_structure` containing
   `object_id`, `ordinal`, `sequence`, `sequence_hash`
7. Returns the Job summary (status URL, job ID)

A status endpoint `GET /objects/{id}/proteins/{ordinal}/prediction`:

1. Checks `ProteinPrediction` by sequence hash (stored on the protein record or
   looked up via ordinal)
2. If found, returns prediction metadata and PDB URL
3. If a job is running, returns job status and progress
4. If no job and no prediction, returns "not_started"

### Layer 3 — Prediction handler

A new handler `predict_structure` in `backend/app/queue/prediction_handlers.py`:

```python
@handler(
    "predict_structure",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=2, mem_mb=4096, io=IoClass.LIGHT),
    max_attempts=2,
)
def predict_structure(ctx: JobContext) -> dict:
    # Read sequence from payload
    # POST to sidecar URL (configurable, from settings)
    # Write response PDB to blob_path / predictions / {ordinal}.pdb
    # Compute mean pLDTT
    # Save ProteinPrediction document
    # Return result dict
```

Uses `HandlerMode.SUBPROCESS` because it makes an HTTP call and writes files —
it's I/O-bound but synchronous, and the SUBPROCESS mode runs it off the event
loop. The sidecar URL comes from a new setting `PREDICTION_SIDECAR_URL`
(default: `http://192.168.1.237:21235`).

### Layer 4 — Sidecar

A standalone Python service on the genai machine. Minimal FastAPI app:

```
genai:/opt/esmfold-sidecar/
├── main.py          # FastAPI app
├── requirements.txt # fastapi, uvicorn, esm[fold]
└── run.sh           # launchctl plist or systemd unit
```

`POST /predict`:
- Input: `{"sequence": "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHF..."}`
- Output: PDB file bytes with Content-Type `chemical/x-pdb`
- Status codes: 200 (success), 400 (invalid sequence), 503 (loading/busy)

The sidecar runs on port 21235 (adjacent to ds4's 21234). Managed via systemd
on the genai Ubuntu machine.

### Layer 5 — Frontend changes

The `PredictButton` component in `ProteinStructureTab.tsx` changes from
always-disabled to a stateful component that:

1. On mount, checks `GET /objects/{id}/proteins/{ordinal}/prediction`
2. Renders the appropriate button based on the response
3. When clicked, calls `POST /objects/{id}/proteins/{ordinal}/predict`
4. Polls the status endpoint while the job runs (every 5 seconds)
5. On completion, shows the predicted structure in the existing `Icn3dFrame`

The `RecordStructure` component gains a new state `predicted` that renders the
PDB from the prediction endpoint alongside the cached prediction metadata
(model name, confidence score).

The pLDDT color legend is added as a small key below the iCn3D frame:
```
Confidence key: ██ Very high (90+) ██ Confident (70-90) ██ Low (50-70) ██ Very low (<50)
```

### State machine for the structure panel

Current states: `loading`, `failed`, `lookup_failed`, `no_reference`,
`no_structure`, `resolved`

New states added: `predicted` (shows predicted structure), `predicting` (shows
progress bar), `prediction_failed` (shows error + retry), `prediction_unavailable`
(sidecar down)

The full state table:

| State | Shows | Predict button |
|---|---|---|
| `loading` | Spinner | Disabled |
| `failed` | Error box | Disabled |
| `lookup_failed` | Error + retry | Disabled |
| `no_reference` | "No known protein" | **Enabled** → "Predict structure" |
| `no_structure` | "No experimental structure" | **Enabled** → "Predict structure" |
| `resolved` | iCn3D with experimental PDB | **Enabled** → "Predict structure" (for other chains) |
| `predicted` | iCn3D with predicted PDB + confidence key | **Enabled** → "View prediction" |
| `predicting` | Progress bar + "Predicting… (X%)" | **Disabled** |
| `prediction_failed` | Error + "Service unavailable" | **Enabled** → "Retry prediction" |

### What the frontend API client gains

```typescript
// Check prediction status for a record
proteinRecordPrediction: (objectId: string, ordinal: number) =>
  request<ProteinPredictionStatus>(`/objects/${objectId}/proteins/${ordinal}/prediction`),

// Start a prediction
startProteinPrediction: (objectId: string, ordinal: number) =>
  request<JobSummary>(`/objects/${objectId}/proteins/${ordinal}/predict`, {
    method: "POST",
  }),
```

### New frontend types

```typescript
export type PredictionState = "not_started" | "running" | "completed" | "failed";

export interface ProteinPredictionStatus {
  state: PredictionState;
  job_id: string | null;
  progress: { pct: number; message: string } | null;
  prediction: {
    model_name: string;
    model_version: string;
    mean_plddt: number;
    pdb_path: string;  // URL to the PDB file
  } | null;
}
```

## Testing

### Backend tests

- **Sequence reading**: Test reading a record's sequence from a FASTA file using
  byte_offset, including edge cases (last record, single-record file, CRLF
  line endings, trailing newlines).
- **Prediction endpoint**: Test that creating a prediction job returns a JobSummary,
  that checking status on a cached prediction returns the metadata, that an
  unknown record returns 404.
- **Handler**: Test that the handler calls the configured sidecar URL, handles
  timeouts, stores the PDB, and creates the ProteinPrediction document.
- **Sidecar validation**: Test that invalid sequences (empty, too long, bad chars)
  return 400.

### Integration tests

- Run the full prediction flow against a real (or mocked) sidecar:
  1. Select a record with no structure
  2. Click Predict
  3. Verify job created
  4. Wait for completion
  5. Verify PDB stored and cached
  6. Verify iCn3D renders the predicted structure

### Sidecar tests

- Unit tests for sequence validation
- Integration test: POST a known sequence, verify PDB output has correct format
  and B-factor column contains pLDDT values
- Test concurrent requests are queued (not rejected)

## Configuration

New setting in `backend/app/config.py`:

```python
# URL of the ESMFold prediction sidecar. Empty string or None means
# prediction is unavailable — the UI shows "Service not configured."
PREDICTION_SIDECAR_URL: str = ""
```

Set in `.env`:
```
PREDICTION_SIDECAR_URL=http://192.168.1.237:21235
```

## Follow-up tickets

1. **CPU fallback on Mac Studio** — run the sidecar on Apple Silicon via MPS,
   for users without a GPU machine.
2. **Batch prediction** — predict all unresolved proteins in a file with one
   click, for proteome-scale annotation validation.
3. **Boltz-1 upgrade** — replace or augment ESMFold with Boltz-1 when it
   matures, for AlphaFold3-level accuracy on challenging targets.
4. **Structure relaxation** — run OpenMM energy minimization on predicted
   structures for improved geometry.
