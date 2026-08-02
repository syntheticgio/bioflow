"""Resolving an accession to its runs.

The XML fixtures are the same real NCBI records `test_sra.py` uses --
SRR11768093 (Illumina ChIP-Seq, paired) and SRR000001 (454 WGS, single) -- so
the parser is exercised against genuine variation rather than something shaped
to match the code.

No test here touches the network. The resolution functions take parsed XML or
have their fetch stubbed; the live path is verified separately.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app.metadata import sra_resolver

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def chipseq_package() -> ET.Element:
    root = ET.fromstring((FIXTURES / "sra_SRR11768093.xml").read_text())
    return root.find(".//EXPERIMENT_PACKAGE") or root


@pytest.fixture
def wgs_package() -> ET.Element:
    root = ET.fromstring((FIXTURES / "sra_SRR000001.xml").read_text())
    return root.find(".//EXPERIMENT_PACKAGE") or root


class TestClassify:
    @pytest.mark.parametrize(
        "accession,kind",
        [
            ("SRR11768093", "run"),
            ("ERR949836", "run"),
            ("DRR000001", "run"),
            ("SRX8321150", "experiment"),
            ("SRS6640466", "sample"),
            ("SRP261086", "study"),
            ("PRJNA631678", "bioproject"),
            ("PRJEB12345", "bioproject"),
            ("PRJDB4176", "bioproject"),
            ("SAMN14886310", "biosample"),
            ("SAMEA3231268", "biosample"),
            ("SAMD00000001", "biosample"),
        ],
    )
    def test_recognizes_every_namespace(self, accession, kind):
        """All six archives' prefixes, across NCBI, EBI and DDBJ. A user pastes
        whichever their paper cited."""
        assert sra_resolver.classify(accession) == kind

    def test_is_case_insensitive(self):
        assert sra_resolver.classify("prjna631678") == "bioproject"

    @pytest.mark.parametrize("junk", ["", "NOTREAL", "12345"])
    def test_rejects_what_it_does_not_know(self, junk):
        assert sra_resolver.classify(junk) is None


class TestIsResolvable:
    @pytest.mark.parametrize(
        "accession",
        ["SRR11768093", "PRJNA631678", "SAMN14886310", "SAMEA3231268", "SAMD00000001"],
    )
    def test_accepts_well_formed_accessions(self, accession):
        assert sra_resolver.is_resolvable(accession)

    def test_accepts_the_ebi_letter_after_the_stem(self):
        """SAMEA/SAMED carry an archive letter between the prefix and the
        digits. A digits-only pattern rejected every EBI BioSample."""
        assert sra_resolver.is_resolvable("SAMEA3231268")

    @pytest.mark.parametrize("junk", ["", "SRR", "PRJNA", "NOTREAL123", "SRR12"])
    def test_rejects_malformed_input_without_a_network_call(self, junk):
        """Checked before the request so a typo answers immediately rather
        than after a round trip to NCBI."""
        assert not sra_resolver.is_resolvable(junk)


class TestRunsFromPackage:
    def test_extracts_the_run_and_its_measurements(self, chipseq_package):
        runs = sra_resolver.runs_from_package(chipseq_package)
        assert len(runs) == 1
        run = runs[0]
        assert run.accession == "SRR11768093"
        assert run.spots == 6993562
        assert run.bases == 1055723708

    def test_size_comes_from_the_archive_not_an_estimate(self, chipseq_package):
        """The RUN element carries NCBI's own byte count. Deriving one from
        `bases` would be wrong by a compression factor that varies per run --
        and this figure gates the download's disk pre-flight."""
        run = sra_resolver.runs_from_package(chipseq_package)[0]
        assert run.bytes == 447172246
        assert run.bytes != run.bases

    def test_carries_the_experiment_context_onto_the_run(self, chipseq_package):
        run = sra_resolver.runs_from_package(chipseq_package)[0]
        assert run.experiment == "SRX8321150"
        assert run.study == "SRP261086"
        assert run.bioproject == "PRJNA631678"
        assert run.biosample == "SAMN14886310"
        assert run.organism == "Trypanosoma brucei brucei"

    def test_platform_is_the_tag_name(self, chipseq_package):
        """SRA encodes the platform as an element name, not an attribute, and
        this exact string is what the QC dispatch keys on."""
        run = sra_resolver.runs_from_package(chipseq_package)[0]
        assert run.platform == "ILLUMINA"
        assert run.instrument == "NextSeq 550"

    def test_captures_the_library_description(self, chipseq_package):
        run = sra_resolver.runs_from_package(chipseq_package)[0]
        assert run.library_strategy == "ChIP-Seq"
        assert run.library_layout == "PAIRED"

    def test_a_different_platform_parses_too(self, wgs_package):
        """SRR000001 is a 454 record, so the platform handling is not tuned to
        the Illumina fixture. Its instrument naming differs as well."""
        run = sra_resolver.runs_from_package(wgs_package)[0]
        assert run.accession == "SRR000001"
        assert run.platform == "LS454"
        assert run.platform != "ILLUMINA"

    def test_single_end_layout_is_read_as_such(self):
        """Both fixtures happen to be paired, so the SINGLE branch needs its
        own case -- it is what decides whether a download yields one file or
        two."""
        package = ET.fromstring(
            "<EXPERIMENT_PACKAGE><LIBRARY_DESCRIPTOR><LIBRARY_LAYOUT>"
            "<SINGLE/></LIBRARY_LAYOUT></LIBRARY_DESCRIPTOR>"
            '<RUN accession="SRR1"/></EXPERIMENT_PACKAGE>'
        )
        assert sra_resolver.runs_from_package(package)[0].library_layout == "SINGLE"

    def test_collects_sample_attributes(self, chipseq_package):
        run = sra_resolver.runs_from_package(chipseq_package)[0]
        assert run.sample_attributes
        assert any("strain" in k.lower() for k in run.sample_attributes)

    def test_a_package_with_no_runs_yields_nothing(self):
        """A record that resolved but holds no downloadable run. Empty, not an
        exception: it is a legitimate state for a registered-but-unreleased
        study."""
        package = ET.fromstring("<EXPERIMENT_PACKAGE><EXPERIMENT/></EXPERIMENT_PACKAGE>")
        assert sra_resolver.runs_from_package(package) == []

    def test_a_run_without_an_accession_is_skipped(self):
        """Nothing can be downloaded without one, so listing it would offer the
        user a row that cannot work."""
        package = ET.fromstring('<EXPERIMENT_PACKAGE><RUN total_spots="5"/></EXPERIMENT_PACKAGE>')
        assert sra_resolver.runs_from_package(package) == []

    def test_missing_fields_degrade_to_none_rather_than_raising(self):
        """A run NCBI recorded sparsely is still downloadable. Dropping it for
        a missing attribute would be worse than showing it incompletely."""
        package = ET.fromstring('<EXPERIMENT_PACKAGE><RUN accession="SRR1"/></EXPERIMENT_PACKAGE>')
        run = sra_resolver.runs_from_package(package)[0]
        assert run.accession == "SRR1"
        assert run.platform is None
        assert run.bytes is None
        assert run.library_layout is None

    def test_unparseable_numbers_become_none(self):
        """Rather than propagating a string into a field the size arithmetic
        and the disk pre-flight both treat as a number."""
        package = ET.fromstring(
            '<EXPERIMENT_PACKAGE><RUN accession="SRR1" total_spots="lots" size=""/>'
            "</EXPERIMENT_PACKAGE>"
        )
        run = sra_resolver.runs_from_package(package)[0]
        assert run.spots is None
        assert run.bytes is None

    def test_every_run_in_a_multi_run_experiment_is_returned(self):
        """One library sequenced across lanes. Each run is separately
        downloadable, so each is its own row."""
        package = ET.fromstring(
            "<EXPERIMENT_PACKAGE>"
            '<EXPERIMENT accession="SRX1"/>'
            '<RUN accession="SRR1" total_spots="10"/>'
            '<RUN accession="SRR2" total_spots="20"/>'
            "</EXPERIMENT_PACKAGE>"
        )
        runs = sra_resolver.runs_from_package(package)
        assert [r.accession for r in runs] == ["SRR1", "SRR2"]
        # The shared context is copied onto both, not just the first.
        assert all(r.experiment == "SRX1" for r in runs)


class TestBuildHierarchy:
    def test_groups_runs_by_sample(self):
        """Sample, not experiment: it is the biologically meaningful level a
        user recognizes, where an experiment accession is a prep detail."""
        runs = [
            sra_resolver.RunInfo(accession="SRR1", biosample="SAMN1", bases=100),
            sra_resolver.RunInfo(accession="SRR2", biosample="SAMN1", bases=200),
            sra_resolver.RunInfo(accession="SRR3", biosample="SAMN2", bases=50),
        ]
        nodes = sra_resolver.build_hierarchy(runs)
        assert [n.accession for n in nodes] == ["SAMN1", "SAMN2"]
        assert nodes[0].child_count == 2
        assert nodes[0].total_bases == 300

    def test_falls_back_when_no_biosample_is_recorded(self):
        runs = [sra_resolver.RunInfo(accession="SRR1", sample="SRS1")]
        assert sra_resolver.build_hierarchy(runs)[0].accession == "SRS1"

    def test_a_run_with_no_parent_at_all_still_appears(self):
        """Grouped under itself rather than dropped -- it is still selectable."""
        runs = [sra_resolver.RunInfo(accession="SRR1")]
        assert sra_resolver.build_hierarchy(runs)[0].accession == "SRR1"

    def test_total_bases_is_none_when_nothing_reported_any(self):
        """Rather than 0, which would read as an empty run."""
        runs = [sra_resolver.RunInfo(accession="SRR1", biosample="SAMN1")]
        assert sra_resolver.build_hierarchy(runs)[0].total_bases is None


class TestResolve:
    """The orchestration, with the network stubbed."""

    def test_a_malformed_accession_never_reaches_the_network(self, monkeypatch):
        def explode(*a, **k):
            raise AssertionError("should not have called NCBI")

        monkeypatch.setattr(sra_resolver, "search_uids", explode)
        result = sra_resolver.resolve("NOTREAL")
        assert result.error and "not a recognized accession" in result.error
        assert result.runs == []

    def test_no_results_reports_an_error_rather_than_an_empty_success(
        self, monkeypatch
    ):
        monkeypatch.setattr(sra_resolver, "search_uids", lambda a, **k: ([], 0))
        result = sra_resolver.resolve("SRR99999999")
        assert result.error and "No sequencing runs found" in result.error

    def test_a_run_accession_narrows_to_that_run(self, monkeypatch, chipseq_package):
        """An experiment package holds every sibling run. Asking about one run
        should not offer to download its siblings."""
        extra = ET.fromstring(
            '<EXPERIMENT_PACKAGE><RUN accession="SRR_OTHER"/></EXPERIMENT_PACKAGE>'
        )
        monkeypatch.setattr(sra_resolver, "search_uids", lambda a, **k: (["1"], 1))
        monkeypatch.setattr(
            sra_resolver, "fetch_packages", lambda uids: [chipseq_package, extra]
        )
        result = sra_resolver.resolve("SRR11768093")
        assert [r.accession for r in result.runs] == ["SRR11768093"]

    def test_an_experiment_accession_keeps_every_run(self, monkeypatch, chipseq_package):
        monkeypatch.setattr(sra_resolver, "search_uids", lambda a, **k: (["1"], 1))
        monkeypatch.setattr(sra_resolver, "fetch_packages", lambda uids: [chipseq_package])
        result = sra_resolver.resolve("SRX8321150")
        assert result.kind == "experiment"
        assert result.total_run_count == 1

    def test_platform_filter_excludes_and_explains(self, monkeypatch, chipseq_package):
        """An empty table with no explanation would read as a broken lookup."""
        monkeypatch.setattr(sra_resolver, "search_uids", lambda a, **k: (["1"], 1))
        monkeypatch.setattr(sra_resolver, "fetch_packages", lambda uids: [chipseq_package])
        result = sra_resolver.resolve("SRX8321150", platform_filter="OXFORD_NANOPORE")
        assert result.runs == []
        assert result.error and "ILLUMINA" in result.error

    def test_platform_filter_keeps_a_match(self, monkeypatch, chipseq_package):
        monkeypatch.setattr(sra_resolver, "search_uids", lambda a, **k: (["1"], 1))
        monkeypatch.setattr(sra_resolver, "fetch_packages", lambda uids: [chipseq_package])
        result = sra_resolver.resolve("SRX8321150", platform_filter="ILLUMINA")
        assert result.total_run_count == 1
        assert result.error is None

    def test_total_bytes_sums_the_selected_runs(self, monkeypatch, chipseq_package):
        monkeypatch.setattr(sra_resolver, "search_uids", lambda a, **k: (["1"], 1))
        monkeypatch.setattr(sra_resolver, "fetch_packages", lambda uids: [chipseq_package])
        result = sra_resolver.resolve("SRX8321150")
        assert result.total_bytes_estimate == 447172246

    def test_truncation_is_reported_when_the_study_exceeds_the_ceiling(
        self, monkeypatch, chipseq_package
    ):
        """Showing a truncated list as though it were complete would let a user
        believe they had selected a whole study."""
        monkeypatch.setattr(sra_resolver, "search_uids", lambda a, **k: (["1"], 5000))
        monkeypatch.setattr(sra_resolver, "fetch_packages", lambda uids: [chipseq_package])
        assert sra_resolver.resolve("SRP261086").truncated is True

    def test_a_package_that_fails_to_parse_does_not_lose_the_others(
        self, monkeypatch, chipseq_package
    ):
        """250 of 288 runs is a usable answer; nothing at all is not."""
        class Exploding:
            def findall(self, *a, **k):
                raise ValueError("bad package")

            def find(self, *a, **k):
                raise ValueError("bad package")

        monkeypatch.setattr(sra_resolver, "search_uids", lambda a, **k: (["1", "2"], 2))
        monkeypatch.setattr(
            sra_resolver, "fetch_packages", lambda uids: [Exploding(), chipseq_package]
        )
        result = sra_resolver.resolve("SRP261086")
        assert [r.accession for r in result.runs] == ["SRR11768093"]

    def test_metadata_that_returns_nothing_usable_is_an_error_not_a_blank(
        self, monkeypatch
    ):
        monkeypatch.setattr(sra_resolver, "search_uids", lambda a, **k: (["1"], 1))
        monkeypatch.setattr(sra_resolver, "fetch_packages", lambda uids: [])
        result = sra_resolver.resolve("SRP261086")
        assert result.error and "no usable metadata" in result.error


class TestSerialization:
    def test_a_resolution_round_trips_through_its_dict(self, chipseq_package):
        """The Redis cache stores the dict form, so a round trip that lost a
        field would make a cache hit differ from a miss."""
        runs = sra_resolver.runs_from_package(chipseq_package)
        original = sra_resolver.SraResolution(
            accession="SRX8321150",
            kind="experiment",
            title="a title",
            organism="Trypanosoma brucei brucei",
            hierarchy=sra_resolver.build_hierarchy(runs),
            runs=runs,
            total_run_count=len(runs),
            total_bytes_estimate=447172246,
        )
        restored = sra_resolver._from_dict(original.as_dict())

        assert restored.accession == original.accession
        assert restored.total_bytes_estimate == original.total_bytes_estimate
        assert [r.accession for r in restored.runs] == [r.accession for r in original.runs]
        assert restored.runs[0].platform == "ILLUMINA"
        assert restored.runs[0].bytes == 447172246
        assert [h.accession for h in restored.hierarchy] == [
            h.accession for h in original.hierarchy
        ]


class TestAssemblyClassification:
    def test_refseq_accession_classifies_as_assembly(self):
        assert sra_resolver.classify("GCF_000002445.2") == "assembly"

    def test_genbank_accession_classifies_as_assembly(self):
        assert sra_resolver.classify("GCA_000001405.29") == "assembly"

    def test_lowercase_is_accepted(self):
        """Users paste from papers and spreadsheets; case is not signal."""
        assert sra_resolver.classify("gcf_000002445.2") == "assembly"

    def test_an_assembly_accession_is_resolvable(self):
        assert sra_resolver.is_resolvable("GCF_000002445.2") is True

    def test_an_unversioned_assembly_is_not_resolvable(self):
        """NCBI assembly accessions always carry a version. Rejecting it here
        gives an immediate answer instead of a round trip that finds nothing."""
        assert sra_resolver.is_resolvable("GCF_000002445") is False

    def test_resolve_does_not_send_an_assembly_to_esearch(self, monkeypatch):
        """db=sra&term=GCF_... finds nothing, so the user would be told "no
        sequencing runs found" -- true, and actively misleading."""
        called = []
        monkeypatch.setattr(
            sra_resolver, "search_uids",
            lambda *a, **k: called.append(a) or ([], 0),
        )
        result = sra_resolver.resolve("GCF_000002445.2")
        assert called == []
        assert result.kind == "assembly"
        assert result.error is not None
        assert "assembly" in result.error.lower()


class TestSearchRunsByOrganism:
    """Paginated search, unlike `search_uids`'s single capped shot.

    `sra._get` is stubbed directly rather than `search_uids`, since this
    exercises the real esearch call shape (retstart/retmax) that a paginated
    organism search needs and an accession resolution never does.
    """

    def test_parses_uids_and_total_count(self, monkeypatch):
        import json

        monkeypatch.setattr(
            sra_resolver.sra,
            "_get",
            lambda url: json.dumps(
                {"esearchresult": {"idlist": ["1", "2", "3"], "count": "500"}}
            ),
        )
        uids, total = sra_resolver.search_runs_by_organism("Homo sapiens")
        assert uids == ["1", "2", "3"]
        assert total == 500

    def test_retstart_and_retmax_reach_the_request(self, monkeypatch):
        seen = {}

        def fake_get(url):
            seen["url"] = url
            return '{"esearchresult": {"idlist": [], "count": "0"}}'

        monkeypatch.setattr(sra_resolver.sra, "_get", fake_get)
        sra_resolver.search_runs_by_organism("Mus musculus", retstart=40, retmax=20)

        assert "retstart=40" in seen["url"]
        assert "retmax=20" in seen["url"]
        assert "term=Mus" in seen["url"]
        assert "%5BOrganism%5D" in seen["url"] or "[Organism]" in seen["url"]

    def test_network_failure_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(sra_resolver.sra, "_get", lambda url: None)
        uids, total = sra_resolver.search_runs_by_organism("Homo sapiens")
        assert uids == []
        assert total == 0

    def test_unparseable_body_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(sra_resolver.sra, "_get", lambda url: "not json")
        uids, total = sra_resolver.search_runs_by_organism("Homo sapiens")
        assert uids == []
        assert total == 0
