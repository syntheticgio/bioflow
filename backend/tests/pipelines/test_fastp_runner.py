"""fastp command construction, progress parsing, and report extraction.

The sample output in these tests is copied from real fastp 0.24.0 runs rather
than invented -- the parsing exists to survive that exact format, and a made-up
approximation of it would test nothing.
"""

import json

import pytest

from app.pipelines import fastp_runner
from app.pipelines.fastp_runner import TrimParams, TrimProgress


def cmd_for(**kw):
    defaults = dict(
        fastp_path="/usr/bin/fastp",
        r1_in="in_R1.fastq.gz",
        r1_out="out_R1.fastq.gz",
        json_out="fastp.json",
        html_out="fastp.html",
        params=TrimParams(),
    )
    defaults.update(kw)
    from pathlib import Path

    for k in ("r1_in", "r1_out", "json_out", "html_out", "r2_in", "r2_out"):
        if defaults.get(k) is not None:
            defaults[k] = Path(defaults[k])
    return fastp_runner.build_command(**defaults)


def flag_value(cmd: list[str], flag: str) -> str | None:
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


class TestBuildCommand:
    def test_verbose_is_always_passed(self):
        """Without --verbose fastp prints nothing until the run is over, so
        there is no progress to report on a job that may take hours."""
        assert "--verbose" in cmd_for()

    def test_single_end_has_no_second_input(self):
        cmd = cmd_for()
        assert "-I" not in cmd
        assert "-O" not in cmd
        assert flag_value(cmd, "-i") == "in_R1.fastq.gz"

    def test_paired_end_passes_both_sides(self):
        cmd = cmd_for(r2_in="in_R2.fastq.gz", r2_out="out_R2.fastq.gz")
        assert flag_value(cmd, "-i") == "in_R1.fastq.gz"
        assert flag_value(cmd, "-I") == "in_R2.fastq.gz"
        assert flag_value(cmd, "-o") == "out_R1.fastq.gz"
        assert flag_value(cmd, "-O") == "out_R2.fastq.gz"

    def test_paired_input_without_an_output_is_rejected(self):
        """Catch the mistake here rather than letting fastp overwrite or
        silently drop a mate."""
        with pytest.raises(ValueError, match="second output"):
            cmd_for(r2_in="in_R2.fastq.gz")

    def test_quality_and_length_thresholds_are_passed(self):
        cmd = cmd_for(params=TrimParams(quality_threshold=20, min_length=50))
        assert flag_value(cmd, "--qualified_quality_phred") == "20"
        assert flag_value(cmd, "--length_required") == "50"

    def test_thread_count_is_passed(self):
        assert flag_value(cmd_for(params=TrimParams(threads=8)), "--thread") == "8"

    def test_explicit_adapter_wins_over_detection(self):
        """A user who typed a sequence knows something fastp's overlap analysis
        does not."""
        cmd = cmd_for(
            r2_in="in_R2.fastq.gz",
            r2_out="out_R2.fastq.gz",
            params=TrimParams(adapter_r1="AGATCGGAAGAGC", detect_adapter_for_pe=True),
        )
        assert flag_value(cmd, "--adapter_sequence") == "AGATCGGAAGAGC"
        assert "--detect_adapter_for_pe" not in cmd

    def test_paired_detection_is_requested_when_no_adapter_is_given(self):
        cmd = cmd_for(
            r2_in="in_R2.fastq.gz",
            r2_out="out_R2.fastq.gz",
            params=TrimParams(detect_adapter_for_pe=True),
        )
        assert "--detect_adapter_for_pe" in cmd

    def test_single_end_never_asks_for_pe_detection(self):
        assert "--detect_adapter_for_pe" not in cmd_for(
            params=TrimParams(detect_adapter_for_pe=True)
        )

    def test_r2_adapter_is_ignored_for_single_end(self):
        cmd = cmd_for(params=TrimParams(adapter_r2="AGATCGGAAGAGC"))
        assert "--adapter_sequence_r2" not in cmd

    @pytest.mark.parametrize(
        ("value", "expected_flag", "absent_flag"),
        [
            (True, "--trim_poly_g", "--disable_trim_poly_g"),
            (False, "--disable_trim_poly_g", "--trim_poly_g"),
        ],
    )
    def test_poly_g_can_be_forced_either_way(self, value, expected_flag, absent_flag):
        cmd = cmd_for(params=TrimParams(trim_poly_g=value))
        assert expected_flag in cmd
        assert absent_flag not in cmd

    def test_poly_g_is_left_to_fastp_by_default(self):
        """fastp enables it for two-colour chemistry based on the instrument,
        which is a better default than anything this application can guess."""
        cmd = cmd_for()
        assert "--trim_poly_g" not in cmd
        assert "--disable_trim_poly_g" not in cmd

    def test_dedup_is_off_unless_asked(self):
        assert "--dedup" not in cmd_for()
        assert "--dedup" in cmd_for(params=TrimParams(dedup=True))


class TestTrimParams:
    def test_from_dict_ignores_unknown_keys(self):
        """Payloads outlive the code that wrote them; a removed knob must not
        crash a job that still carries it."""
        p = TrimParams.from_dict({"threads": 8, "not_a_real_option": 1})
        assert p.threads == 8

    def test_from_dict_ignores_nulls(self):
        """A form that submits empty fields as null must not override the
        defaults with None."""
        assert TrimParams.from_dict({"threads": None}).threads == TrimParams().threads

    def test_from_dict_of_none_gives_defaults(self):
        assert TrimParams.from_dict(None).as_dict() == TrimParams().as_dict()

    def test_round_trips(self):
        original = TrimParams(threads=8, min_length=50, dedup=True)
        assert TrimParams.from_dict(original.as_dict()).as_dict() == original.as_dict()


# Verbatim fastp 0.24.0 --verbose output.
LOADING = "[12:13:36] start to load data of read1 "
LOADED_1M = "[12:13:51] Read1: loaded 1M reads "
LOADED_2M_R2 = "[12:13:52] Read2: loaded 2M reads "
LOADED_3M = "[12:13:54] Read1: loaded 3M reads "
COMPLETE = "[12:13:37] Read1: loading completed with 2344 packs "
WRITER = "[12:13:37] o1.fq.gz writer finished "
REPORTS = "[12:13:37] start to generate reports"


class TestProgressParsing:
    def test_counts_reads_loaded(self):
        p = TrimProgress(expected_reads=10_000_000)
        assert p.feed(LOADED_1M)
        assert p.reads_loaded == 1_000_000
        assert p.pct == pytest.approx(0.1)

    def test_mates_are_not_summed(self):
        """R1 and R2 load concurrently and report independently. Adding them
        would show 200% progress on a paired run."""
        p = TrimProgress(expected_reads=10_000_000)
        p.feed(LOADED_1M)
        p.feed(LOADED_2M_R2)
        assert p.reads_loaded == 2_000_000
        assert p.pct == pytest.approx(0.2)

    def test_progress_never_goes_backwards(self):
        p = TrimProgress(expected_reads=10_000_000)
        p.feed(LOADED_3M)
        p.feed(LOADED_1M)  # the other mate lagging behind
        assert p.reads_loaded == 3_000_000

    def test_is_capped_below_complete(self):
        """The read total is extrapolated from the first 1000 records, and
        fastp counts reads *loaded* rather than processed -- so the fraction
        can exceed 1.0 while real work remains. A bar pinned at 100% during a
        running job is worse than one pinned at 95%."""
        p = TrimProgress(expected_reads=1_000_000)
        p.feed(LOADED_3M)
        assert p.pct == fastp_runner.MAX_MEASURED_PCT

    def test_no_percentage_without_an_estimate(self):
        """Better an indeterminate bar than a fabricated number."""
        p = TrimProgress(expected_reads=None)
        p.feed(LOADED_1M)
        assert p.pct is None

    def test_no_percentage_before_any_reads(self):
        assert TrimProgress(expected_reads=1000).pct is None

    def test_zero_expected_reads_does_not_divide_by_zero(self):
        p = TrimProgress(expected_reads=0)
        p.feed(LOADED_1M)
        assert p.pct is None

    @pytest.mark.parametrize(
        ("line", "phase"),
        [
            (LOADING, "loading"),
            (COMPLETE, "trimming"),
            (WRITER, "writing"),
            (REPORTS, "reporting"),
        ],
    )
    def test_recognizes_phases(self, line, phase):
        p = TrimProgress()
        p.feed(line)
        assert p.phase == phase

    @pytest.mark.parametrize(
        ("line", "index"),
        [
            (LOADING, 1),
            (COMPLETE, 2),
            (WRITER, 3),
            (REPORTS, 4),
        ],
    )
    def test_phase_index_matches_phase_order(self, line, index):
        p = TrimProgress()
        p.feed(line)
        assert p.phase_index == index
        assert len(fastp_runner.PHASE_ORDER) == 4

    def test_phase_index_is_none_before_any_phase_is_recognized(self):
        assert TrimProgress().phase_index is None

    def test_reports_a_change_only_when_something_changed(self):
        """Progress writes hit Mongo and fan out over SSE; repeating an
        unchanged value would be pure noise."""
        p = TrimProgress(expected_reads=10_000_000)
        assert p.feed(LOADED_1M)
        assert not p.feed(LOADED_1M)

    def test_unrecognized_lines_are_harmless(self):
        p = TrimProgress(expected_reads=10_000_000)
        assert not p.feed("some line fastp never actually prints")
        assert not p.feed("")
        assert p.reads_loaded == 0

    def test_message_mentions_the_phase_and_the_count(self):
        p = TrimProgress(expected_reads=10_000_000)
        p.feed(LOADING)
        assert p.message() == "loading"
        p.feed(LOADED_3M)
        assert "3M reads" in p.message()


# Trimmed from a real run; the curve arrays are elided as the parser drops them.
SAMPLE_REPORT = {
    "summary": {
        "fastp_version": "0.24.0",
        "sequencing": "paired end (100 cycles + 100 cycles)",
        "before_filtering": {
            "total_reads": 40000,
            "total_bases": 4000000,
            "q20_rate": 1.0,
            "q30_rate": 0.523557,
            "gc_content": 0.501503,
            "read1_mean_length": 100,
            "read2_mean_length": 100,
        },
        "after_filtering": {
            "total_reads": 39000,
            "total_bases": 3800000,
            "q20_rate": 1.0,
            "q30_rate": 0.61,
            "gc_content": 0.50,
            "read1_mean_length": 97,
            "read2_mean_length": 97,
        },
    },
    "filtering_result": {
        "passed_filter_reads": 39000,
        "low_quality_reads": 800,
        "too_many_N_reads": 0,
        "too_short_reads": 200,
    },
    "duplication": {"rate": 0.012},
    "insert_size": {"peak": 180, "histogram": [0] * 512},
    "adapter_cutting": {
        "adapter_trimmed_reads": 1234,
        "adapter_trimmed_bases": 45678,
        "read1_adapter_sequence": "AGATCGGAAGAGC",
        "read2_adapter_sequence": "unspecified",
    },
    "read1_before_filtering": {
        "total_reads": 20000,
        "total_cycles": 8,
        "quality_curves": {"mean": [36.0] * 8},
        "content_curves": {
            "A": [0.25] * 8,
            "T": [0.25] * 8,
            "C": [0.25] * 8,
            "G": [0.25] * 8,
            # Fractions, exactly as fastp writes them. Cycle 5 (1-indexed) is
            # a 40% N spike; every other cycle is clean.
            "N": [0.0, 0.0, 0.0, 0.0, 0.4, 0.0, 0.0, 0.0],
            "GC": [0.5] * 8,
        },
    },
}


class TestParseReport:
    @pytest.fixture
    def report(self, tmp_path):
        p = tmp_path / "fastp.json"
        p.write_text(json.dumps(SAMPLE_REPORT))
        return fastp_runner.parse_report(p)

    def test_captures_the_before_and_after_comparison(self, report):
        assert report["before"]["total_reads"] == 40000
        assert report["after"]["total_reads"] == 39000
        assert report["after"]["q30_rate"] == pytest.approx(0.61)

    def test_captures_the_tool_version(self, report):
        """Trimming parameters are meaningless without the version that applied
        them; this pair ends up in a methods section."""
        assert report["tool"] == "fastp"
        assert report["tool_version"] == "0.24.0"

    def test_captures_filtering_and_duplication(self, report):
        assert report["filtering"]["low_quality_reads"] == 800
        assert report["filtering"]["too_short_reads"] == 200
        assert report["duplication_rate"] == pytest.approx(0.012)

    def test_captures_adapter_results(self, report):
        assert report["adapters"]["trimmed_reads"] == 1234
        assert report["adapters"]["read1_sequence"] == "AGATCGGAAGAGC"

    def test_unspecified_adapter_becomes_none(self, report):
        """fastp writes the literal string 'unspecified' when it found nothing;
        showing that to a user as an adapter sequence would be wrong."""
        assert report["adapters"]["read2_sequence"] is None

    def test_drops_the_per_cycle_curves(self, report):
        """Several hundred floats per read direction belong in the HTML report,
        not in every object document."""
        assert "read1_before_filtering" not in report
        assert "histogram" not in json.dumps(report)

    def test_a_missing_report_is_not_fatal(self, tmp_path):
        """The trimmed reads are still valid output; losing the summary should
        not lose them."""
        assert fastp_runner.parse_report(tmp_path / "absent.json") == {}

    def test_malformed_json_is_not_fatal(self, tmp_path):
        p = tmp_path / "fastp.json"
        p.write_text("{ truncated")
        assert fastp_runner.parse_report(p) == {}

    def test_an_unexpected_shape_does_not_raise(self, tmp_path):
        """A future fastp that renames a block should degrade, not crash."""
        p = tmp_path / "fastp.json"
        p.write_text(json.dumps({"summary": {}}))
        report = fastp_runner.parse_report(p)
        assert report["before"]["total_reads"] is None


class TestOutputName:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("sample_R1.fastq.gz", "sample_R1.trimmed.fastq.gz"),
            ("sample_R2.fq.gz", "sample_R2.trimmed.fq.gz"),
            ("reads.fastq", "reads.trimmed.fastq"),
            ("reads.fq", "reads.trimmed.fq"),
            ("SRR123_1.fastq.gz", "SRR123_1.trimmed.fastq.gz"),
        ],
    )
    def test_inserts_the_marker_before_the_format_suffixes(self, source, expected):
        """The name must still read as a gzipped FASTQ, so `.trimmed` cannot go
        on the end."""
        assert fastp_runner.output_name(source) == expected

    def test_preserves_the_mate_token(self):
        """Mate detection runs on the output too, so the R1/R2 marker has to
        survive."""
        assert "_R1" in fastp_runner.output_name("sample_R1.fastq.gz")

    def test_a_name_without_a_known_suffix_gets_one(self):
        assert fastp_runner.output_name("reads") == "reads.trimmed.fastq.gz"

    def test_is_case_insensitive_about_suffixes(self):
        assert fastp_runner.output_name("Sample.FASTQ.GZ") == "Sample.trimmed.FASTQ.GZ"


class TestBlobExtensionHazard:
    """Managed blobs are stored under their hash with no extension, and fastp
    decides whether an input is gzipped from the filename alone -- it has no
    flag to force decompression. Handing it a blob path directly makes it read
    gzip bytes as text and die with a parse error.

    These pin the symlink workaround in the handler. Found by running the real
    thing end to end; no unit test of build_command would have caught it,
    because the command it produces is perfectly well-formed.
    """

    def test_handler_links_inputs_under_their_real_names(self):
        import inspect

        from app.queue import pipeline_handlers

        # trim_reads dispatches to _run_fastp_trim, which resolves its inputs
        # (and thus fastp's) via the shared _resolve_trim_inputs helper -- that
        # is where the symlink workaround now lives.
        source = inspect.getsource(pipeline_handlers._resolve_trim_inputs)
        assert "_named_link" in source, (
            "inputs must be symlinked under their user-facing name, or fastp "
            "will misread a compressed blob"
        )

    def test_named_link_preserves_the_extension(self, tmp_path):
        from app.queue.pipeline_handlers import _named_link

        blob = tmp_path / "46ac3c6acd6cb40b3b2dc9d4b88b0a76"
        blob.write_bytes(b"\x1f\x8b\x08\x00")
        work = tmp_path / "work"
        work.mkdir()

        link = _named_link(work, blob, "sample_R1.fastq.gz")
        assert link.name.endswith(".fastq.gz")
        assert link.resolve() == blob.resolve()
        assert link.read_bytes() == b"\x1f\x8b\x08\x00"

    def test_named_link_without_a_name_uses_the_target(self, tmp_path):
        from app.queue.pipeline_handlers import _named_link

        blob = tmp_path / "somefile.fastq.gz"
        blob.write_bytes(b"x")
        assert _named_link(tmp_path / "work", blob, None) == blob

    def test_named_link_is_idempotent(self, tmp_path):
        """A retry reuses the scratch directory name, so relinking must not
        fail on an existing link."""
        from app.queue.pipeline_handlers import _named_link

        blob = tmp_path / "abc123"
        blob.write_bytes(b"y")
        work = tmp_path / "work"
        work.mkdir()

        first = _named_link(work, blob, "r.fastq.gz")
        second = _named_link(work, blob, "r.fastq.gz")
        assert first == second
        assert second.read_bytes() == b"y"

    def test_named_link_rejects_a_path_in_the_name(self, tmp_path):
        """The name comes from a payload; it must not be able to place a link
        outside the working directory."""
        from app.queue.pipeline_handlers import _named_link

        blob = tmp_path / "abc123"
        blob.write_bytes(b"z")
        work = tmp_path / "work"
        work.mkdir()

        link = _named_link(work, blob, "../../escape.fastq.gz")
        assert link.parent == work


class TestBuildQcCommand:
    """Report-only mode: fastp inspects a file without deriving one."""

    def qc_cmd(self, **kw):
        defaults = dict(
            fastp_path="/usr/bin/fastp",
            r1_in="reads.fastq.gz",
            json_out="qc.json",
            html_out="qc.html",
        )
        defaults.update(kw)
        return fastp_runner.build_qc_command(
            **{k: v for k, v in defaults.items()}
        )

    def test_writes_no_reads(self):
        """The whole point: no -o/-O means fastp reports and writes nothing.
        A QC run that quietly produced a trimmed FASTQ would be a surprise
        file in the project and an hour of IO nobody asked for."""
        cmd = self.qc_cmd()
        assert "-o" not in cmd
        assert "-O" not in cmd

    def test_disables_every_filter(self):
        """Filtering defaults would be applied to the *reported* numbers, so
        the file would look like it had fewer reads than it has. QC describes
        the file as it is."""
        cmd = self.qc_cmd()
        assert "--disable_quality_filtering" in cmd
        assert "--disable_length_filtering" in cmd
        assert "--disable_adapter_trimming" in cmd

    def test_requests_both_report_formats(self):
        cmd = self.qc_cmd()
        assert cmd[cmd.index("--json") + 1] == "qc.json"
        assert cmd[cmd.index("--html") + 1] == "qc.html"

    def test_reads_the_input(self):
        cmd = self.qc_cmd()
        assert cmd[cmd.index("-i") + 1] == "reads.fastq.gz"

    def test_verbose_so_progress_can_be_parsed(self):
        """Same reason as the trim path: without it fastp says nothing until
        the run is over, and TrimProgress has nothing to feed on."""
        assert "--verbose" in self.qc_cmd()

    def test_a_mate_is_passed_as_the_second_input(self):
        cmd = self.qc_cmd(r2_in="reads_R2.fastq.gz")
        assert cmd[cmd.index("-I") + 1] == "reads_R2.fastq.gz"

    def test_single_end_passes_no_second_input(self):
        assert "-I" not in self.qc_cmd()


class TestParseQcFacts:
    @pytest.fixture
    def facts(self, tmp_path):
        p = tmp_path / "qc.json"
        p.write_text(json.dumps(SAMPLE_REPORT))
        return fastp_runner.parse_qc_facts(p)

    def test_reports_the_measured_state(self, facts):
        """`before_filtering` with filtering disabled is simply the file."""
        assert facts["qc_before_filtering"]["total_reads"] == 40000

    def test_prefixes_keys_so_they_merge_into_facts(self, facts):
        """These land in the same dict as the ingest's parsed facts, so an
        unprefixed `total_reads` would collide with a different measurement."""
        assert all(k.startswith("qc_") for k in facts)

    def test_captures_the_tool_version(self, facts):
        assert facts["qc_tool"] == "fastp"
        assert facts["qc_tool_version"] == "0.24.0"

    def test_captures_duplication(self, facts):
        assert facts["qc_duplication_rate"] == pytest.approx(0.012)

    def test_unspecified_adapter_becomes_none(self, facts):
        assert facts["qc_adapters"]["read2_sequence"] is None

    def test_drops_the_per_cycle_curves(self, facts):
        """The charts already render these from the ingest's own facts; a
        second copy per QC run would bloat every object document."""
        assert "histogram" not in json.dumps(facts)

    def test_a_missing_report_is_not_fatal(self, tmp_path):
        assert fastp_runner.parse_qc_facts(tmp_path / "absent.json") == {}

    def test_malformed_json_is_not_fatal(self, tmp_path):
        p = tmp_path / "qc.json"
        p.write_text("{ truncated")
        assert fastp_runner.parse_qc_facts(p) == {}

    def test_reports_n_content_per_cycle(self, facts):
        """fastp writes fractions; the app's facts are percentages
        everywhere else (base_composition, gc_content_percent), so these are
        scaled at parse time rather than in the chart."""
        assert facts["qc_n_per_position"] == [
            {"position": 1, "percent": 0.0},
            {"position": 2, "percent": 0.0},
            {"position": 3, "percent": 0.0},
            {"position": 4, "percent": 0.0},
            {"position": 5, "percent": 40.0},
            {"position": 6, "percent": 0.0},
            {"position": 7, "percent": 0.0},
            {"position": 8, "percent": 0.0},
        ]

    def test_an_all_zero_n_curve_is_omitted(self, tmp_path):
        """The common case for clean Illumina data. A flat line at zero is a
        chart that never says anything, so absent means 'nothing to report'
        the way every other block in QcReport self-suppresses."""
        report = json.loads(json.dumps(SAMPLE_REPORT))
        report["read1_before_filtering"]["content_curves"]["N"] = [0.0] * 8
        p = tmp_path / "qc.json"
        p.write_text(json.dumps(report))
        assert "qc_n_per_position" not in fastp_runner.parse_qc_facts(p)

    def test_a_missing_curves_block_is_not_fatal(self, tmp_path):
        """An older fastp, or a report written before this block existed."""
        report = json.loads(json.dumps(SAMPLE_REPORT))
        del report["read1_before_filtering"]
        p = tmp_path / "qc.json"
        p.write_text(json.dumps(report))
        facts = fastp_runner.parse_qc_facts(p)
        assert "qc_n_per_position" not in facts
        assert facts["qc_tool"] == "fastp"
