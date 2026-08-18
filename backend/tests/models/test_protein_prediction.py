"""ProteinPrediction document creation and query."""
from app.models.protein_prediction import ProteinPrediction


def test_create_prediction():
    """Verify ProteinPrediction can be instantiated with required fields."""
    pred = ProteinPrediction(
        sequence_hash="abc123",
        sequence_length=100,
        model_name="esmfold",
        model_version="2.0.1",
        pdb_path="/data/predictions/obj1/0.pdb",
        mean_plddt=85.5,
        plddt_per_residue=[0.9, 0.8, 0.85],
        source_object_id="507f1f77bcf86cd799439011",
        source_ordinal=0,
    )
    assert pred.sequence_hash == "abc123"
    assert pred.mean_plddt == 85.5
    assert pred.model_name == "esmfold"


def test_prediction_indexes():
    """Verify the unique index on sequence_hash exists."""
    indexes = ProteinPrediction.Settings.indexes
    assert len(indexes) == 1
    assert indexes[0].document["name"] == "uniq_sequence_hash"
