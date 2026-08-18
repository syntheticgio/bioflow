# Protein Structure Prediction Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make the disabled "Predict structure" button in the Structure tab functional — predict a protein's 3D structure via ESMFold running as a sidecar on the genai machine, cache results by sequence hash, and render predictions in the existing iCn3D viewer.

**Architecture:** A pipeline job (HandlerMode.SUBPROCESS) calls an ESMFold sidecar HTTP service on the genai machine (port 21235). The sidecar loads the model once and exposes `POST /predict`. Results are cached by sequence MD5 hash in a new `ProteinPrediction` collection. The frontend polls for completion and renders predicted PDBs through the existing `Icn3dFrame` with B-factor pLDDT coloring.

**Tech Stack:** Python (ESMFold via `esm` package), FastAPI (sidecar), Beanie/Mongo (cache), iCn3D (rendering), existing pipeline job infrastructure.

**Design doc:** `docs/superpowers/specs/2026-08-19-protein-structure-prediction-design.md`

---

### Task 1: Add sequence-reading utility

**Objective:** Read one protein record's sequence from a FASTA file using its stored byte_offset and length, without re-scanning the file.

**Files:**
- Create: `backend/app/storage/sequence_reader.py`
- Test: `backend/tests/storage/test_sequence_reader.py`

**Step 1: Write failing test**

```python
# tests/storage/test_sequence_reader.py
"""Test reading a protein record's sequence from a FASTA file by byte offset."""
import tempfile
from pathlib import Path
from app.storage.sequence_reader import read_protein_sequence


def test_read_first_record():
    """Read the first record of a two-record FASTA."""
    fasta = ">sp|P00924|ENO1_YEAST\nMVLSPADKTNVKAAW\nGKVGAHAGEYGAEALER\n>sp|P00925|ENO2_YEAST\nSEQUENCE2\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".fasta", delete=False) as f:
        f.write(fasta)
        path = f.name
    try:
        seq = read_protein_sequence(Path(path), byte_offset=0, length=3)
        assert seq == "MVLSPADKTNVKAAWGKVGAHAGEYGAEALER", f"Got: {seq}"
    finally:
        Path(path).unlink(missing_ok=True)


def test_read_last_record():
    """Read the last record — no trailing newline after sequence."""
    fasta = ">sp|P00924|ENO1_YEAST\nMVLSPADKTNVKAAW\n>sp|P00925|ENO2_YEAST\nSEQUENCE2"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".fasta", delete=False) as f:
        f.write(fasta)
        path = f.name
    try:
        seq = read_protein_sequence(Path(path), byte_offset=len(">sp|P00924|ENO1_YEAST\nMVLSPADKTNVKAAW\n"), length=2)
        assert seq == "SEQUENCE2", f"Got: {seq}"
    finally:
        Path(path).unlink(missing_ok=True)


def test_single_record():
    """A single-record FASTA with trailing newline."""
    fasta = ">test\nACDEFGHIKLMNPQRSTVWY\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".fasta", delete=False) as f:
        f.write(fasta)
        path = f.name
    try:
        seq = read_protein_sequence(Path(path), byte_offset=0, length=1)
        assert seq == "ACDEFGHIKLMNPQRSTVWY", f"Got: {seq}"
    finally:
        Path(path).unlink(missing_ok=True)


def test_crlf_line_endings():
    """CRLF line endings must not produce stray \\r characters."""
    fasta = ">test\r\nACDEF\r\nGHIKL\r\n"
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".fasta", delete=False) as f:
        f.write(fasta.encode("utf-8"))
        path = f.name
    try:
        seq = read_protein_sequence(Path(path), byte_offset=0, length=1)
        assert seq == "ACDEFGHIKL", f"Got: {repr(seq)}"
    finally:
        Path(path).unlink(missing_ok=True)
```

**Step 2: Run test to verify failure**

```bash
python -m pytest backend/tests/storage/test_sequence_reader.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.storage.sequence_reader'`

**Step 3: Write minimal implementation**

```python
# backend/app/storage/sequence_reader.py
"""Read one protein record's sequence from a FASTA file by byte offset.

The byte_offset points at the '>' character of the record's header line.
The record extends to the next '>' or EOF. Sequence lines are concatenated
with newlines stripped (including \\r for CRLF files).
"""
from pathlib import Path


def read_protein_sequence(path: Path, byte_offset: int, length: int) -> str:
    """Return the amino-acid sequence of one protein record.
    
    Args:
        path: Path to the FASTA file.
        byte_offset: Byte offset of the '>' character (0-based).
        length: Number of amino acids (ProteinRecord.length).
    
    Returns:
        The concatenated sequence lines with whitespace stripped.
    """
    with open(path, "rb") as f:
        f.seek(byte_offset)
        lines = []
        for line in f:
            if line.startswith(b">") and lines:
                # We've hit the next record
                break
            # Strip \n, \r\n, and any trailing whitespace
            lines.append(line.decode("utf-8", errors="replace").strip())
    
    # First line is the header (starts with >) — remove it
    if lines and lines[0].startswith(">"):
        lines.pop(0)
    
    return "".join(lines)
```

**Step 4: Run test to verify pass**

```bash
python -m pytest backend/tests/storage/test_sequence_reader.py -v
```

Expected: 4 passed

**Step 5: Commit**

```bash
git add backend/app/storage/sequence_reader.py backend/tests/storage/test_sequence_reader.py
git commit -m "feat: add utility to read a protein sequence by byte offset from a FASTA file"
```

---

### Task 2: Create ProteinPrediction model

**Objective:** Add the Beanie document model for caching predicted structures by sequence hash.

**Files:**
- Create: `backend/app/models/protein_prediction.py`
- Modify: `backend/app/models/__init__.py` (export new model)
- Test: `backend/tests/models/test_protein_prediction.py`

**Step 1: Write failing test**

```python
# tests/models/test_protein_prediction.py
"""ProteinPrediction document creation and query."""
import pytest
from app.models.protein_prediction import ProteinPrediction


@pytest.mark.asyncio
async def test_create_prediction():
    pred = ProteinPrediction(
        sequence_hash="abc123",
        sequence_length=100,
        model_name="esmfold",
        model_version="2.0.1",
        pdb_path="/data/predictions/obj1/0.pdb",
        mean_plddt=85.5,
        plddt_per_residue=[0.9, 0.8, 0.85],
        source_object_id="507f1f77bcf86cd799439011",  # valid ObjectId hex
        source_ordinal=0,
    )
    # Just validates the model can be instantiated
    assert pred.sequence_hash == "abc123"
    assert pred.mean_plddt == 85.5


@pytest.mark.asyncio
async def test_prediction_indexes():
    """Verify the unique index on sequence_hash exists."""
    indexes = ProteinPrediction.Settings.indexes
    assert len(indexes) == 1
    assert indexes[0].name == "uniq_sequence_hash"
```

**Step 2: Run test to verify failure**

```bash
python -m pytest backend/tests/models/test_protein_prediction.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.protein_prediction'`

**Step 3: Write minimal implementation**

```python
# backend/app/models/protein_prediction.py
"""Cached protein structure predictions, keyed by sequence hash.

A prediction is identified by the MD5 hash of the full amino acid sequence,
not by accession — a protein from an annotation tool has no accession but
still benefits from caching.
"""
from datetime import datetime

from beanie import PydanticObjectId
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class ProteinPrediction(TimestampedDocument):
    """One sequence that has been predicted by a structure prediction model."""

    sequence_hash: str = Field(description="MD5 hex digest of the full sequence")
    sequence_length: int
    model_name: str
    model_version: str
    pdb_path: str = Field(description="Absolute path to the PDB file on disk")
    mean_plddt: float = Field(ge=0.0, le=1.0)
    plddt_per_residue: list[float] = Field(default_factory=list)
    source_object_id: PydanticObjectId
    source_ordinal: int
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "protein_predictions"
        indexes = [
            IndexModel([("sequence_hash", ASCENDING)], unique=True, name="uniq_sequence_hash"),
        ]
```

Add to `backend/app/models/__init__.py`:
```python
from app.models.protein_prediction import ProteinPrediction
# Add to __all__
```

**Step 4: Run test to verify pass**

```bash
python -m pytest backend/tests/models/test_protein_prediction.py -v
```

Expected: 2 passed

**Step 5: Commit**

```bash
git add backend/app/models/protein_prediction.py backend/app/models/__init__.py backend/tests/models/test_protein_prediction.py
git commit -m "feat: add ProteinPrediction model for cached structure predictions"
```

---

### Task 3: Add config setting for prediction sidecar URL

**Objective:** Add `PREDICTION_SIDECAR_URL` to the app config so the handler knows where to call.

**Files:**
- Modify: `backend/app/config.py`

**Step 1: Find the config class and add the setting**

Search for the settings class pattern:

```bash
grep -n "class Settings" backend/app/config.py
```

**Step 2: Add the setting**

Add after existing URL/endpoint settings:
```python
# URL of the ESMFold prediction sidecar. Empty string means prediction
# is unavailable — the UI shows "Service not configured."
PREDICTION_SIDECAR_URL: str = ""
```

**Step 3: Verify it loads**

```bash
python -c "from app.config import settings; print(repr(settings.PREDICTION_SIDECAR_URL))"
```

Expected: `''`

**Step 4: Commit**

```bash
git add backend/app/config.py
git commit -m "feat: add PREDICTION_SIDECAR_URL config setting"
```

---

### Task 4: Create prediction API endpoints

**Objective:** Add two endpoints: POST to start a prediction job, GET to check prediction status.

**Files:**
- Modify: `backend/app/api/v1/objects.py`
- Modify: `backend/app/api/v1/schemas.py` (new response models)
- Test: `backend/tests/api/test_protein_prediction.py`

**Step 1: Add response schemas**

In `backend/app/api/v1/schemas.py`, add:

```python
class PredictionState(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PredictionProgress(BaseModel):
    pct: float = Field(ge=0, le=100)
    message: str = ""


class PredictionResult(BaseModel):
    model_name: str
    model_version: str
    mean_plddt: float
    pdb_url: str  # URL to serve the PDB file


class ProteinPredictionStatus(BaseModel):
    state: PredictionState
    job_id: str | None = None
    progress: PredictionProgress | None = None
    prediction: PredictionResult | None = None
```

**Step 2: Add endpoints**

In `backend/app/api/v1/objects.py`, add:

```python
@router.post(
    "/{object_id}/protein-records/{ordinal}/predict",
    response_model=JobSummary,
)
async def start_protein_prediction(
    object_id: PydanticObjectId,
    ordinal: int,
    owner: OwnerDep,
    background_tasks: BackgroundTasks,
) -> JobSummary:
    """Start a structure prediction for one protein record.
    
    Reads the sequence from the file via byte_offset, checks for a cached
    prediction by sequence hash, and either returns the cached result or
    creates a predict_structure job.
    """
    obj = await object_service.get_object(object_id, owner=owner)
    record = await ProteinRecord.find_one(
        ProteinRecord.object_id == obj.id, ProteinRecord.ordinal == ordinal
    )
    if record is None:
        raise NotFoundError(f"No protein record {ordinal} for this file.")
    
    # Read the sequence
    seq = read_protein_sequence(
        Path(blob_path(obj.id)) / obj.filename, record.byte_offset, record.length
    )
    seq_hash = hashlib.md5(seq.encode()).hexdigest()
    
    # Check cache
    cached = await ProteinPrediction.find_one(
        ProteinPrediction.sequence_hash == seq_hash
    )
    if cached is not None:
        return _prediction_job_summary(cached, obj.id)
    
    # No cache — create a job
    if not settings.PREDICTION_SIDECAR_URL:
        raise BadRequestError("Structure prediction is not configured.")
    
    job = await queue.enqueue(
        job_type="predict_structure",
        payload={
            "object_id": str(obj.id),
            "ordinal": ordinal,
            "sequence": seq,
            "sequence_hash": seq_hash,
        },
        owner=owner,
        label=f"Predict structure for {record.identifier}",
    )
    return job.to_summary()


@router.get(
    "/{object_id}/protein-records/{ordinal}/prediction",
    response_model=ProteinPredictionStatus,
)
async def get_protein_prediction_status(
    object_id: PydanticObjectId,
    ordinal: int,
    owner: OwnerDep,
) -> ProteinPredictionStatus:
    """Check prediction status for one protein record."""
    obj = await object_service.get_object(object_id, owner=owner)
    record = await ProteinRecord.find_one(
        ProteinRecord.object_id == obj.id, ProteinRecord.ordinal == ordinal
    )
    if record is None:
        raise NotFoundError(f"No protein record {ordinal} for this file.")
    
    # Read sequence to compute hash
    seq = read_protein_sequence(
        Path(blob_path(obj.id)) / obj.filename, record.byte_offset, record.length
    )
    seq_hash = hashlib.md5(seq.encode()).hexdigest()
    
    # Check cache
    cached = await ProteinPrediction.find_one(
        ProteinPrediction.sequence_hash == seq_hash
    )
    if cached is not None:
        return ProteinPredictionStatus(
            state=PredictionState.COMPLETED,
            prediction=PredictionResult(
                model_name=cached.model_name,
                model_version=cached.model_version,
                mean_plddt=cached.mean_plddt,
                pdb_url=f"/objects/{obj.id}/protein-records/{ordinal}/prediction.pdb",
            ),
        )
    
    # Check for running job
    job = await Job.find_one(
        Job.object_id == obj.id,
        Job.job_type == "predict_structure",
        Job.state == JobState.RUNNING,
        {"payload.ordinal": ordinal},
    )
    if job is not None:
        return ProteinPredictionStatus(
            state=PredictionState.RUNNING,
            job_id=str(job.id),
            progress=PredictionProgress(
                pct=job.progress.pct if job.progress else 0,
                message=job.progress.message if job.progress else "Starting prediction…",
            ),
        )
    
    return ProteinPredictionStatus(state=PredictionState.NOT_STARTED)


@router.get(
    "/{object_id}/protein-records/{ordinal}/prediction.pdb",
)
async def get_protein_prediction_pdb(
    object_id: PydanticObjectId,
    ordinal: int,
    owner: OwnerDep,
):
    """Serve the predicted PDB file for a protein record."""
    obj = await object_service.get_object(object_id, owner=owner)
    record = await ProteinRecord.find_one(
        ProteinRecord.object_id == obj.id, ProteinRecord.ordinal == ordinal
    )
    if record is None:
        raise NotFoundError(f"No protein record {ordinal} for this file.")
    
    # Read sequence to compute hash
    seq = read_protein_sequence(
        Path(blob_path(obj.id)) / obj.filename, record.byte_offset, record.length
    )
    seq_hash = hashlib.md5(seq.encode()).hexdigest()
    
    cached = await ProteinPrediction.find_one(
        ProteinPrediction.sequence_hash == seq_hash
    )
    if cached is None:
        raise NotFoundError("No prediction for this record.")
    
    return FileResponse(cached.pdb_path, media_type="chemical/x-pdb")
```

**Step 3: Write test**

```python
# tests/api/test_protein_prediction.py
"""Test prediction API endpoints."""
import pytest
from unittest.mock import patch, AsyncMock


@pytest.mark.asyncio
async def test_start_prediction_no_cache_creates_job(client, monkeypatch):
    """POST /{id}/protein-records/0/predict creates a job when no cache."""
    # Mock the sequence reader, cache check, and queue
    monkeypatch.setattr("app.api.v1.objects.read_protein_sequence", lambda *a: "ACDEFGHIKLMNPQRSTVWY")
    monkeypatch.setattr("app.api.v1.objects.ProteinPrediction.find_one", AsyncMock(return_value=None))
    monkeypatch.setattr("app.api.v1.objects.settings.PREDICTION_SIDECAR_URL", "http://sidecar:21235")
    mock_enqueue = AsyncMock(return_value=Mock(id="job123"))
    monkeypatch.setattr("app.api.v1.objects.queue.enqueue", mock_enqueue)
    
    # ... (full test body)
```

**Step 4: Run tests**

```bash
python -m pytest backend/tests/api/test_protein_prediction.py -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add backend/app/api/v1/objects.py backend/app/api/v1/schemas.py backend/tests/api/test_protein_prediction.py
git commit -m "feat: add predict and prediction-status API endpoints"
```

---

### Task 5: Create prediction handler

**Objective:** Implement the `predict_structure` handler that calls the sidecar, stores the PDB, and caches the result.

**Files:**
- Create: `backend/app/queue/prediction_handlers.py`
- Modify: `backend/app/queue/handlers.py` (import the new module)
- Modify: `backend/app/models/run.py` (add PREDICTION to RunKind)
- Test: `backend/tests/queue/test_prediction_handlers.py`

**Step 1: Add RunKind.PREDICTION**

In `backend/app/models/run.py`, add to `RunKind`:
```python
PREDICTION = "prediction"
```

**Step 2: Write the handler**

```python
# backend/app/queue/prediction_handlers.py
"""Structure prediction handlers that call the ESMFold sidecar."""
import hashlib
import json
import urllib.request
import urllib.error
from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import Job, JobState, ProteinPrediction
from app.queue.registry import HandlerMode, JobContext, handler
from app.storage.paths import blob_path

log = get_logger(__name__)

_PREDICTION_TIMEOUT = 30 * 60  # 30 minutes max

# Valid amino acid one-letter codes
_VALID_AA = set("ACDEFGHIKLMNPQRSTVWY")


def _validate_sequence(seq: str) -> None:
    """Raise PermanentError if the sequence is invalid."""
    if not seq:
        raise PermanentError("Empty sequence")
    if len(seq) < 20:
        raise PermanentError(f"Sequence too short ({len(seq)} aa, minimum 20)")
    if len(seq) > 2000:
        raise PermanentError(f"Sequence too long ({len(seq)} aa, maximum 2000)")
    invalid = set(seq.upper()) - _VALID_AA
    if invalid:
        raise PermanentError(f"Invalid amino acids in sequence: {''.join(sorted(invalid))}")


@handler(
    "predict_structure",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=2, mem_mb=4096, io=IoClass.LIGHT),
    max_attempts=2,
)
def predict_structure(ctx: JobContext) -> dict:
    """Predict a protein structure via the ESMFold sidecar.
    
    Payload: { object_id, ordinal, sequence, sequence_hash }
    """
    object_id = ctx.payload.get("object_id")
    ordinal = ctx.payload.get("ordinal")
    sequence = ctx.payload.get("sequence", "")
    sequence_hash = ctx.payload.get("sequence_hash", "")
    
    if not all([object_id, ordinal is not None, sequence, sequence_hash]):
        raise PermanentError("Missing required payload fields")
    
    _validate_sequence(sequence)
    
    sidecar_url = settings.PREDICTION_SIDECAR_URL
    if not sidecar_url:
        raise PermanentError("Structure prediction sidecar is not configured")
    
    # Call the sidecar
    req_data = json.dumps({"sequence": sequence}).encode()
    req = urllib.request.Request(
        f"{sidecar_url}/predict",
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    
    try:
        ctx.progress(pct=10, message="Contacting prediction service…")
        with urllib.request.urlopen(req, timeout=_PREDICTION_TIMEOUT) as resp:
            pdb_bytes = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 400:
            body = e.read().decode()
            raise PermanentError(f"Sidecar rejected sequence: {body}")
        raise RetryableError(f"Sidecar HTTP {e.code}: {e.reason}")
    except (urllib.error.URLError, OSError) as e:
        raise RetryableError(f"Could not reach prediction sidecar: {e}")
    
    if not pdb_bytes:
        raise RetryableError("Sidecar returned empty response")
    
    ctx.progress(pct=50, message="Saving predicted structure…")
    
    # Store the PDB file
    obj_blob_path = blob_path(object_id)
    pred_dir = obj_blob_path / "predictions"
    pred_dir.mkdir(parents=True, exist_ok=True)
    pdb_path = pred_dir / f"{ordinal}.pdb"
    pdb_path.write_bytes(pdb_bytes)
    
    # Parse mean pLDDT from the PDB B-factor column
    mean_plddt = _parse_mean_plddt(pdb_bytes)
    
    ctx.progress(pct=80, message="Caching prediction result…")
    
    # Cache the result (in a thread-safe way — use run_from_thread)
    from app.db.client import run_from_thread
    run_from_thread(
        ProteinPrediction.find_one(
            ProteinPrediction.sequence_hash == sequence_hash
        ).upsert(
            {"$set": {
                "sequence_length": len(sequence),
                "model_name": "esmfold",
                "model_version": _get_esmfold_version(),
                "pdb_path": str(pdb_path),
                "mean_plddt": mean_plddt,
                "plddt_per_residue": _parse_plddt_array(pdb_bytes),
                "source_object_id": object_id,
                "source_ordinal": ordinal,
            }},
            on_insert=ProteinPrediction(
                sequence_hash=sequence_hash,
                sequence_length=len(sequence),
                model_name="esmfold",
                model_version=_get_esmfold_version(),
                pdb_path=str(pdb_path),
                mean_plddt=mean_plddt,
                plddt_per_residue=_parse_plddt_array(pdb_bytes),
                source_object_id=object_id,
                source_ordinal=ordinal,
            ),
        )
    )
    
    return {
        "object_id": object_id,
        "ordinal": ordinal,
        "sequence_hash": sequence_hash,
        "pdb_path": str(pdb_path),
        "mean_plddt": mean_plddt,
    }


def _parse_mean_plddt(pdb_bytes: bytes) -> float:
    """Extract mean pLDDT from the B-factor column (columns 61-66) of a PDB file."""
    scores = []
    for line in pdb_bytes.decode().splitlines():
        if line.startswith(("ATOM", "HETATM")):
            try:
                bfactor = float(line[60:66].strip())
                scores.append(bfactor)
            except (ValueError, IndexError):
                continue
    if not scores:
        return 0.0
    return sum(scores) / len(scores) / 100.0  # Normalize to 0-1


def _parse_plddt_array(pdb_bytes: bytes) -> list[float]:
    """Extract per-residue pLDDT values from the B-factor column."""
    scores = []
    seen_residues = set()
    for line in pdb_bytes.decode().splitlines():
        if line.startswith(("ATOM", "HETATM")):
            try:
                residue_id = (line[21:26].strip(), line[17:20].strip())
                if residue_id not in seen_residues:
                    seen_residues.add(residue_id)
                    bfactor = float(line[60:66].strip())
                    scores.append(bfactor / 100.0)
            except (ValueError, IndexError):
                continue
    return scores


def _get_esmfold_version() -> str:
    """Get the ESMFold version string."""
    try:
        import esm
        return getattr(esm, "__version__", "unknown")
    except ImportError:
        return "unknown"
```

**Step 3: Register the handler**

In `backend/app/queue/handlers.py`, add at the top:
```python
from app.queue import prediction_handlers  # noqa: F401 — registers predict_structure handler
```

**Step 4: Write test**

```python
# tests/queue/test_prediction_handlers.py
"""Test predict_structure handler."""
import json
from unittest.mock import patch, MagicMock
from app.queue.prediction_handlers import predict_structure, _validate_sequence


def test_validate_sequence_valid():
    _validate_sequence("ACDEFGHIKLMNPQRSTVWY" * 2)  # 40 aa


def test_validate_sequence_too_short():
    with pytest.raises(PermanentError, match="too short"):
        _validate_sequence("ACD")


def test_validate_sequence_too_long():
    with pytest.raises(PermanentError, match="too long"):
        _validate_sequence("A" * 2001)


def test_validate_sequence_invalid_chars():
    with pytest.raises(PermanentError, match="Invalid amino acids"):
        _validate_sequence("ACDEFGHIKLMNPQRSTVWYB")  # B is not valid
```

**Step 5: Run tests**

```bash
python -m pytest backend/tests/queue/test_prediction_handlers.py -v
```

Expected: PASS

**Step 6: Commit**

```bash
git add backend/app/queue/prediction_handlers.py backend/app/queue/handlers.py backend/app/models/run.py backend/tests/queue/test_prediction_handlers.py
git commit -m "feat: add predict_structure pipeline handler"
```

---

### Task 6: Build the ESMFold sidecar

**Objective:** Create the standalone FastAPI service that loads ESMFold and exposes a predict endpoint.

**Files:**
- Create: `ops/esmfold-sidecar/main.py`
- Create: `ops/esmfold-sidecar/requirements.txt`
- Create: `ops/esmfold-sidecar/run.sh`
- Create: `ops/esmfold-sidecar/test_sidecar.py`

**Step 1: Write the sidecar**

```python
# ops/esmfold-sidecar/main.py
"""ESMFold structure prediction sidecar.

FastAPI service that loads an ESMFold model at startup and exposes
POST /predict for single-sequence structure prediction.
"""
import logging
import time
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Valid amino acid one-letter codes
_VALID_AA = set("ACDEFGHIKLMNPQRSTVWY")

model = None
model_version = "unknown"


class PredictRequest(BaseModel):
    sequence: str = Field(..., min_length=20, max_length=2000)
    
    @field_validator("sequence")
    @classmethod
    def validate_sequence(cls, v):
        v = v.strip().upper()
        invalid = set(v) - _VALID_AA
        if invalid:
            raise ValueError(f"Invalid amino acids: {''.join(sorted(invalid))}")
        return v


class PredictResponse(BaseModel):
    status: str
    sequence_length: int
    inference_time_s: float
    model_version: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, model_version
    log.info("Loading ESMFold model…")
    start = time.time()
    try:
        import esm
        model = esm.pretrained.esmfold_v1()
        model = model.eval()
        model_version = getattr(esm, "__version__", "unknown")
        # Move to GPU if available
        if torch.cuda.is_available():
            model = model.cuda()
            log.info("Using CUDA GPU")
        else:
            log.info("Using CPU (no CUDA detected)")
        log.info(f"Model loaded in {time.time() - start:.1f}s, version={model_version}")
    except Exception as e:
        log.error(f"Failed to load ESMFold model: {e}")
        raise
    yield


app = FastAPI(title="ESMFold Sidecar", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "model_loaded": model is not None, "model_version": model_version}


@app.post("/predict", response_model=PredictResponse)
async def predict(request: PredictRequest):
    if model is None:
        raise HTTPException(status_code=503, detail="Model is still loading")
    
    seq = request.sequence.strip().upper()
    log.info(f"Predicting structure for sequence of length {len(seq)}")
    
    start = time.time()
    try:
        with torch.no_grad():
            output = model.infer(seq)
        
        pdb_str = model.output_to_pdb(output)
        inference_time = time.time() - start
        log.info(f"Prediction complete in {inference_time:.1f}s, length={len(seq)}")
        
        return Response(
            content=pdb_str,
            media_type="chemical/x-pdb",
            headers={
                "X-Inference-Time-S": f"{inference_time:.1f}",
                "X-Model-Version": model_version,
                "X-Sequence-Length": str(len(seq)),
            },
        )
    except Exception as e:
        log.error(f"Prediction failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
```

```txt
# ops/esmfold-sidecar/requirements.txt
fastapi>=0.115.0
uvicorn[standard]>=0.34.0
torch>=2.4.0
esm[fold]>=3.0.0
```

```bash
# ops/esmfold-sidecar/run.sh
#!/bin/bash
# Start the ESMFold sidecar on port 21235
# Usage: ./run.sh [port]
PORT=${1:-21235}
cd "$(dirname "$0")"
exec uvicorn main:app --host 0.0.0.0 --port "$PORT" --log-level info
```

**Step 3: Write test**

```python
# ops/esmfold-sidecar/test_sidecar.py
"""Test the sidecar API (requires running instance)."""
import requests


def test_health():
    resp = requests.get("http://localhost:21235/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_predict_invalid_sequence():
    resp = requests.post(
        "http://localhost:21235/predict",
        json={"sequence": "INVALID_B"},
    )
    assert resp.status_code == 400


def test_predict_short_sequence():
    resp = requests.post(
        "http://localhost:21235/predict",
        json={"sequence": "ACD"},
    )
    assert resp.status_code == 400
```

**Step 4: Commit**

```bash
git add ops/esmfold-sidecar/
git commit -m "feat: add ESMFold sidecar service for structure prediction"
```

---

### Task 7: Update frontend PredictButton

**Objective:** Make the PredictButton stateful — check prediction status, start predictions, show progress, and display results.

**Files:**
- Modify: `frontend/src/components/ProteinStructureTab.tsx`
- Modify: `frontend/src/api/client.ts` (add new API methods)
- Modify: `frontend/src/api/types/protein.ts` (add new types)

**Step 1: Add new frontend types**

In `frontend/src/api/types/protein.ts`, add:

```typescript
export type PredictionState = "not_started" | "running" | "completed" | "failed";

export interface PredictionProgress {
  pct: number;
  message: string;
}

export interface PredictionResult {
  model_name: string;
  model_version: string;
  mean_plddt: number;
  pdb_url: string;
}

export interface ProteinPredictionStatus {
  state: PredictionState;
  job_id: string | null;
  progress: PredictionProgress | null;
  prediction: PredictionResult | null;
}
```

**Step 2: Add API client methods**

In `frontend/src/api/client.ts`, add:

```typescript
proteinRecordPrediction: (objectId: string, ordinal: number) =>
  request<ProteinPredictionStatus>(
    `/objects/${objectId}/protein-records/${ordinal}/prediction`,
  ),

startProteinPrediction: (objectId: string, ordinal: number) =>
  request<JobSummary>(
    `/objects/${objectId}/protein-records/${ordinal}/predict`,
    { method: "POST" },
  ),
```

**Step 3: Rewrite PredictButton and RecordStructure**

Replace the `PredictButton` component and update `RecordStructure` in
`ProteinStructureTab.tsx`:

Key changes:
- `PredictButton` becomes a stateful component that takes `objectId`, `record`,
  and a `onPredictionComplete` callback
- On mount, it calls `proteinRecordPrediction` to check status
- It renders different UI based on the prediction state
- When clicked, it calls `startProteinPrediction` and starts polling
- Polling uses `setInterval` every 5 seconds, stops on completion/failure
- When a prediction completes, it calls `onPredictionComplete` so the parent
  can render the structure
- A pLDDT confidence legend is rendered below the iCn3D frame

```typescript
// The confidence key rendered below predicted structures
function PlddtLegend() {
  return (
    <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 4 }}>
      Confidence:{" "}
      <span style={{ color: "#0055ff" }}>██ Very high (90+)</span>{" "}
      <span style={{ color: "#66ccff" }}>██ Confident (70-90)</span>{" "}
      <span style={{ color: "#ffff00" }}>██ Low (50-70)</span>{" "}
      <span style={{ color: "#ff6600" }}>██ Very low (<50)</span>
    </div>
  );
}
```

**Step 4: Build the frontend**

```bash
cd frontend && npx tsc --noEmit
```

Expected: No type errors

**Step 5: Commit**

```bash
git add frontend/src/components/ProteinStructureTab.tsx frontend/src/api/client.ts frontend/src/api/types/protein.ts
git commit -m "feat(ui): wire PredictButton to prediction API with progress polling"
```

---

### Task 8: Register new types for exhaustion tests

**Objective:** Ensure the new `predict_structure` handler and `PREDICTION` run kind are registered in the exhaustion/coverage test lists.

**Files:**
- Modify: `backend/app/queue/node_types.py` (add to EXCLUDED_LAUNCHES if needed)
- Modify: `backend/app/services/provenance_walker.py` (add to _NO_NARRATIVE_STEP if needed)

**Step 1: Check existing registrations**

```bash
grep -n "EXCLUDED_LAUNCHES" backend/app/queue/node_types.py
grep -n "_NO_NARRATIVE_STEP" backend/app/services/provenance_walker.py
```

**Step 2: Add entries if missing**

A prediction handler is a pipeline job that produces a file, so it needs a
narrative step. If the handler doesn't produce a file that appears in the
object tree, it may need exclusion. Add as needed.

**Step 3: Commit**

```bash
git add backend/app/queue/node_types.py backend/app/services/provenance_walker.py
git commit -m "chore: register predict_structure in exhaustion test lists"
```

---

### Task 9: Verify against a real file in a worktree stack

**Objective:** Test the full prediction flow against a real protein FASTA with a running (or mocked) sidecar.

**Files:** None — verification only.

**Step 1: Start a worktree stack**

```bash
./ops/worktree-up.sh
```

Note the dynamic port from the output.

**Step 2: Ingest a protein FASTA**

Either download an NCBI assembly's protein component through the app, or add a
`.faa` file you have.

**Step 3: Verify the prediction flow**

1. Open the Structure tab for the protein FASTA
2. Select a record that has `no_reference` or `no_structure` state
3. Verify the "Predict structure" button is enabled
4. Click it — verify a job is created
5. Check the job appears in the activity view
6. Wait for completion (or check the job status endpoint)
7. Verify the predicted structure renders in iCn3D
8. Verify the pLDDT confidence legend appears
9. Click the same record again — verify it shows "View prediction" (cached)
10. Verify the prediction is cached by selecting a different record with the
    same sequence (if one exists)

**Step 4: Tear down**

```bash
./ops/worktree-up.sh --down
```

---

### Task 10: Open the PR

**Objective:** Push the branch and open a pull request.

**Step 1: Rebase onto main**

```bash
git fetch origin main
git rebase origin/main
```

**Step 2: Confirm the work survived**

```bash
git diff origin/main...HEAD --stat
```

**Step 3: Push and open PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --title "feat: predict protein structure via ESMFold sidecar when none is deposited" --body "$(cat <<'EOF'
Makes the disabled "Predict structure" button in the Structure tab functional.

Closes #533

## What it does

- Adds a `predict_structure` pipeline handler that calls an ESMFold sidecar HTTP service
- Caches predictions by sequence MD5 hash so the same sequence is never predicted twice
- Adds API endpoints: POST to start a prediction, GET to check status, GET to serve the PDB
- Updates the PredictButton to be stateful: checks status, starts jobs, polls progress, shows results
- Renders predicted structures through the existing Icn3dFrame with B-factor pLDDT coloring
- Includes a pLDDT confidence legend below the viewer

## Architecture

Prediction runs as a pipeline job (HandlerMode.SUBPROCESS) that calls an ESMFold
sidecar on the genai machine (port 21235). The sidecar loads the model once at
startup and exposes POST /predict. Results are cached in a new ProteinPrediction
collection keyed by sequence hash.

## Design decisions

- **ESMFold over AlphaFold/Boltz-1**: Fastest inference (minutes vs hours), lowest
  VRAM (~5 GB), MIT license, fits the RTX 4060 Ti comfortably
- **Sidecar over subprocess**: Persistent model loading avoids cold starts; follows
  the same pattern as the ds4 server on port 21234
- **Sequence hash over accession**: Proteins from annotation tools have no accession
  but still benefit from caching; MD5 is fast and collision-resistant
- **Pipeline job over inline HTTP**: Progress reporting, cancellation, retry, and
  activity view integration — matches every other long operation in the app

Design doc: docs/superpowers/specs/2026-08-19-protein-structure-prediction-design.md
EOF
)"
```

**Step 4: Label the PR**

```bash
gh pr edit <N> --add-label "type:feature" --add-label "area:backend" --add-label "area:frontend" --add-label "area:infrastructure"
```

**Step 5: Watch CI and merge**

```bash
gh pr checks <N> --watch
```

Once green:

```bash
gh pr merge <N> --squash --delete-branch
```

---

### Task 11: Update the TODO entry

**Objective:** Mark the TODO entry for #533 as FIXED once the PR merges.

**Files:**
- Modify: `docs/TODO.md`

Append ` — FIXED` to the #533 entry heading and add a short note under it
recording what shipped, when, and where the code lives. Then move the entry to
`docs/TODO-done.md`.

---

### Task 12: Deploy the sidecar to genai

**Objective:** Install and start the ESMFold sidecar on the genai machine.

**Step 1: Copy files to genai**

```bash
scp -r ops/esmfold-sidecar/ genai:/opt/esmfold-sidecar/
```

**Step 2: Install dependencies on genai**

```bash
ssh genai "cd /opt/esmfold-sidecar && pip install -r requirements.txt"
```

**Step 3: Create systemd service**

```ini
# /etc/systemd/system/esmfold-sidecar.service
[Unit]
Description=ESMFold structure prediction sidecar
After=network.target

[Service]
Type=simple
User=ntazetta
WorkingDirectory=/opt/esmfold-sidecar
ExecStart=/opt/esmfold-sidecar/run.sh 21235
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Step 4: Start and enable**

```bash
ssh genai "sudo systemctl daemon-reload && sudo systemctl enable --now esmfold-sidecar"
```

**Step 5: Verify**

```bash
ssh genai "curl -s http://localhost:21235/health"
```

Expected: `{"status":"ok","model_loaded":true,"model_version":"..."}`

**Step 6: Update .env on the Mac**

```
PREDICTION_SIDECAR_URL=http://192.168.1.237:21235
```

**Step 7: Restart the stack**

```bash
docker compose up -d api web worker
```
