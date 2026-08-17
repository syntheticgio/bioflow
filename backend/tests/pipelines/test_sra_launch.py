"""SRA download launch rules.

The validation and payload construction, without a database or HTTP -- the
same split as `test_launch_rules.py` for the pipeline launches.
"""

import pytest
from app.metadata.sra_resolver import RunInfo
from app.services import sra_service


class TestCleanAccessions:
    def test_normalizes_case_and_whitespace(self):
        assert sra_service._clean_accessions([" srr1 ", "SRR2"]) == ["SRR1", "SRR2"]

    def test_deduplicates(self):
        """The queue would reject the second copy as an in-flight duplicate and
        report it as "already downloading", which is a confusing way to say
        "you listed it twice"."""
        assert sra_service._clean_accessions(["SRR1", "srr1", "SRR2"]) == ["SRR1", "SRR2"]

    def test_preserves_order(self):
        """The label names the first accession, so order has to be stable."""
        assert sra_service._clean_accessions(["SRR3", "SRR1", "SRR2"]) == [
            "SRR3",
            "SRR1",
            "SRR2",
        ]

    def test_drops_blanks(self):
        assert sra_service._clean_accessions(["", "  ", "SRR1"]) == ["SRR1"]

    def test_handles_no_input(self):
        assert sra_service._clean_accessions([]) == []
        assert sra_service._clean_accessions(None) == []


class TestDownloadLabel:
    def test_a_single_run_names_it(self):
        assert sra_service._download_label(["SRR1"]) == "Download SRR1 from SRA"

    def test_a_batch_reports_the_count_and_an_example(self):
        """Stored rather than derived, so the run stays describable after its
        jobs are TTL-pruned."""
        label = sra_service._download_label(["SRR1", "SRR2", "SRR3"])
        assert "3 runs" in label
        assert "SRR1" in label


class TestIngestMetadata:
    def test_maps_onto_the_same_vocabulary_as_ingest_enrichment(self):
        """A downloaded file must be annotated identically to an uploaded one
        that was enriched at ingest, or the two stop being findable by the
        same search."""
        run = RunInfo(
            accession="SRR11768093",
            experiment="SRX8321150",
            study="SRP261086",
            bioproject="PRJNA631678",
            biosample="SAMN14886310",
            organism="Trypanosoma brucei brucei",
            platform="ILLUMINA",
            instrument="NextSeq 550",
            library_strategy="ChIP-Seq",
            library_layout="PAIRED",
        )
        meta = sra_service._ingest_metadata(run)

        assert meta["sra_run"] == "SRR11768093"
        assert meta["bioproject"] == "PRJNA631678"
        assert meta["organism"] == "Trypanosoma brucei brucei"
        # The instrument model, not the platform family: that is what the
        # schema's `platform` field holds everywhere else.
        assert meta["platform"] == "NextSeq 550"
        assert meta["read_type"] == "paired-end"
        assert meta["assay"] == "ChIP-seq"

    def test_library_source_and_molecule_type_flow_through_ingest(self):
        run = RunInfo(
            accession="SRR11768093",
            library_strategy="ChIP-Seq",
            library_source="GENOMIC",
        )
        meta = sra_service._ingest_metadata(run)
        assert meta["library_source"] == "Genomic"
        assert meta["molecule_type"] == "DNA"

    def test_single_end_layout_maps_through(self):
        meta = sra_service._ingest_metadata(
            RunInfo(accession="SRR1", library_layout="SINGLE")
        )
        assert meta["read_type"] == "single-end"

    def test_a_sparse_record_yields_only_what_is_known(self):
        """Absent fields are omitted rather than written as None, which would
        put empty values into searchable metadata."""
        meta = sra_service._ingest_metadata(RunInfo(accession="SRR1"))
        assert meta == {"sra_run": "SRR1"}

    def test_sample_attributes_are_carried_across(self):
        meta = sra_service._ingest_metadata(
            RunInfo(accession="SRR1", sample_attributes={"strain": "Lister 427"})
        )
        assert meta.get("strain") == "Lister 427"


class TestSelectionLimit:
    def test_the_limit_is_stated_rather_than_implicit(self):
        """The dialog mirrors this number, so it has to be readable from here."""
        assert sra_service.MAX_RUNS_PER_REQUEST == 100


@pytest.mark.anyio
class TestLaunchValidation:
    """The guards that must fire before anything is enqueued."""

    async def test_an_empty_selection_is_rejected(self, monkeypatch):
        from app.errors import ValidationError

        monkeypatch.setattr(sra_service.tools, "require", lambda t: t)
        with pytest.raises(ValidationError, match="No runs selected"):
            await sra_service.launch_download(
                project_id=None, run_accessions=[], owner="o"
            )

    async def test_a_non_run_accession_is_rejected_with_guidance(self, monkeypatch):
        """Only runs are downloadable. A user who pastes a study accession
        needs to be told to resolve it first, not handed a failed job."""
        from app.errors import ValidationError

        monkeypatch.setattr(sra_service.tools, "require", lambda t: t)
        with pytest.raises(ValidationError, match="Only runs"):
            await sra_service.launch_download(
                project_id="x", run_accessions=["SRP261086"], owner="o"
            )

    async def test_too_many_runs_is_refused_before_queueing_any(self, monkeypatch):
        """One click should not be able to queue a multi-terabyte request;
        past the limit it is far likelier a select-all misclick than intent."""
        from app.errors import ValidationError

        monkeypatch.setattr(sra_service.tools, "require", lambda t: t)
        too_many = [f"SRR{i}" for i in range(sra_service.MAX_RUNS_PER_REQUEST + 1)]
        with pytest.raises(ValidationError, match="Too many runs"):
            await sra_service.launch_download(
                project_id="x", run_accessions=too_many, owner="o"
            )

    async def test_the_selection_is_checked_before_the_database(self, monkeypatch):
        """These validations need no round trip, and answering "no runs
        selected" should not depend on one that tells us nothing."""
        from app.errors import ValidationError
        from app.services import project_service

        monkeypatch.setattr(sra_service.tools, "require", lambda t: t)

        # Patched at `project_service.get_project` rather than `Project.get`:
        # the owner-scoped lookup is what the service calls now, and patching
        # the model underneath it would leave this test passing while the
        # round trip it forbids happened one layer up.
        async def explode(*a, **kw):
            raise AssertionError("should not have queried for the project")

        monkeypatch.setattr(project_service, "get_project", explode)
        with pytest.raises(ValidationError):
            await sra_service.launch_download(
                project_id="x", run_accessions=["  "], owner="o"
            )

    async def test_a_missing_project_is_a_not_found(self, monkeypatch):
        from app.errors import NotFoundError
        from app.services import project_service

        monkeypatch.setattr(sra_service.tools, "require", lambda t: t)

        # `get_project` raises for a missing project *and* for one owned by
        # another profile -- the same NotFoundError, deliberately. This test
        # covers both cases at once, which is the point of that design.
        async def _not_found(*a, **kw):
            raise NotFoundError("Project not found")

        monkeypatch.setattr(project_service, "get_project", _not_found)

        with pytest.raises(NotFoundError):
            await sra_service.launch_download(
                project_id="x", run_accessions=["SRR1"], owner="o"
            )

