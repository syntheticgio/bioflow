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
