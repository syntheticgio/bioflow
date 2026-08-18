"""ProteinPrediction document creation and query."""
import pytest
from pydantic import ValidationError

from app.models.protein_prediction import ProteinPrediction
from app.queue.prediction_handlers import _parse_mean_plddt, _parse_plddt_array


def test_create_prediction(beanie_models):
    """Verify ProteinPrediction can be instantiated with required fields."""
    pred = ProteinPrediction(
        sequence_hash="abc123",
        sequence_length=100,
        model_name="esmfold",
        model_version="2.0.1",
        pdb_path="/data/predictions/obj1/0.pdb",
        mean_plddt=0.855,
        plddt_per_residue=[0.9, 0.8, 0.85],
        source_object_id="507f1f77bcf86cd799439011",
        source_ordinal=0,
    )
    assert pred.sequence_hash == "abc123"
    assert pred.mean_plddt == 0.855
    assert pred.model_name == "esmfold"


def test_prediction_indexes():
    """Verify the unique index on sequence_hash exists."""
    indexes = ProteinPrediction.Settings.indexes
    assert len(indexes) == 1
    assert indexes[0].document["name"] == "uniq_sequence_hash"


def test_mean_plddt_rejects_the_0_100_scale(beanie_models):
    """A raw 0-100 pLDDT must not validate — it would render as 8550% in the UI.

    The whole stack stores this normalised: `_parse_mean_plddt` divides the PDB
    B-factor column by 100, and ProteinStructureTab multiplies by 100 to display.
    This pins the bound so it cannot be widened back to `le=100`.
    """
    with pytest.raises(ValidationError):
        ProteinPrediction(
            sequence_hash="abc123",
            sequence_length=100,
            model_name="esmfold",
            model_version="2.0.1",
            pdb_path="/data/predictions/obj1/0.pdb",
            mean_plddt=85.5,
            source_object_id="507f1f77bcf86cd799439011",
            source_ordinal=0,
        )


def test_parser_emits_the_scale_the_model_accepts(beanie_models):
    """The sidecar parser's output must satisfy the field constraint.

    These two live in different modules with nothing tying them together; a
    change to either divisor breaks persistence of every real prediction.
    """
    pdb = b"\n".join(
        b"ATOM  %5d  CA  ALA A%4d      0.000   0.000   0.000  1.00 %5.2f           C"
        % (i, i, bfactor)
        for i, bfactor in enumerate([92.5, 88.0, 71.25], start=1)
    )

    mean = _parse_mean_plddt(pdb)

    assert mean == pytest.approx(0.8391666, rel=1e-6)
    pred = ProteinPrediction(
        sequence_hash="abc123",
        sequence_length=3,
        model_name="esmfold",
        model_version="2.0.1",
        pdb_path="/data/predictions/obj1/0.pdb",
        mean_plddt=mean,
        plddt_per_residue=_parse_plddt_array(pdb),
        source_object_id="507f1f77bcf86cd799439011",
        source_ordinal=0,
    )
    assert pred.mean_plddt == pytest.approx(0.8391666, rel=1e-6)
    assert pred.plddt_per_residue == pytest.approx([0.925, 0.88, 0.7125])
