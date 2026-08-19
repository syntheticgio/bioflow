"""Provenance helpers in app.queue.results, tested as pure functions over dicts.

Salmon and featureCounts write to the same role (COUNTS) and the same file
format, so `counted_by` is the only thing on the object itself that tells
them apart -- see `salmon_provenance`'s docstring. These tests exercise that
distinction directly rather than through the applier, which needs a live DB.
"""

from app.queue import results


class TestSalmonProvenance:
    def test_records_salmon_as_the_quantifier(self):
        prov = results.salmon_provenance(
            {
                "tool_version": "1.10.2",
                "annotation_name": "cds.fna",
                "annotation_sha256": "deadbeef",
                "facts": {"genes_detected": 12},
            }
        )
        # counts_provenance writes counted_by="featurecounts"; the two paths
        # must be distinguishable on the object itself, not only by which job
        # produced it.
        assert prov["counted_by"] == "salmon"
        assert prov["salmon_version"] == "1.10.2"
        assert prov["annotation_sha256"] == "deadbeef"
        assert prov["genes_detected"] == 12

    def test_transcriptome_name_is_carried_for_the_merge_error_message(self):
        prov = results.salmon_provenance(
            {"annotation_name": "cds.fna", "annotation_sha256": "x", "facts": {}}
        )
        assert prov["annotation_name"] == "cds.fna"
