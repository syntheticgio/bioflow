"""Launch-time validation and payload construction.

These cover the pure decisions -- what may be trimmed, which file leads a pair,
what makes two runs identical -- without a database or HTTP.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.errors import PermanentError, ValidationError
from app.models import FormatKind, ObjectStatus
from app.pipelines import tools
from app.pipelines.align_runner import ReadChemistry
from app.services import pipeline_service


class FakeObject:
    """Enough of a DataObject for the checks under test."""

    def __init__(self, name="sample_R1.fastq.gz", *, kind=FormatKind.FASTQ,
                 status=ObjectStatus.READY, metadata=None, facts=None):
        self.name = name
        self.format = type("F", (), {"kind": kind})()
        self.status = status
        self.metadata = metadata or {}
        self.facts = facts or {}
        self.id = name


class TestTrimmable:
    def test_accepts_ready_fastq(self):
        pipeline_service._check_fastq_ready(FakeObject())

    @pytest.mark.parametrize(
        "status",
        [ObjectStatus.UPLOADING, ObjectStatus.HASHING, ObjectStatus.INGESTING,
         ObjectStatus.ERROR, ObjectStatus.MISSING],
    )
    def test_rejects_a_file_that_is_not_ready(self, status):
        """Trimming a file still being written would read a partial archive."""
        with pytest.raises(ValidationError, match="not ready"):
            pipeline_service._check_fastq_ready(FakeObject(status=status))

    @pytest.mark.parametrize(
        "kind", [FormatKind.BAM, FormatKind.VCF, FormatKind.FASTA, FormatKind.BED]
    )
    def test_rejects_anything_that_is_not_fastq(self, kind):
        """fastp reads FASTQ. Handing it a BAM produces a confusing parse error
        several minutes into a job rather than an answer now."""
        with pytest.raises(ValidationError, match="not FASTQ"):
            pipeline_service._check_fastq_ready(FakeObject(kind=kind))

    def test_the_error_names_the_file(self):
        """The message ends up in a toast; 'an object is not ready' would not
        tell the user which one."""
        with pytest.raises(ValidationError, match="reads_R1"):
            pipeline_service._check_fastq_ready(
                FakeObject("reads_R1.fastq.gz", status=ObjectStatus.ERROR)
            )


class TestQcSharesTheFastqCheck:
    """QC has the same input requirement as trim, and reuses the same check.
    What differs is only the verb in the message."""

    def test_accepts_ready_fastq(self):
        pipeline_service._check_fastq_ready(FakeObject(), verb="QC")

    def test_rejects_a_bam(self):
        with pytest.raises(ValidationError, match="not FASTQ"):
            pipeline_service._check_fastq_ready(
                FakeObject(kind=FormatKind.BAM), verb="QC"
            )

    def test_the_message_names_the_operation_that_was_asked_for(self):
        """'not ready to trim' on a QC run would send the user looking for a
        trim they never started."""
        with pytest.raises(ValidationError, match="not ready to QC"):
            pipeline_service._check_fastq_ready(
                FakeObject(status=ObjectStatus.ERROR), verb="QC"
            )

    def test_defaults_to_trim_for_the_existing_callers(self):
        with pytest.raises(ValidationError, match="not ready to trim"):
            pipeline_service._check_fastq_ready(FakeObject(status=ObjectStatus.ERROR))


class TestParamsFingerprint:
    def test_identical_params_match(self):
        a = {"threads": 4, "min_length": 15}
        b = {"min_length": 15, "threads": 4}  # key order must not matter
        assert pipeline_service._params_fingerprint(
            a
        ) == pipeline_service._params_fingerprint(b)

    def test_different_params_differ(self):
        """Re-trimming with new settings is a different run, so the dedup key
        must not collapse it into the first one."""
        assert pipeline_service._params_fingerprint(
            {"min_length": 15}
        ) != pipeline_service._params_fingerprint({"min_length": 50})

    def test_is_short_enough_for_a_key(self):
        fp = pipeline_service._params_fingerprint({"threads": 4})
        assert len(fp) == 12


class TestDefaults:
    def test_threads_come_from_settings(self):
        from app.config import settings

        assert pipeline_service.default_params()["threads"] == (
            settings.pipeline_default_threads
        )

    def test_defaults_are_serializable(self):
        """They cross the wire to the launch form."""
        import json

        json.dumps(pipeline_service.default_params())

    def test_adapter_detection_is_on_by_default(self):
        """For paired reads fastp finds adapters by overlap analysis, which is
        more reliable than matching a known list."""
        assert pipeline_service.default_params()["detect_adapter_for_pe"] is True

    def test_poly_g_is_left_unset(self):
        """None means 'let fastp decide from the instrument', which is better
        than anything this application can guess."""
        assert pipeline_service.default_params()["trim_poly_g"] is None


class TestToolAwareDefaults:
    def test_fastp_is_the_default_tool(self):
        assert pipeline_service.default_params() == pipeline_service.default_params("fastp")

    def test_cutadapt_defaults_have_cutadapt_shaped_keys(self):
        params = pipeline_service.default_params("cutadapt")
        assert "quality_cutoff" in params
        assert "unqualified_percent_limit" not in params  # fastp-only key

    def test_trimmomatic_defaults_have_trimmomatic_shaped_keys(self):
        params = pipeline_service.default_params("trimmomatic")
        assert "sliding_window_size" in params
        assert "quality_cutoff" not in params  # cutadapt-only key

    def test_unknown_tool_raises(self):
        with pytest.raises(ValidationError, match="Unknown trim tool"):
            pipeline_service.default_params("not-a-real-tool")


class TestIsLongRead:
    """fastp's adapter detection and length filters are built for short
    reads; this is what TrimDialog's warning and launch_trim's advisory key
    off of. Chemistry, when QC has already inferred it, wins over platform
    -- it is the more specific fact -- but a file nobody has QC'd yet still
    needs an answer from platform alone."""

    def test_a_hifi_chemistry_fact_is_long_read(self):
        obj = FakeObject(facts={"qc_read_chemistry": ReadChemistry.HIFI.value})
        assert pipeline_service.is_long_read(obj) is True

    def test_an_ont_duplex_chemistry_fact_is_long_read(self):
        obj = FakeObject(facts={"qc_read_chemistry": ReadChemistry.ONT_DUPLEX.value})
        assert pipeline_service.is_long_read(obj) is True

    def test_a_short_chemistry_fact_is_not_long_read(self):
        """A file whose chemistry was inferred as SHORT (mislabelled length)
        is not long-read regardless of what platform it claims."""
        obj = FakeObject(
            metadata={"platform": "PacBio Sequel IIe"},
            facts={"qc_read_chemistry": ReadChemistry.SHORT.value},
        )
        assert pipeline_service.is_long_read(obj) is False

    def test_unknown_chemistry_falls_back_to_platform(self):
        obj = FakeObject(
            metadata={"platform": "Oxford Nanopore"},
            facts={"qc_read_chemistry": ReadChemistry.UNKNOWN.value},
        )
        assert pipeline_service.is_long_read(obj) is True

    def test_no_chemistry_fact_falls_back_to_platform_ont(self):
        """The common case before QC has run: only the platform is known."""
        obj = FakeObject(metadata={"platform": "PromethION"})
        assert pipeline_service.is_long_read(obj) is True

    def test_no_chemistry_fact_falls_back_to_platform_pacbio(self):
        obj = FakeObject(metadata={"platform": "PacBio Sequel IIe"})
        assert pipeline_service.is_long_read(obj) is True

    def test_illumina_is_not_long_read(self):
        obj = FakeObject(metadata={"platform": "Illumina NovaSeq"})
        assert pipeline_service.is_long_read(obj) is False

    def test_an_unannotated_file_is_not_long_read(self):
        """Absent metadata defaults to Illumina, the overwhelmingly common
        case -- the same default sam_platform itself uses."""
        assert pipeline_service.is_long_read(FakeObject()) is False

    def test_an_unrecognized_chemistry_value_falls_back_to_platform(self):
        """Facts are tool-written data, not a validated enum: a stale or
        malformed value must not crash the trim dialog."""
        obj = FakeObject(
            metadata={"platform": "Oxford Nanopore"},
            facts={"qc_read_chemistry": "not-a-real-chemistry"},
        )
        assert pipeline_service.is_long_read(obj) is True


class TestShortReadTunedTrimTools:
    """The long-read advisory in launch_trim fires only for tools whose
    *default* filters actually risk discarding long reads -- not for every
    tool is_long_read happens to be true for. fastp's min_length defaults to
    15 and Trimmomatic's to 36 (its own documented default), both tuned for
    Illumina. cutadapt's min_length defaults to 1 and its own tools.py
    summary advertises cross-platform support, so warning about it would be
    a false alarm: a user picking cutadapt specifically because it works on
    any platform should not be told it doesn't."""

    def test_fastp_is_short_read_tuned(self):
        assert "fastp" in pipeline_service._SHORT_READ_TUNED_TRIM_TOOLS

    def test_trimmomatic_is_short_read_tuned(self):
        assert "trimmomatic" in pipeline_service._SHORT_READ_TUNED_TRIM_TOOLS

    def test_cutadapt_is_not_short_read_tuned(self):
        assert "cutadapt" not in pipeline_service._SHORT_READ_TUNED_TRIM_TOOLS

    def test_every_short_read_tuned_tool_is_a_real_trim_tool(self):
        """Guards against a typo silently disabling the advisory for a tool
        that actually needs it."""
        assert pipeline_service._SHORT_READ_TUNED_TRIM_TOOLS <= set(
            pipeline_service._TRIM_PARAM_TYPES
        )


class TestVariantCallable:
    def test_accepts_a_ready_bam(self):
        pipeline_service._check_variant_callable(
            FakeObject("aligned.bam", kind=FormatKind.BAM)
        )

    @pytest.mark.parametrize(
        "kind", [FormatKind.FASTQ, FormatKind.FASTA, FormatKind.VCF, FormatKind.BED]
    )
    def test_rejects_anything_that_is_not_a_bam(self, kind):
        """Both callers read an indexed alignment. Handing bcftools a FASTQ
        produces a confusing parse error rather than an answer now."""
        with pytest.raises(ValidationError, match="not a BAM"):
            pipeline_service._check_variant_callable(FakeObject("x", kind=kind))

    @pytest.mark.parametrize(
        "status",
        [ObjectStatus.UPLOADING, ObjectStatus.HASHING, ObjectStatus.ERROR,
         ObjectStatus.MISSING],
    )
    def test_rejects_a_file_that_is_not_ready(self, status):
        with pytest.raises(ValidationError, match="not ready"):
            pipeline_service._check_variant_callable(
                FakeObject("aligned.bam", kind=FormatKind.BAM, status=status)
            )


class TestVariantDedupKey:
    """Two identical requests must collapse into one job; two genuinely
    different ones must not."""

    def _key(self, **kw):
        params = {"caller": "clair3", "threads": 4, **kw.pop("params", {})}
        return pipeline_service._variant_dedup_key(
            bam_id=kw.get("bam_id", "bam1"), params=params
        )

    def test_identical_requests_match(self):
        assert self._key() == self._key()

    def test_a_different_bam_differs(self):
        assert self._key() != self._key(bam_id="bam2")

    def test_a_different_caller_differs(self):
        """Calling the same BAM with Clair3 and with bcftools is two real
        results to compare, not a double-submit to collapse."""
        assert self._key() != self._key(params={"caller": "bcftools"})

    def test_a_different_thread_count_differs(self):
        assert self._key() != self._key(params={"threads": 8})


def _fake_dv_tool(*, available: bool, install_state=None, name="deepvariant"):
    """A Tool matching real probe shapes, not just the `available` flag in
    isolation -- `Tool.available` is derived from `path`/`error`/
    `install_state` together, and a fake that sets `available` without also
    setting the fields it is derived from can report the opposite of what it
    claims. (Caught exactly this way: an early version of this helper set
    `path` whenever `install_state` was truthy regardless of `available`,
    which made `available=False, install_state=None` -- the BUNDLED-tool-
    genuinely-missing case -- silently report as available.)
    """
    if available:
        assert install_state in (None, tools.InstallState.INSTALLED)
        return tools.Tool(
            name=name, path="/usr/bin/docker", version="1.9.0", install_state=install_state
        )
    if install_state is tools.InstallState.NOT_INSTALLED:
        # A real not-installed probe still resolves the docker client itself.
        return tools.Tool(
            name=name, path="/usr/bin/docker", version=None, install_state=install_state
        )
    # UNKNOWN (no client, or daemon unreachable) or plain missing (every
    # BUNDLED tool's genuine absence): no path, no install_state distinction
    # to offer.
    return tools.Tool(name=name, path=None, version=None, install_state=install_state)


class TestRequireOrOfferInstall:
    """The confirm-then-chain decision: refuse-with-size, install-with-
    consent, or pass straight through, depending on install_state and
    whether the caller has agreed to the download.

    async pipeline_service._require_or_offer_install directly, rather than
    through the full launch_variant_calling -- it is the one piece of new
    logic this task adds, and it needs no BAM, reference, or database to
    exercise on its own.
    """

    async def test_an_available_tool_passes_straight_through(self):
        tool = _fake_dv_tool(available=True)
        job_id = await pipeline_service._require_or_offer_install(
            tool, owner="local", install_optional=False
        )
        assert job_id is None

    async def test_not_installed_without_consent_refuses_naming_the_size(self):
        tool = _fake_dv_tool(
            available=False, install_state=tools.InstallState.NOT_INSTALLED
        )
        with pytest.raises(ValidationError) as excinfo:
            await pipeline_service._require_or_offer_install(
                tool, owner="local", install_optional=False
            )
        exc = excinfo.value
        assert exc.details["tool"] == "deepvariant"
        assert exc.details["needs"] == "install_tool"
        assert exc.details["download_bytes"] == tools.TOOL_META["deepvariant"].download_bytes
        # The size belongs in the message too -- the dialog's refusal text
        # comes straight from this, and "about X GB" is the whole point of
        # asking rather than silently downloading.
        assert "GB" in exc.message

    async def test_not_installed_with_consent_enqueues_and_returns_the_job_id(self):
        tool = _fake_dv_tool(
            available=False, install_state=tools.InstallState.NOT_INSTALLED
        )
        fake_job = type("J", (), {"id": "install-job-1"})()
        with patch(
            "app.services.tool_install_service.install",
            new=AsyncMock(return_value=fake_job),
        ) as mock_install:
            job_id = await pipeline_service._require_or_offer_install(
                tool, owner="local", install_optional=True
            )
        mock_install.assert_awaited_once_with(tool_name="deepvariant", owner="local")
        assert job_id == "install-job-1"

    async def test_unknown_install_state_still_refuses_even_with_consent(self):
        """UNKNOWN means the daemon could not be reached -- consent to a
        download cannot fix that, so this must not silently attempt an
        install that would only fail the same way _docker_client already
        does inside the handler."""
        tool = _fake_dv_tool(
            available=False, install_state=tools.InstallState.UNKNOWN
        )
        with pytest.raises(PermanentError):
            await pipeline_service._require_or_offer_install(
                tool, owner="local", install_optional=True
            )

    async def test_a_bundled_tools_plain_unavailability_still_raises_permanent(self):
        """No install_state at all (every BUNDLED tool's default) must not be
        mistaken for an offer -- consent to install a tool that has no
        install path at all would dangle."""
        tool = _fake_dv_tool(available=False, install_state=None, name="clair3")
        with pytest.raises(PermanentError):
            await pipeline_service._require_or_offer_install(
                tool, owner="local", install_optional=True
            )
