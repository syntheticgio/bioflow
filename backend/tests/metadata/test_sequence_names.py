"""Per-sequence chromosome names from NCBI's sequence_reports endpoint.

Every fixture is a real captured response. The Aspergillus one exists
specifically because two of its records share `chr_name: "11"` -- labelling by
chr_name alone would put two bars reading "11" on the strip, one of them the
largest bar there is.
"""

import json
from pathlib import Path
from unittest.mock import patch

from app.metadata import enrich, ncbi_assembly
from app.models import FormatKind

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# RefSeq accessions for GRCh38 chromosomes 1-22, X, Y and the mitochondrion,
# in the order a karyotype lists them.
_HUMAN_CHROMOSOME_ACCESSIONS = [f"NC_{n:06d}.{v}" for n, v in
    [(1,11),(2,12),(3,12),(4,12),(5,10),(6,12),(7,14),(8,11),(9,12),(10,11),
     (11,10),(12,12),(13,11),(14,9),(15,10),(16,10),(17,11),(18,10),(19,10),
     (20,11),(21,9),(22,11),(23,11),(24,10)]] + ["NC_012920.1"]


YEAST = "ncbi_seqreports_GCF_000146045.2.json"
ASPERGILLUS = "ncbi_seqreports_GCF_000002445.2.json"
HUMAN = "ncbi_seqreports_GCF_000001405.40_slice.json"
# The real GRCh38 record distribution: every assembled molecule, plus the
# chr1 scaffold/patch/alt family that starves the label budget. The _slice
# fixture above is seven records and cannot reproduce that.
HUMAN_FULL = "ncbi_seqreports_GCF_000001405.40_human.json"


class TestParseSequenceReports:
    def test_maps_both_accession_namespaces_to_one_label(self):
        """One lookup must label the GCA file and the GCF file alike."""
        labels = ncbi_assembly.parse_sequence_reports(_load(YEAST))
        assert labels["NC_001133.9"] == "I"
        assert labels["BK006935.2"] == "I"

    def test_labels_the_mitochondrion(self):
        labels = ncbi_assembly.parse_sequence_reports(_load(YEAST))
        assert labels["NC_001224.1"] == "MT"

    def test_distinguishes_records_sharing_a_chr_name(self):
        """The regression a naive chr_name implementation produces.

        Both of these report chr_name "11"; they are different scaffolds and
        must not both read "11".
        """
        labels = ncbi_assembly.parse_sequence_reports(_load(ASPERGILLUS))
        assert labels["NT_165288.1"] == "chr11-scaffold01"
        assert labels["NT_165287.1"] == "chr11-scaffold02"
        assert labels["NT_165288.1"] != labels["NT_165287.1"]

    def test_assembled_molecules_still_use_chr_name(self):
        labels = ncbi_assembly.parse_sequence_reports(_load(ASPERGILLUS))
        assert labels["NC_008409.1"] == "1"
        assert labels["NC_005063.2"] == "2"

    def test_human_unlocalized_scaffolds_get_their_own_names(self):
        labels = ncbi_assembly.parse_sequence_reports(_load(HUMAN))
        assert labels["NC_000001.11"] == "1"
        assert labels["NT_187361.1"] == "HSCHR1_CTG1_UNLOCALIZED"
        assert labels["NC_012920.1"] == "MT"

    def test_every_human_chromosome_is_labelled(self):
        """The #836 regression.

        `sort_order` is the chromosome number, not a global rank: all 44 chr1
        scaffolds, patches and alts report `sort_order: 1`. Sorting on it alone
        spends the whole label budget inside chromosome 1, and chromosomes 2-22,
        X and Y fall through to the accession-digit fallback -- which renders
        NC_000023.11 as "23" and NC_000024.10 as "24".
        """
        labels = ncbi_assembly.parse_sequence_reports(_load(HUMAN_FULL))
        expected = [str(n) for n in range(1, 23)] + ["X", "Y", "MT"]
        assert [labels.get(a) for a in _HUMAN_CHROMOSOME_ACCESSIONS] == expected

    def test_assembled_molecules_outrank_scaffolds_for_the_budget(self):
        """A chromosome must never lose its label to a patch of another one."""
        payload = {
            "reports": [
                {
                    "chr_name": "1",
                    "sequence_name": f"HSCHR1_CTG{i}",
                    "role": "unlocalized-scaffold",
                    "sort_order": 1,
                    "refseq_accession": f"NT_{i:06d}.1",
                }
                for i in range(80)
            ]
            + [
                {
                    "chr_name": "X",
                    "role": "assembled-molecule",
                    "sort_order": 23,
                    "refseq_accession": "NC_000023.11",
                }
            ]
        }
        labels = ncbi_assembly.parse_sequence_reports(payload)
        assert labels["NC_000023.11"] == "X"

    def test_caps_the_map(self):
        """Bounded like sequence_lengths: the strip draws 24 bars and lists the
        rest, so labels past the cap have nothing to label.

        The cap counts records, not entries. Each record here carries both
        accessions, so the map is twice the record budget.
        """
        reports = [
            {
                "chr_name": str(i),
                "refseq_accession": f"NC_{i:06d}.1",
                "genbank_accession": f"CM{i:06d}.1",
                "role": "assembled-molecule",
                "sort_order": i,
            }
            for i in range(400)
        ]
        labels = ncbi_assembly.parse_sequence_reports({"reports": reports})
        assert len(labels) == ncbi_assembly.MAX_STORED_LABELS * 2

    def test_labels_every_chromosome_before_any_scaffold(self):
        """The GRCh38.p14 regression: chromosomes 2-Y went unlabelled.

        NCBI's sort_order interleaves each chromosome with the scaffolds
        unlocalized to it, so a budget spent in file order is exhausted inside
        chromosome 1 -- leaving the strip to fall back to accession digits
        ("664", "665") for every chromosome after it.
        """
        reports = []
        for chrom in range(1, 24):
            reports.append(
                {
                    "chr_name": str(chrom),
                    "refseq_accession": f"NC_{chrom:06d}.11",
                    "genbank_accession": f"CM{chrom:06d}.2",
                    "role": "assembled-molecule",
                    "sort_order": chrom,
                }
            )
            # The interleaving that caused the bug: 40 scaffolds per chromosome
            # sitting between it and the next one.
            for s in range(40):
                reports.append(
                    {
                        "chr_name": str(chrom),
                        "sequence_name": f"HSCHR{chrom}_CTG{s}_UNLOCALIZED",
                        "genbank_accession": f"KI{chrom:03d}{s:03d}.1",
                        "role": "unlocalized-scaffold",
                        "sort_order": chrom,
                    }
                )

        labels = ncbi_assembly.parse_sequence_reports({"reports": reports})

        for chrom in range(1, 24):
            assert labels.get(f"CM{chrom:06d}.2") == str(chrom)
            assert labels.get(f"NC_{chrom:06d}.11") == str(chrom)

    def test_a_record_with_no_accession_does_not_spend_the_budget(self):
        """It labels nothing, so it must not displace a record that does."""
        reports = [{"chr_name": "junk", "role": "assembled-molecule"}] * 400
        reports.append(
            {
                "chr_name": "1",
                "genbank_accession": "CM000663.2",
                "role": "assembled-molecule",
            }
        )
        labels = ncbi_assembly.parse_sequence_reports({"reports": reports})
        assert labels["CM000663.2"] == "1"

    def test_survives_malformed_payloads(self):
        """Same never-raises contract as parse_report."""
        assert ncbi_assembly.parse_sequence_reports({}) == {}
        assert ncbi_assembly.parse_sequence_reports({"reports": None}) == {}
        assert ncbi_assembly.parse_sequence_reports({"reports": "nope"}) == {}
        assert ncbi_assembly.parse_sequence_reports({"reports": [None, 3, "x"]}) == {}
        # A record with no usable accession contributes nothing but must not raise.
        assert ncbi_assembly.parse_sequence_reports({"reports": [{"chr_name": "I"}]}) == {}
        # A record with an accession but no name has nothing to say either.
        assert (
            ncbi_assembly.parse_sequence_reports(
                {"reports": [{"refseq_accession": "NC_1.1"}]}
            )
            == {}
        )


class TestLookupSequenceNames:
    def test_returns_labels_from_a_real_payload(self):
        body = (FIXTURES / YEAST).read_bytes()
        with patch("app.metadata.ncbi_assembly._get", return_value=body) as get:
            labels = ncbi_assembly.lookup_sequence_names("GCF_000146045.2")
        assert labels["NC_001133.9"] == "I"
        assert "sequence_reports" in get.call_args[0][0]

    def test_rejects_a_malformed_accession_without_calling_out(self):
        with patch("app.metadata.ncbi_assembly._get") as get:
            assert ncbi_assembly.lookup_sequence_names("not-an-accession") is None
        get.assert_not_called()

    def test_returns_none_when_the_request_fails(self):
        with patch("app.metadata.ncbi_assembly._get", return_value=None):
            assert ncbi_assembly.lookup_sequence_names("GCF_000146045.2") is None

    def test_returns_none_on_unparseable_json(self):
        with patch("app.metadata.ncbi_assembly._get", return_value=b"<html>nope"):
            assert ncbi_assembly.lookup_sequence_names("GCF_000146045.2") is None

    def test_returns_none_rather_than_an_empty_map(self):
        """An empty map and a failed lookup are the same to the caller, and
        None keeps a meaningless `sequence_labels: {}` out of facts."""
        with patch("app.metadata.ncbi_assembly._get", return_value=b'{"reports": []}'):
            assert ncbi_assembly.lookup_sequence_names("GCF_000146045.2") is None

    def test_never_raises_when_the_helper_explodes(self):
        with patch("app.metadata.ncbi_assembly._get", side_effect=RuntimeError("boom")):
            assert ncbi_assembly.lookup_sequence_names("GCF_000146045.2") is None


class TestEnrichmentStoresLabels:
    def _meta(self):
        return ncbi_assembly.AssemblyMetadata(
            accession="GCF_000146045.2", assembly_name="R64"
        )

    def test_labels_and_roles_land_in_facts(self):
        """Both facts come from one response: `sequence_labels` stays written
        for readers that predate `sequence_roles`."""
        roles = {"NC_001133.9": {"label": "I", "core": True, "order": 0}}
        with (
            patch("app.metadata.ncbi_assembly.lookup", return_value=self._meta()),
            patch("app.metadata.ncbi_assembly.lookup_sequence_roles", return_value=roles),
        ):
            result = enrich.enrich_from_assembly(
                filename="GCF_000146045.2_R64_genomic.fna",
                existing_metadata={},
                format_kind=FormatKind.FASTA,
            )
        assert result.facts["sequence_roles"] == roles
        assert result.facts["sequence_labels"] == {"NC_001133.9": "I"}

    def test_a_failed_name_lookup_leaves_the_rest_intact(self):
        """The names are a bonus. Losing them must not cost the stats that the
        assembly lookup already succeeded in fetching."""
        with (
            patch("app.metadata.ncbi_assembly.lookup", return_value=self._meta()),
            patch("app.metadata.ncbi_assembly.lookup_sequence_roles", return_value=None),
        ):
            result = enrich.enrich_from_assembly(
                filename="GCF_000146045.2_R64_genomic.fna",
                existing_metadata={},
                format_kind=FormatKind.FASTA,
            )
        assert "sequence_labels" not in result.facts
        assert "sequence_roles" not in result.facts
        assert result.facts["ncbi_assembly_name"] == "R64"

    def test_a_raising_name_lookup_does_not_break_ingest(self):
        with (
            patch("app.metadata.ncbi_assembly.lookup", return_value=self._meta()),
            patch(
                "app.metadata.ncbi_assembly.lookup_sequence_roles",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = enrich.enrich_from_assembly(
                filename="GCF_000146045.2_R64_genomic.fna",
                existing_metadata={},
                format_kind=FormatKind.FASTA,
            )
        assert "sequence_labels" not in result.facts
        assert "sequence_roles" not in result.facts
        assert result.facts["ncbi_assembly_name"] == "R64"


class TestParseSequenceRoles:
    """The richer per-sequence map behind "show only the core chromosomes".

    `parse_sequence_reports` answers "what is this sequence called"; this
    answers "is this sequence one of the chromosomes". The strip needs both:
    the first to caption a bar, the second to decide whether to draw one.
    """

    def test_marks_human_chromosomes_as_assembled_molecules(self):
        roles = ncbi_assembly.parse_sequence_roles(_load(HUMAN_FULL))
        core = [a for a in _HUMAN_CHROMOSOME_ACCESSIONS if roles.get(a, {}).get("core")]
        assert core == _HUMAN_CHROMOSOME_ACCESSIONS

    def test_excludes_scaffolds_patches_and_alts(self):
        roles = ncbi_assembly.parse_sequence_roles(_load(HUMAN_FULL))
        assert roles["NT_187361.1"]["core"] is False
        assert sum(1 for v in roles.values() if v["core"]) > 0
        # Every non-core record is still present -- the overflow list needs them.
        assert len(roles) > sum(1 for v in roles.values() if v["core"])

    def test_keeps_the_assembly_ordering(self):
        """Bars are drawn in karyotype order (1..22, X, Y, MT), not by length:
        chr11 is longer than chr10 and would otherwise swap places."""
        roles = ncbi_assembly.parse_sequence_roles(_load(HUMAN_FULL))
        core = sorted(
            (v["order"], a) for a, v in roles.items() if v["core"] and a.startswith("NC_")
        )
        assert [a for _, a in core] == _HUMAN_CHROMOSOME_ACCESSIONS[:24] + [
            "NC_012920.1"
        ]

    def test_yeast_is_entirely_core(self):
        """A complete small assembly has nothing to hide."""
        roles = ncbi_assembly.parse_sequence_roles(_load(YEAST))
        assert all(v["core"] for v in roles.values())

    def test_carries_the_label_alongside_the_role(self):
        roles = ncbi_assembly.parse_sequence_roles(_load(YEAST))
        assert roles["NC_001133.9"]["label"] == "I"
        assert roles["BK006935.2"]["label"] == "I"

    def test_caps_assembled_molecules(self):
        reports = [
            {
                "chr_name": str(i),
                "refseq_accession": f"NC_{i:06d}.1",
                "role": "assembled-molecule",
                "sort_order": i,
            }
            for i in range(500)
        ]
        roles = ncbi_assembly.parse_sequence_roles({"reports": reports})
        core = sum(1 for v in roles.values() if v["core"])
        assert core <= ncbi_assembly.MAX_ASSEMBLED_MOLECULES

    def test_survives_malformed_payloads(self):
        assert ncbi_assembly.parse_sequence_roles({}) == {}
        assert ncbi_assembly.parse_sequence_roles({"reports": None}) == {}
        assert ncbi_assembly.parse_sequence_roles({"reports": [None, 3, "x"]}) == {}
        assert ncbi_assembly.parse_sequence_roles({"reports": [{"chr_name": "I"}]}) == {}
