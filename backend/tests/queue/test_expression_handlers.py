"""The expression job handlers at the seam: quantify and differential_expression.

The runners underneath are pure functions over dicts and are tested as such
(test_counts_runner.py, test_de_runner.py). This file exercises the handlers
themselves -- payload validation, blob resolution, and -- for
`differential_expression` -- that the N-input agreement checks actually run
when the handler fans in N counts files.

The refusals are the substance. `differential_expression` is the first
handler whose correctness depends on its inputs agreeing with each other, and
per CLAUDE.md the tests assert the direction that fails when the seam breaks:
mismatched inputs rejected with the named reason. A test that only fed
matching samples would pass whether or not `merge_counts` was wired in.

`run_deseq2` is not exercised -- it is a thin call into PyDESeq2, and a test
that mocked the library would only assert that the mock was called (see
test_de_runner.py's docstring). It is replaced at the handler seam so the
handler's own wiring -- payload -> SampleCounts -> design -> result dict -- is
what is under test.
"""

from pathlib import Path

import pytest

from app.errors import PermanentError, ValidationError
from app.pipelines import tools
from app.queue import expression_handlers
from app.queue.registry import JobContext

# A minimal featureCounts table: `#` comment line, Geneid header, then rows.
# parse_counts skips lines starting with `#` or matching ^Geneid\t.
_COUNTS_TSV = (
    "# program : featureCounts\n"
    "Geneid\tChr\tStart\tEnd\tStrand\tLength\taligned.bam\n"
    "g1\tchrI\t1\t100\t+\t100\t10\n"
    "g2\tchrI\t200\t300\t+\t100\t20\n"
)

_SUMMARY_TSV = (
    "Status\tgroup\n"
    "Assigned\t30\n"
    "Unassigned_NoFeatures\t5\n"
    "Unassigned_Ambiguity\t0\n"
    "Unassigned_MultiMapping\t0\n"
)


class _FakeDEResult:
    """Stand-in for de_runner.DEResult at the run_deseq2 seam."""

    facts = {"genes_tested": 2, "significant_genes": 1}

    def to_tsv(self) -> str:
        return "gene\tbase_mean\tlog2_fold_change\nYAL068C\t42.5\t2.18\n"


def _ctx(payload: dict) -> JobContext:
    return JobContext(job_id="job-1", payload=payload, epoch=1, attempts=1, owner="local")


def _fake_tool(name: str, version: str) -> tools.Tool:
    return tools.Tool(name=name, path=f"/usr/bin/{name}", version=version)


def _counts_entry(tmp_path, name, counts, *, condition="control", annotation=None) -> dict:
    """A differential_expression payload entry: a real counts file plus the
    fields the handler copies into SampleCounts."""
    path = tmp_path / f"{name}.counts.tsv"
    rows = "".join(f"{g}\tchrI\t1\t100\t+\t100\t{c}\n" for g, c in counts.items())
    path.write_text("Geneid\tChr\tStart\tEnd\tStrand\tLength\tcount\n" + rows)
    entry = {
        "sample": name,
        "condition": condition,
        "counts_path": str(path),
        "counts_object_id": f"id-{name}",
    }
    if annotation is not None:
        entry["annotation_sha256"] = annotation
    return entry


@pytest.fixture
def featurecounts_available(monkeypatch):
    """Pin the featureCounts probe so require() passes deterministically,
    whether or not the binary exists in this image."""
    fake = _fake_tool("featurecounts", "2.0.6")
    monkeypatch.setattr(expression_handlers.tools, "featurecounts", lambda: fake)
    return fake


@pytest.fixture
def pydeseq2_available(monkeypatch):
    fake = _fake_tool("pydeseq2", "0.4.12")
    monkeypatch.setattr(expression_handlers.tools, "pydeseq2", lambda: fake)
    return fake


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Send tmp/ and logs/ under the test's own directory, so a handler that
    reaches _prepare_workdir cannot write to the host's /data. logs_dir and
    tmp_dir are derived read-only properties; patch what they derive from."""
    monkeypatch.setattr(expression_handlers.settings, "bioinfo_home", tmp_path)
    return tmp_path


class TestQuantifyValidation:
    """The refusals before anything runs: a payload that cannot be counted."""

    def test_missing_object_id_is_permanent(self, featurecounts_available):
        with pytest.raises(PermanentError, match="object_id"):
            expression_handlers.quantify(_ctx({}))

    def test_missing_bam_blob_is_permanent(self, featurecounts_available, home):
        with pytest.raises(PermanentError, match="bam"):
            expression_handlers.quantify(_ctx({"object_id": "obj-1"}))

    def test_missing_annotation_blob_is_permanent(self, featurecounts_available, home, tmp_path):
        bam = tmp_path / "reads.bam"
        bam.write_bytes(b"not-a-real-bam")
        with pytest.raises(PermanentError, match="annotation"):
            expression_handlers.quantify(_ctx({"object_id": "obj-1", "bam_path": str(bam)}))

    def test_a_bad_strandedness_is_refused_by_name(self, featurecounts_available, home, tmp_path):
        bam = tmp_path / "reads.bam"
        bam.write_bytes(b"x")
        gtf = tmp_path / "ann.gtf"
        gtf.write_text("")
        with pytest.raises(ValidationError, match="strandedness"):
            expression_handlers.quantify(
                _ctx(
                    {
                        "object_id": "obj-1",
                        "bam_path": str(bam),
                        "annotation_path": str(gtf),
                        "params": {"strandedness": 5},
                    }
                )
            )


class TestQuantifyRun:
    """What happens once featureCounts itself has run (or not)."""

    def test_reported_success_without_a_counts_file_is_permanent(
        self, featurecounts_available, home, tmp_path, monkeypatch
    ):
        bam = tmp_path / "reads.bam"
        bam.write_bytes(b"x")
        gtf = tmp_path / "ann.gtf"
        gtf.write_text("")
        monkeypatch.setattr(expression_handlers, "run_subprocess", lambda *a, **k: 0)
        with pytest.raises(PermanentError, match="wrote no counts file"):
            expression_handlers.quantify(
                _ctx({"object_id": "obj-1", "bam_path": str(bam), "annotation_path": str(gtf)})
            )

    def test_a_nonzero_exit_is_classified_permanent(
        self, featurecounts_available, home, tmp_path, monkeypatch
    ):
        bam = tmp_path / "reads.bam"
        bam.write_bytes(b"x")
        gtf = tmp_path / "ann.gtf"
        gtf.write_text("")
        monkeypatch.setattr(expression_handlers, "run_subprocess", lambda *a, **k: 1)
        with pytest.raises(PermanentError, match="featureCounts exited 1"):
            expression_handlers.quantify(
                _ctx({"object_id": "obj-1", "bam_path": str(bam), "annotation_path": str(gtf)})
            )

    def test_success_returns_the_counts_and_its_provenance(
        self, featurecounts_available, home, tmp_path, monkeypatch
    ):
        bam = tmp_path / "reads.bam"
        bam.write_bytes(b"x")
        gtf = tmp_path / "ann.gtf"
        gtf.write_text("")

        def fake_run(ctx, cmd, **kw):
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_text(_COUNTS_TSV)
            Path(str(out) + ".summary").write_text(_SUMMARY_TSV)
            return 0

        monkeypatch.setattr(expression_handlers, "run_subprocess", fake_run)
        result = expression_handlers.quantify(
            _ctx(
                {
                    "object_id": "obj-1",
                    "bam_path": str(bam),
                    "annotation_path": str(gtf),
                    "annotation_object_id": "gtf-1",
                    "annotation_sha256": "sha-ann",
                    "project_id": "proj-1",
                    "params": {"strandedness": 1, "paired": True},
                }
            )
        )

        # The provenance the merge in differential_expression depends on:
        # which annotation, by name and digest, this sample was counted against.
        assert result["object_id"] == "obj-1"
        assert result["annotation_object_id"] == "gtf-1"
        assert result["annotation_sha256"] == "sha-ann"
        assert result["annotation_name"] == "annotation.gtf"
        assert result["project_id"] == "proj-1"
        assert result["tool_version"] == "2.0.6"
        assert result["output"]["name"] == "aligned.counts.tsv"
        assert result["params"]["strandedness"] == 1
        assert result["params"]["paired"] is True
        assert result["facts"]["assigned_pct"] == 85.71
        assert result["facts"]["genes_detected"] == 2

    def test_a_near_zero_assignment_rate_is_recorded_not_fatal(
        self, featurecounts_available, home, tmp_path, monkeypatch
    ):
        """The handler's job is to surface the two silent failures of this
        pipeline -- wrong strandedness, mismatched annotation -- without
        failing the run, because a genuinely empty sample is real and should
        still produce a file."""
        bam = tmp_path / "reads.bam"
        bam.write_bytes(b"x")
        gtf = tmp_path / "ann.gtf"
        gtf.write_text("")

        def fake_run(ctx, cmd, **kw):
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_text(_COUNTS_TSV)
            Path(str(out) + ".summary").write_text(
                "Status\tgroup\nAssigned\t1\nUnassigned_NoFeatures\t99\n"
            )
            return 0

        monkeypatch.setattr(expression_handlers, "run_subprocess", fake_run)
        result = expression_handlers.quantify(
            _ctx({"object_id": "obj-1", "bam_path": str(bam), "annotation_path": str(gtf)})
        )
        assert result["facts"]["assigned_pct"] == 1.0
        assert result["facts"]["low_assignment_warning"] is True


class TestDifferentialExpressionValidation:
    def test_no_samples_is_permanent(self, pydeseq2_available):
        with pytest.raises(PermanentError, match="samples"):
            expression_handlers.differential_expression(_ctx({}))

    def test_a_contrast_missing_an_arm_is_permanent(self, pydeseq2_available):
        with pytest.raises(PermanentError, match="contrast"):
            expression_handlers.differential_expression(
                _ctx({"samples": [{"counts_path": "x"}], "contrast": {"test": "treated"}})
            )


class TestDifferentialExpressionAgreement:
    """The N-input checks, at the handler. The counts files are real files
    under tmp_path: the seam under test is payload -> file -> SampleCounts ->
    merge, and the refusal is the direction that fails if the agreement
    checks stop running."""

    def test_different_annotation_digests_are_refused_with_the_groups_named(
        self, pydeseq2_available, home, tmp_path
    ):
        payload = {
            "samples": [
                _counts_entry(
                    tmp_path, "a", {"g1": 1, "g2": 2}, condition="control", annotation="ann1"
                ),
                _counts_entry(
                    tmp_path, "b", {"g1": 3, "g2": 4}, condition="treated", annotation="ann2"
                ),
            ],
            "contrast": {"test": "treated", "reference": "control"},
        }
        with pytest.raises(ValidationError) as exc:
            expression_handlers.differential_expression(_ctx(payload))
        message = str(exc.value)
        assert "different annotations" in message
        assert "a" in message and "b" in message

    def test_disagreeing_gene_sets_are_refused_with_both_samples_named(
        self, pydeseq2_available, home, tmp_path
    ):
        payload = {
            "samples": [
                _counts_entry(
                    tmp_path, "a", {"g1": 1, "g2": 2}, condition="control", annotation="ann1"
                ),
                _counts_entry(
                    tmp_path, "b", {"g1": 3, "g3": 4}, condition="treated", annotation="ann1"
                ),
            ],
            "contrast": {"test": "treated", "reference": "control"},
        }
        with pytest.raises(ValidationError) as exc:
            expression_handlers.differential_expression(_ctx(payload))
        message = str(exc.value)
        assert "same genes" in message
        # Names both samples: "re-quantify these two" is the fix.
        assert "'a'" in message and "'b'" in message

    def test_an_empty_counts_file_is_refused_by_name(self, pydeseq2_available, home, tmp_path):
        payload = {
            "samples": [
                _counts_entry(tmp_path, "a", {}, condition="control", annotation="ann1"),
            ],
            "contrast": {"test": "treated", "reference": "control"},
        }
        with pytest.raises(ValidationError, match="no counts"):
            expression_handlers.differential_expression(_ctx(payload))


class TestDifferentialExpressionSuccess:
    def test_matching_samples_merge_into_the_contrast(
        self, pydeseq2_available, home, tmp_path, monkeypatch
    ):
        payload = {
            "samples": [
                _counts_entry(
                    tmp_path, "a", {"g1": 1, "g2": 2}, condition="control", annotation="ann1"
                ),
                _counts_entry(
                    tmp_path, "b", {"g1": 3, "g2": 4}, condition="treated", annotation="ann1"
                ),
            ],
            "contrast": {"test": "treated", "reference": "control"},
            "threads": 2,
        }

        seen = {}

        def fake_run_deseq2(matrix, *, test, reference, threads, on_phase=None):
            seen["matrix"] = matrix
            seen["test"] = test
            seen["reference"] = reference
            seen["threads"] = threads
            return _FakeDEResult()

        monkeypatch.setattr(expression_handlers.de_runner, "run_deseq2", fake_run_deseq2)
        result = expression_handlers.differential_expression(_ctx(payload))

        # The matrix the merge built, in the order the handler fed it.
        assert seen["test"] == "treated"
        assert seen["reference"] == "control"
        assert seen["threads"] == 2
        assert seen["matrix"].genes == ["g1", "g2"]
        assert seen["matrix"].samples == ["a", "b"]
        assert seen["matrix"].values == [[1, 2], [3, 4]]

        # The result the applier stores: contrast, per-sample design, and the
        # sample ids whose counts fed the test.
        assert result["params"]["contrast"] == {"test": "treated", "reference": "control"}
        assert result["params"]["design"] == {"a": "control", "b": "treated"}
        assert result["counts_object_ids"] == ["id-a", "id-b"]
        assert result["tool_version"] == "0.4.12"
        assert result["output"]["name"] == "de_treated_vs_control.tsv"


class TestSalmonQuantifyResultContract:
    """The salmon_quantify handler's contract with the results applier.

    The handler runs in a worker thread and cannot touch the database, so its
    entire output is the dict it returns. These tests pin the keys that dict
    must carry -- particularly annotation_sha256, which is what lets
    de_runner.merge_counts refuse a design mixing incompatible samples.
    """

    def test_transcriptome_digest_is_carried_as_annotation_sha256(self):
        # pipeline_service reads this key out of facts when it builds a DE
        # design, and merge_counts refuses samples whose values differ. For
        # Salmon the digest is the transcriptome's -- there is no annotation.
        # This also, correctly, refuses a matrix mixing Salmon and
        # featureCounts samples: their digests can never match, and they do
        # not describe the same gene universe.
        result = expression_handlers._salmon_result_dict(
            object_id="64b" + "0" * 21,
            transcriptome_object_id="64c" + "0" * 21,
            project_id="64d" + "0" * 21,
            job_id="64e" + "0" * 21,
            output_path="/tmp/x/SRR1.counts.tsv",
            tool_version="1.10.2",
            transcriptome_name="cds.fna",
            transcriptome_sha256="deadbeef",
            facts={"genes_detected": 12},
            workdir="/tmp/x",
        )
        assert result["annotation_sha256"] == "deadbeef"
        assert result["annotation_name"] == "cds.fna"
        assert result["facts"]["quantified_by"] == "salmon"

    def test_output_carries_the_path_and_name_the_applier_needs(self):
        result = expression_handlers._salmon_result_dict(
            object_id="64b" + "0" * 21,
            transcriptome_object_id="64c" + "0" * 21,
            project_id="64d" + "0" * 21,
            job_id="64e" + "0" * 21,
            output_path="/tmp/x/SRR1.counts.tsv",
            tool_version="1.10.2",
            transcriptome_name="cds.fna",
            transcriptome_sha256="deadbeef",
            facts={},
            workdir="/tmp/x",
        )
        assert result["output"]["name"] == "SRR1.counts.tsv"
        assert result["output"]["tmp_path"] == "/tmp/x/SRR1.counts.tsv"


class TestSalmonQuantifyMateDetection:
    """Paired-end detection at the handler, not just the result-dict builder.

    salmon_quantify must decide whether a sample is paired the same way this
    codebase's other handlers do: a second file addressed by
    mate_sha256/mate_path (see assembly_handlers.payload_has_mate and
    align_handlers._resolve_digest_or_path's docstring), not by a key
    ('reads2_blob_id') nothing in this codebase ever sets. These tests run
    the real handler end to end (subprocess mocked) so a regression back to
    the dead key fails a test instead of only failing silently at runtime.
    """

    @pytest.fixture
    def salmon_available(self, monkeypatch):
        fake = _fake_tool("salmon", "1.10.2")
        monkeypatch.setattr(expression_handlers.tools, "salmon", lambda: fake)
        return fake

    def _fake_run_subprocess(self, calls):
        def fake_run(ctx, cmd, **kw):
            calls.append(cmd)
            if cmd[1] == "index":
                Path(cmd[cmd.index("-i") + 1]).mkdir(parents=True, exist_ok=True)
            else:
                out_dir = Path(cmd[cmd.index("-o") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "quant.sf").write_text(
                    "Name\tLength\tEffectiveLength\tTPM\tNumReads\n"
                    "t1\t100\t80\t1.0\t10.0\n"
                )
            return 0

        return fake_run

    def test_single_end_run_passes_one_reads_file_to_salmon(
        self, salmon_available, home, tmp_path, monkeypatch
    ):
        transcriptome = tmp_path / "cds.fna"
        transcriptome.write_text(">t1 [gene=g1]\nACGT\n")
        reads = tmp_path / "reads_1.fastq.gz"
        reads.write_bytes(b"x")

        calls: list[list[str]] = []
        monkeypatch.setattr(
            expression_handlers, "run_subprocess", self._fake_run_subprocess(calls)
        )

        expression_handlers.salmon_quantify(
            expression_handlers.JobContext(
                job_id="job-1",
                payload={
                    "object_id": "obj-1",
                    "transcriptome_path": str(transcriptome),
                    "reads_path": str(reads),
                },
                epoch=1,
                attempts=1,
                owner="local",
            )
        )

        quant_call = next(c for c in calls if c[1] == "quant")
        assert "-r" in quant_call
        assert "-1" not in quant_call
        assert "-2" not in quant_call

    def test_a_mate_file_is_detected_by_mate_sha256_and_paired_into_the_run(
        self, salmon_available, home, tmp_path, monkeypatch
    ):
        transcriptome = tmp_path / "cds.fna"
        transcriptome.write_text(">t1 [gene=g1]\nACGT\n")
        reads = tmp_path / "reads_1.fastq.gz"
        reads.write_bytes(b"x")
        mate = tmp_path / "reads_2.fastq.gz"
        mate.write_bytes(b"y")

        calls: list[list[str]] = []
        monkeypatch.setattr(
            expression_handlers, "run_subprocess", self._fake_run_subprocess(calls)
        )

        expression_handlers.salmon_quantify(
            expression_handlers.JobContext(
                job_id="job-2",
                payload={
                    "object_id": "obj-1",
                    "transcriptome_path": str(transcriptome),
                    "reads_path": str(reads),
                    "mate_path": str(mate),
                },
                epoch=1,
                attempts=1,
                owner="local",
            )
        )

        quant_call = next(c for c in calls if c[1] == "quant")
        assert "-1" in quant_call
        assert "-2" in quant_call
        assert "-r" not in quant_call
        # Both files actually got symlinked into the workdir under the mate's
        # own reads_2.fastq.gz name, not silently dropped.
        r1 = Path(quant_call[quant_call.index("-1") + 1])
        r2 = Path(quant_call[quant_call.index("-2") + 1])
        assert r1.resolve() == reads.resolve()
        assert r2.resolve() == mate.resolve()

    def test_a_mate_by_sha256_alone_is_also_detected(
        self, salmon_available, home, tmp_path, monkeypatch
    ):
        """mate_sha256 (digest-addressed blob), not just mate_path, must
        trigger the paired branch -- payload_has_mate's own condition is an
        `or` over both keys."""
        transcriptome = tmp_path / "cds.fna"
        transcriptome.write_text(">t1 [gene=g1]\nACGT\n")
        reads = tmp_path / "reads_1.fastq.gz"
        reads.write_bytes(b"x")

        blob_dir = tmp_path / "objects" / "de"
        blob_dir.mkdir(parents=True)
        digest = "de" + "0" * 62
        blob_file = blob_dir / digest
        blob_file.write_bytes(b"y")

        import app.queue.align_handlers as align_handlers

        monkeypatch.setattr(align_handlers, "blob_path", lambda d: blob_file)

        calls: list[list[str]] = []
        monkeypatch.setattr(
            expression_handlers, "run_subprocess", self._fake_run_subprocess(calls)
        )

        expression_handlers.salmon_quantify(
            expression_handlers.JobContext(
                job_id="job-3",
                payload={
                    "object_id": "obj-1",
                    "transcriptome_path": str(transcriptome),
                    "reads_path": str(reads),
                    "mate_sha256": digest,
                },
                epoch=1,
                attempts=1,
                owner="local",
            )
        )

        quant_call = next(c for c in calls if c[1] == "quant")
        assert "-1" in quant_call
        assert "-2" in quant_call


def test_transcript_assembly_result_dict_carries_counts_and_output(tmp_path):
    """The dict `results._apply_transcript_assembly` consumes.

    Asserted as a unit rather than through a real subprocess because the
    handler's contract with results.py is the part that breaks silently: a
    renamed key here fails nothing until a real job produces an object with
    no facts on it.
    """
    from app.queue import expression_handlers

    out_gtf = tmp_path / "sample.transcripts.gtf"
    out_gtf.write_text(
        '# StringTie version 2.2.1\n'
        'chr1\tStringTie\ttranscript\t101\t500\t1000\t+\t.\t'
        'gene_id "STRG.1"; transcript_id "STRG.1.1"; reference_id "T1";\n'
        'chr1\tStringTie\ttranscript\t1201\t1700\t1000\t+\t.\t'
        'gene_id "STRG.2"; transcript_id "STRG.2.1";\n'
    )

    result = expression_handlers._transcript_assembly_result_dict(
        object_id="64b7f0000000000000000001",
        job_id="64b7f0000000000000000002",
        out_gtf=out_gtf,
        name="sample.transcripts.gtf",
    )

    assert result["assembled_by"] == "stringtie"
    assert result["transcript_count"] == 2
    assert result["novel_transcript_count"] == 1
    assert result["gene_count"] == 2
    assert result["output"]["name"] == "sample.transcripts.gtf"
    assert result["output"]["tmp_path"] == str(out_gtf)
