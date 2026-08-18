"""Structure prediction handlers that call the ESMFold sidecar."""
import json
import urllib.error
import urllib.request

from app.config import settings
from app.db.client import run_from_thread
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources, ProteinPrediction
from app.queue.registry import HandlerMode, JobContext, handler
from app.storage.paths import blob_path

log = get_logger(__name__)

_PREDICTION_TIMEOUT = 30 * 60  # 30 minutes max

# Valid amino acid one-letter codes (standard 20)
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
        raise PermanentError(
            f"Invalid amino acids in sequence: {''.join(sorted(invalid))}"
        )


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
        import esm  # type: ignore[import-untyped]

        return getattr(esm, "__version__", "unknown")
    except ImportError:
        return "unknown"


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

    if not object_id or ordinal is None or not sequence or not sequence_hash:
        raise PermanentError("Missing required payload fields")

    # Cast now that we've validated
    object_id = str(object_id)
    ordinal = int(ordinal)

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
            raise PermanentError(f"Sidecar rejected sequence: {body}") from e
        raise RetryableError(f"Sidecar HTTP {e.code}: {e.reason}") from e
    except (urllib.error.URLError, OSError) as e:
        raise RetryableError(f"Could not reach prediction sidecar: {e}") from e

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
    run_from_thread(
        ProteinPrediction.find_one(
            ProteinPrediction.sequence_hash == sequence_hash
        ).upsert(
            {
                "$set": {
                    "sequence_length": len(sequence),
                    "model_name": "esmfold",
                    "model_version": _get_esmfold_version(),
                    "pdb_path": str(pdb_path),
                    "mean_plddt": mean_plddt,
                    "plddt_per_residue": _parse_plddt_array(pdb_bytes),
                    "source_object_id": object_id,
                    "source_ordinal": ordinal,
                }
            },
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
