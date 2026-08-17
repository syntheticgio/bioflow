"""Reading tool and version out of the open `facts` vocabulary.

Table-driven against the real key names the `*_provenance` builders in
`queue/results.py` write, not invented ones -- the point is that this keeps
working when a builder is added.
"""

import pytest

from app.services.provenance_walker import extract_tool_facts


@pytest.mark.parametrize(
    "facts,expected_tool,expected_version",
    [
        # align_provenance
        (
            {"aligned_by": "bwa-mem2", "aligner_version": "2.2.1"},
            "bwa-mem2",
            "2.2.1",
        ),
        # trim provenance
        (
            {"trimmed_by": "fastp", "trim_tool_version": "0.23.4"},
            "fastp",
            "0.23.4",
        ),
        # variant_provenance
        (
            {"variants_called_by": "clair3", "variant_caller_version": "1.0.4"},
            "clair3",
            "1.0.4",
        ),
        # assembly_provenance
        (
            {"assembled_by": "flye", "assembler_version": "2.9.3"},
            "flye",
            "2.9.3",
        ),
        # counts_provenance
        (
            {"counted_by": "featurecounts", "featurecounts_version": "2.0.6"},
            "featurecounts",
            "2.0.6",
        ),
        # A future builder nobody updated this module for: the tool is still
        # found by convention, and the version is simply absent.
        ({"polished_by": "racon"}, "racon", None),
        # Tool recorded, version missing -- the common probe-failure case.
        ({"aligned_by": "bwa-mem2"}, "bwa-mem2", None),
        # Nothing at all.
        ({}, None, None),
    ],
)
def test_extract_tool_facts(facts, expected_tool, expected_version):
    tool, version = extract_tool_facts(facts)
    assert tool == expected_tool
    assert version == expected_version


def test_ignores_non_string_by_values():
    """`sra_fields_applied` is a list, not a tool name."""
    tool, _ = extract_tool_facts({"sra_fields_applied": ["a", "b"]})
    assert tool is None


def test_params_are_found_by_convention():
    from app.services.provenance_walker import extract_params

    assert extract_params({"align_params": {"threads": 8}}) == {"threads": 8}
    assert extract_params({"trim_params": {"q": 20}}) == {"q": 20}
    assert extract_params({}) == {}


def test_ignores_downstream_writeback_facts():
    """A raw-reads object that a later trim ran against, not one it produced.

    Found via Task 10's real-data walk: `_apply_trim_reads` in
    `queue/results.py` writes `trimmed_by`/`trim_tool_version`/`trim_params`
    onto its *input* objects as well as its outputs ("the report goes on
    both inputs"), and a root object downloaded from the SRA can carry that
    triple despite having no trim step of its own. `trim_outputs` is what
    marks the triple as belonging to the downstream job rather than this
    object's own origin.
    """
    facts = {
        "sra_downloaded_from": "DRR1066343",
        "sra_download_source": "ncbi",
        "trimmed_by": "trimmomatic",
        "trim_tool_version": "0.39",
        "trim_params": {"min_length": 36},
        "trim_outputs": ["665f1a2b3c4d5e6f7a8b9c0d", "665f1a2b3c4d5e6f7a8b9c0e"],
    }
    tool, version = extract_tool_facts(facts)
    assert tool is None
    assert version is None

    from app.services.provenance_walker import extract_params

    assert extract_params(facts) == {}


def test_writeback_facts_do_not_hide_this_objects_own_tool():
    """A real trim *output* still surfaces its own tool normally.

    The output side of the same job carries no `trim_outputs` key (only the
    input side does), so its own `trimmed_by`/`trim_tool_version` must still
    be read -- the fix must not blind the walker to legitimate trim steps.
    """
    facts = {"trimmed_by": "fastp", "trim_tool_version": "0.23.4"}
    tool, version = extract_tool_facts(facts)
    assert tool == "fastp"
    assert version == "0.23.4"

    from app.services.provenance_walker import extract_params

    assert extract_params({"trim_params": {"q": 20}}) == {"q": 20}


def test_qc_writeback_does_not_hide_a_real_align_step():
    """`align_provenance` copies `qc_read_chemistry` from the reads onto the
    BAM it produces, so a real alignment step's own facts sit beside a QC
    writeback key on the very same object -- the exclusion must not blind
    extraction to `aligned_by`/`aligner_version` just because `qc_tool` (a
    QC writeback, from a different, earlier job) is also present.
    """
    facts = {
        "aligned_by": "bwa-mem2",
        "aligner_version": "2.3",
        "qc_read_chemistry": "short",
    }
    tool, version = extract_tool_facts(facts)
    assert tool == "bwa-mem2"
    assert version == "2.3"


def test_ignores_qc_writeback_version_on_an_unrelated_step():
    """`qc_tool_version` must never surface as another job's own version.

    Also found in the Task 10 real-data walk, distinct from the trim case:
    `_apply_run_qc` merges `qc_tool`/`qc_tool_version` onto whatever object
    QC ran against, but never as that object's own `produced_by_job` (QC
    creates no object of its own -- see `_apply_run_qc`'s docstring). So a
    raw-reads object downloaded from the SRA and later QC'd carried
    `qc_tool_version` with no accompanying `qc_by`/`sra_downloaded_by` key,
    and the orphaned version leaked through as if it were the download
    step's version (rendered as "downloaded from the SRA 0.24.0" against a
    real object before this fix).
    """
    facts = {
        "sra_downloaded_from": "DRR1066343",
        "sra_download_source": "ncbi",
        "qc_status": "ok",
        "qc_tool": "fastp",
        "qc_tool_version": "0.24.0",
    }
    tool, version = extract_tool_facts(facts)
    assert tool is None
    assert version is None


def test_writeback_exclusion_does_not_hide_an_unrelated_step():
    """Excluding the trim writeback must not blind extraction to a real,
    different step's own facts recorded on the same object.
    """
    facts = {
        "aligned_by": "bwa-mem2",
        "aligner_version": "2.3",
        "trimmed_by": "trimmomatic",
        "trim_tool_version": "0.39",
        "trim_params": {"min_length": 36},
        "trim_outputs": ["665f1a2b3c4d5e6f7a8b9c0d"],
    }
    tool, version = extract_tool_facts(facts)
    assert tool == "bwa-mem2"
    assert version == "2.3"
