"""Which components an assembly actually offers.

Offering an annotation checkbox for an assembly that has none produces a
download that succeeds and ingests nothing, which reads as a bug. The
`--preview` JSON below is verbatim from `datasets` 18.30.1.
"""

import json

from app.metadata import ncbi_assembly_components as ac

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


class TestComponentSequenceTypes:
    def test_each_sequence_component_declares_its_type(self):
        """Known from what NCBI was asked for, so these are set rather than
        guessed from the downloaded filename."""
        assert ac.COMPONENTS["genome"].sequence_type == "Genomic"
        assert ac.COMPONENTS["protein"].sequence_type == "Protein"
        assert ac.COMPONENTS["cds"].sequence_type == "CDS"

    def test_annotation_has_no_sequence_type(self):
        """A GFF3 holds coordinates, not sequence. Tagging it would be a
        category error the dropdown then invites the user to 'correct'."""
        assert ac.COMPONENTS["gff3"].sequence_type is None

    def test_declared_types_are_valid_schema_options(self):
        from app.metadata import schemas

        options = set(schemas.all_known_fields()["sequence_type"].options)
        for spec in ac.COMPONENTS.values():
            if spec.sequence_type:
                assert spec.sequence_type in options


class TestComponentTableIntegrity:
    """The table's own invariants, which nothing on the write path checks.

    COMPONENTS is keyed by NCBI's `--include` names and so cannot be derived
    from anything this repository owns -- a key NCBI does not accept fails at
    the `datasets` command line, loudly. What can go wrong silently is the
    table disagreeing with itself, which is what these cover.
    """

    def test_component_order_covers_every_component(self):
        """The one that would fail the way STAR's missing sidecar role did.

        COMPONENT_ORDER is a hand-written tuple parallel to COMPONENTS, and
        `parse_preview`, `from_report` and `include_argument` reach components
        by iterating it rather than COMPONENTS. A component added to the dict
        and not the tuple is therefore never offered in the download dialog:
        no error, no log line, just a checkbox that does not exist.
        """
        assert set(ac.COMPONENT_ORDER) == set(ac.COMPONENTS)

    def test_each_spec_is_filed_under_its_own_key(self):
        """`ComponentSpec.key` is what gets passed to `datasets --include`, so
        a spec filed under a different key than it carries would download the
        wrong component under the right label."""
        for key, spec in ac.COMPONENTS.items():
            assert spec.key == key

    def test_file_types_are_unique(self):
        """`ncbi_assembly_handlers` builds `{spec.file_type: spec}` to label
        extracted files. A duplicate file_type does not raise there -- the
        later spec wins and the earlier component is silently unlabelable."""
        file_types = [spec.file_type for spec in ac.COMPONENTS.values()]
        assert len(set(file_types)) == len(file_types)

    def test_preview_keys_are_unique(self):
        """Same exposure as file_type, on the availability path: two
        components sharing a preview_key would read each other's file counts
        out of `datasets --preview`."""
        preview_keys = [spec.preview_key for spec in ac.COMPONENTS.values()]
        assert len(set(preview_keys)) == len(preview_keys)

    def test_every_role_is_a_real_object_role(self):
        """`ComponentSpec.role` is typed `ObjectRole`, which turns a typo here
        into an import-time error rather than a role no picker matches. What
        the untyped version cost is on record: `fix_legacy_component_roles.py`
        exists because rows roled wrong reach the aligner's reference picker
        as though a protein FASTA were a genome. This test is now a type
        assertion rather than a value-membership check -- the dataclass field
        itself is what does the enforcing.
        """
        from app.models import ObjectRole

        for spec in ac.COMPONENTS.values():
            assert isinstance(spec.role, ObjectRole)
