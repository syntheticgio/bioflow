"""The three reference-guided assembly handlers, driven at the subprocess seam.

`consensus_from_alignment`, `polish_assembly` and `scaffold_assembly` are
SUBPROCESS handlers, so what is testable without the real binaries is
everything between the payload and the result dict: which commands get built
and in what order, which stages are skipped, what lands in `facts`, and --
the part these three make load-bearing -- what happens when a tool exits 0
having produced nothing.

That last one is why this file exists rather than leaning on the runner
tests. All three handlers check their output file explicitly instead of
trusting the return code, and `scaffold_assembly`'s docstring states outright
that the file's existence is the *only* success signal it has: RagTag exits 0
after writing no FASTA when given an unrelated reference. A test that stubbed
`run_subprocess` to return 0 and also wrote the output would pass whether or
not that check existed, so each handler gets the opposite case as well -- a
zero exit with no file written.

The result dicts are asserted against the keys `results._apply_*` actually
reads, since a handler that returns a correct FASTA under a key the applier
does not look for is the "reported success while storing nothing" failure
CLAUDE.md describes, and nothing else in the suite would catch it.
"""

import re
from pathlib import Path

import pytest

from app.errors import PermanentError, RetryableError
from app.pipelines.tools import Tool
from app.queue import reference_assembly_handlers as handlers
from app.queue.registry import JobContext

# Real iVar 1.4.4 consensus stderr, in the shape parse_consensus_stderr's
# anchored regexes expect (see ivar_runner._REFERENCE_LENGTH_RE).
IVAR_STDERR = """\
Reference length: 29903
Positions with 0 depth: 120
Positions with depth below 10: 45
"""

# Real Polypolish 0.7.1 per-contig output. Two contigs on purpose: the parser
# sums across blocks, and a single-contig fixture would pass against a parser
# that took only the first match.
POLYPOLISH_STDERR = """\
Polishing ctgA (10,000 bp):
  mean read depth: 57.5x
  11 positions changed (0.1100% of total positions)
Polishing ctgB (10,000 bp):
  mean read depth: 60.0x
  9 positions changed (0.0900% of total positions)
"""

RAGTAG_STATS = (
    "placed_sequences\tplaced_bp\tunplaced_sequences\tunplaced_bp\tgap_bp\tgap_sequences\n"
    "7\t100000\t2\t3000\t500\t5\n"
)

RAGTAG_CONFIDENCE = (
    "query\tgrouping_confidence\tlocation_confidence\torientation_confidence\n"
    "ctg5_c1_0\t1.0\t1.0\t1.0\n"
    "ctg1_c1_1\t0.4\t0.9\t1.0\n"
)


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Keep workdirs and logs inside tmp_path.

    `_prepare_workdir` writes under `settings.tmp_dir` and every handler
    writes a log under `settings.logs_dir`; both derive from `bioinfo_home`.
    Without this the tests would scribble into the real data directory.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
    return tmp_path / "home"


def _ctx(payload: dict, job_id: str = "job-ref-1") -> JobContext:
    return JobContext(job_id=job_id, payload=payload, epoch=1, attempts=1, owner="ref-owner")


def _tool(name: str, version: str = "1.0.0") -> Tool:
    return Tool(name=name, path=f"/usr/local/bin/{name}", version=version)


def _source(tmp_path: Path, name: str, text: str = ">c\nACGT\n") -> Path:
    path = tmp_path / "sources" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class _Recorder:
    """A `run_subprocess` stand-in that records every command it was given.

    `write` is called after each command with the work directory, so a test
    can plant the file the real tool would have produced -- or deliberately
    not plant it, which is the case these handlers exist to survive.
    """

    def __init__(self, *, write=None, codes=None, lines=""):
        self.cmds: list[list[str]] = []
        self._write = write
        self._codes = list(codes or [])
        self._lines = lines

    def __call__(self, ctx, cmd, *, log_path=None, parser=None, on_line=None):
        self.cmds.append(list(cmd))
        if on_line is not None:
            for line in self._lines.splitlines():
                on_line(line)
        if self._write is not None:
            self._write(len(self.cmds), cmd)
        return self._codes.pop(0) if self._codes else 0

    @property
    def flat(self) -> list[str]:
        return [part for cmd in self.cmds for part in cmd]


class TestConsensusFromAlignment:
    def _payload(self, tmp_path, **extra) -> dict:
        payload = {
            "bam_path": str(_source(tmp_path, "sample.bam", "bam")),
            "bam_name": "sample.bam",
            "bam_object_id": "64b000000000000000000001",
            "reference_path": str(_source(tmp_path, "ref.fasta")),
            "reference_name": "ref.fasta",
            "reference_object_id": "64b000000000000000000002",
        }
        payload.update(extra)
        return payload

    def _tools(self, monkeypatch):
        monkeypatch.setattr(handlers.tools, "ivar", lambda: _tool("ivar", "1.4.4"))
        monkeypatch.setattr(handlers.tools, "samtools", lambda: _tool("samtools", "1.19"))

    def _run(self, monkeypatch, tmp_path, payload, *, produce=True, codes=None):
        self._tools(monkeypatch)

        def write(n, cmd):
            # Stage 1 of the trimmed path is `ivar trim`, which must leave a
            # BAM behind; the consensus stage writes the FASTA. Both are
            # located from the out-prefix the handler chose, so the fake
            # writes wherever the real tool would have.
            for part in cmd:
                if part.endswith("/trimmed"):
                    Path(part).with_suffix(".bam").write_text("trimmed bam")
                if part.endswith("/trimmed.sorted.bam"):
                    Path(part).write_text("sorted bam")
            joined = " ".join(cmd)
            # The consensus stage is a `sh -o pipefail -c "<pipeline>"` call,
            # so `-p <prefix>` lives inside one shell-quoted argument rather
            # than as its own argv element. Recover the prefix from that
            # string the same way the shell would.
            if "ivar consensus" in joined and produce:
                prefix = re.search(r"ivar consensus -p (\S+)", joined).group(1)
                Path(prefix).with_suffix(".fa").write_text(">consensus\nACGTACGT\n")

        recorder = _Recorder(write=write, codes=codes, lines=IVAR_STDERR)
        monkeypatch.setattr(handlers, "run_subprocess", recorder)
        return recorder

    def test_without_a_primer_bed_the_trim_and_sort_stages_are_skipped(self, monkeypatch, tmp_path):
        """One subprocess, not three. `ivar trim` is gated on a primer BED
        being supplied, and running it against an empty BED would strip
        nothing while still forcing the sort that only exists to undo iVar's
        unsorted output."""
        payload = self._payload(tmp_path)
        recorder = self._run(monkeypatch, tmp_path, payload)

        result = handlers.consensus_from_alignment(_ctx(payload))

        assert len(recorder.cmds) == 1
        assert "trim" not in " ".join(recorder.cmds[0])
        assert result["facts"]["consensus_primers_trimmed"] is False

    def test_a_primer_bed_adds_trim_then_sort_before_the_consensus(self, monkeypatch, tmp_path):
        """Order matters and is not incidental: `ivar consensus` reads a
        position-sorted pileup, and iVar's own trim output is unsorted, so a
        consensus built straight off the trimmed BAM would be wrong rather
        than merely slower."""
        payload = self._payload(
            tmp_path,
            primer_bed_path=str(_source(tmp_path, "primers.bed", "chr\t1\t20\tp1\n")),
            primer_bed_name="primers.bed",
        )
        recorder = self._run(monkeypatch, tmp_path, payload)

        result = handlers.consensus_from_alignment(_ctx(payload))

        assert len(recorder.cmds) == 3
        assert "trim" in " ".join(recorder.cmds[0])
        assert "sort" in " ".join(recorder.cmds[1])
        assert "consensus" in " ".join(recorder.cmds[2])
        assert result["facts"]["consensus_primers_trimmed"] is True

    def test_the_sorted_bam_is_what_reaches_the_consensus_stage(self, monkeypatch, tmp_path):
        """The trimmed path must hand `mpileup` the *sorted* BAM. Passing the
        original input through would leave the trim stage doing nothing at
        all, silently -- primers would stay in the consensus and
        `consensus_primers_trimmed: True` would be a false claim."""
        payload = self._payload(
            tmp_path,
            primer_bed_path=str(_source(tmp_path, "primers.bed", "chr\t1\t20\tp1\n")),
            primer_bed_name="primers.bed",
        )
        recorder = self._run(monkeypatch, tmp_path, payload)

        handlers.consensus_from_alignment(_ctx(payload))

        consensus_cmd = " ".join(recorder.cmds[2])
        assert "trimmed.sorted.bam" in consensus_cmd
        assert "in_sample.bam" not in consensus_cmd

    def test_facts_carry_the_thresholds_the_run_actually_used(self, monkeypatch, tmp_path):
        """The parsed stderr is not enough on its own: a consensus is only
        interpretable next to the depth and frequency cutoffs that produced
        it, and those live in the payload, not in iVar's summary."""
        payload = self._payload(tmp_path, min_quality=30, min_freq=0.75, min_depth=20)
        self._run(monkeypatch, tmp_path, payload)

        facts = handlers.consensus_from_alignment(_ctx(payload))["facts"]

        assert facts["consensus_min_quality"] == 30
        assert facts["consensus_min_freq"] == 0.75
        assert facts["consensus_min_depth"] == 20
        assert facts["consensus_tool_version"] == "1.4.4"

    def test_min_freq_zero_is_kept_rather_than_replaced_by_the_default(self, monkeypatch, tmp_path):
        """0.0 is a meaningful setting (call the majority base at any
        frequency) and is also falsy, so an `or 0.0` would read as a no-op
        while quietly being correct here -- but the same shape on
        `min_quality`/`min_depth` is what makes this worth pinning: the
        handler must not treat an explicit 0.0 as "unset"."""
        payload = self._payload(tmp_path, min_freq=0.0)
        self._run(monkeypatch, tmp_path, payload)

        facts = handlers.consensus_from_alignment(_ctx(payload))["facts"]

        assert facts["consensus_min_freq"] == 0.0

    def test_ambiguity_is_derived_from_both_zero_and_low_depth_positions(
        self, monkeypatch, tmp_path
    ):
        """The N count a user reads is zero-depth *plus* below-threshold
        positions. Counting only one of the two understates how much of the
        consensus is unsupported, which is the single number that decides
        whether it is usable."""
        payload = self._payload(tmp_path)
        self._run(monkeypatch, tmp_path, payload)

        facts = handlers.consensus_from_alignment(_ctx(payload))["facts"]

        assert facts["consensus_n_count"] == 165
        assert facts["consensus_ambiguous_pct"] == round(100 * 165 / 29903, 2)

    def test_a_zero_exit_with_no_fasta_is_an_error_not_a_success(self, monkeypatch, tmp_path):
        """iVar's exit codes are unreliable, so the FASTA's existence is the
        real success signal. Without this check the handler would return a
        result pointing at a file that is not there, and the applier would
        fail on ingest with the cause several steps behind it."""
        payload = self._payload(tmp_path)
        self._run(monkeypatch, tmp_path, payload, produce=False)

        with pytest.raises(RetryableError, match="no sequence"):
            handlers.consensus_from_alignment(_ctx(payload))

    def test_an_empty_fasta_is_treated_the_same_as_a_missing_one(self, monkeypatch, tmp_path):
        """A zero-byte consensus is the same non-result as no file, and it is
        the shape iVar actually produces when the pileup is empty."""
        self._tools(monkeypatch)
        payload = self._payload(tmp_path)

        def write(n, cmd):
            joined = " ".join(cmd)
            if "ivar consensus" in joined:
                prefix = re.search(r"ivar consensus -p (\S+)", joined).group(1)
                Path(prefix).with_suffix(".fa").write_text("")

        monkeypatch.setattr(handlers, "run_subprocess", _Recorder(write=write))

        with pytest.raises(RetryableError, match="no sequence"):
            handlers.consensus_from_alignment(_ctx(payload))

    def test_a_trim_that_exits_zero_without_a_bam_stops_the_job(self, monkeypatch, tmp_path):
        """Same posture one stage earlier. Letting this through would sort a
        BAM that does not exist and blame samtools for iVar's failure."""
        self._tools(monkeypatch)
        payload = self._payload(
            tmp_path,
            primer_bed_path=str(_source(tmp_path, "primers.bed", "chr\t1\t20\tp1\n")),
            primer_bed_name="primers.bed",
        )
        monkeypatch.setattr(handlers, "run_subprocess", _Recorder())

        with pytest.raises(RetryableError, match="wrote no BAM"):
            handlers.consensus_from_alignment(_ctx(payload))

    def test_a_nonzero_consensus_exit_raises(self, monkeypatch, tmp_path):
        payload = self._payload(tmp_path)
        self._run(monkeypatch, tmp_path, payload, produce=False, codes=[1])

        with pytest.raises((PermanentError, RetryableError)):
            handlers.consensus_from_alignment(_ctx(payload))

    def test_the_result_carries_the_keys_the_applier_reads(self, monkeypatch, tmp_path):
        """`_apply_consensus_from_alignment` returns early unless it finds
        both `output` and `bam_object_id`, and builds provenance from
        `reference_object_id`. A handler that renamed any of these would log
        nothing and store nothing -- a job green in the UI with no object
        behind it."""
        payload = self._payload(tmp_path)
        self._run(monkeypatch, tmp_path, payload)

        result = handlers.consensus_from_alignment(_ctx(payload))

        assert result["bam_object_id"] == "64b000000000000000000001"
        assert result["reference_object_id"] == "64b000000000000000000002"
        assert result["output"]["name"] == "consensus.fasta"
        assert Path(result["output"]["tmp_path"]).exists()
        assert result["job_id"] == "job-ref-1"


class TestPolishAssembly:
    def _payload(self, tmp_path, *, paired=True, **extra) -> dict:
        payload = {
            "draft_path": str(_source(tmp_path, "draft.fasta")),
            "draft_name": "draft.fasta",
            "draft_object_id": "64b000000000000000000011",
            "reads_path": str(_source(tmp_path, "r1.fastq", "@r\nACGT\n+\nIIII\n")),
            "reads_name": "r1.fastq",
            "reads_object_id": "64b000000000000000000012",
        }
        if paired:
            payload["mate_path"] = str(_source(tmp_path, "r2.fastq", "@r\nACGT\n+\nIIII\n"))
            payload["mate_name"] = "r2.fastq"
            payload["mate_object_id"] = "64b000000000000000000013"
        payload.update(extra)
        return payload

    def _tools(self, monkeypatch):
        monkeypatch.setattr(handlers.tools, "polypolish", lambda: _tool("polypolish", "0.7.1"))
        monkeypatch.setattr(handlers.tools, "bwa_mem2", lambda: _tool("bwa-mem2", "2.2.1"))

    def _run(self, monkeypatch, tmp_path, *, produce=True, codes=None):
        self._tools(monkeypatch)

        def write(n, cmd):
            joined = " ".join(cmd)
            if "polished.fasta" in joined and produce:
                for part in joined.split():
                    if part.endswith("polished.fasta"):
                        Path(part).write_text(">ctgA\nACGT\n>ctgB\nACGT\n")

        recorder = _Recorder(write=write, codes=codes, lines=POLYPOLISH_STDERR)
        monkeypatch.setattr(handlers, "run_subprocess", recorder)
        return recorder

    def test_reads_with_no_object_id_are_rejected_before_any_subprocess(
        self, monkeypatch, tmp_path
    ):
        """Polypolish has nothing to polish *with* here. Permanent rather
        than retryable: an absent read slot is a launch mistake, not a
        transient one, and the aligner would otherwise be indexed and run
        before anything noticed."""
        self._tools(monkeypatch)
        recorder = _Recorder()
        monkeypatch.setattr(handlers, "run_subprocess", recorder)
        payload = {
            "draft_path": str(_source(tmp_path, "draft.fasta")),
            "draft_name": "draft.fasta",
            "draft_object_id": "64b000000000000000000011",
        }

        with pytest.raises(PermanentError, match="at least one read file"):
            handlers.polish_assembly(_ctx(payload))

        assert recorder.cmds == []

    def test_each_read_file_is_aligned_in_its_own_invocation(self, monkeypatch, tmp_path):
        """R1 and R2 must not be aligned as a pair. Polypolish needs every
        location a read maps to, and a paired invocation lets the aligner use
        the mate to pick one -- which is exactly the information the filter
        step is supposed to apply later, on Polypolish's terms."""
        recorder = self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path)

        handlers.polish_assembly(_ctx(payload))

        aligns = [c for c in recorder.cmds if "bwa-mem2 mem" in " ".join(c)]
        assert len(aligns) == 2
        for cmd in aligns:
            joined = " ".join(cmd)
            assert not ("in_r1.fastq" in joined and "in_r2.fastq" in joined)

    def test_the_paired_run_filters_before_polishing(self, monkeypatch, tmp_path):
        """Five stages in order: index, align, align, filter, polish."""
        recorder = self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path)

        handlers.polish_assembly(_ctx(payload))

        assert len(recorder.cmds) == 5
        assert "index" in " ".join(recorder.cmds[0])
        assert "filter" in " ".join(recorder.cmds[3])
        assert "polish" in " ".join(recorder.cmds[4])

    def test_the_filtered_sams_are_what_reach_the_polish_step(self, monkeypatch, tmp_path):
        """The filter's whole purpose is to drop alignments inconsistent with
        the insert size. Polishing the unfiltered SAMs instead would still
        produce a FASTA, so nothing but this assertion would notice."""
        recorder = self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path)

        handlers.polish_assembly(_ctx(payload))

        polish_cmd = " ".join(recorder.cmds[4])
        assert "filtered_1.sam" in polish_cmd
        assert "filtered_2.sam" in polish_cmd
        assert "alignments_1.sam" not in polish_cmd

    def test_a_single_read_file_skips_the_filter_stage(self, monkeypatch, tmp_path):
        """Insert-size filtering is meaningless without a pair, and
        Polypolish's filter subcommand takes exactly two SAMs -- calling it
        with one would fail a run that has nothing wrong with it."""
        recorder = self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path, paired=False)

        result = handlers.polish_assembly(_ctx(payload))

        assert len(recorder.cmds) == 3
        assert not any("filter" in " ".join(c) for c in recorder.cmds)
        assert result["facts"]["polish_read_files"] == 1

    def test_changed_positions_are_summed_across_contigs(self, monkeypatch, tmp_path):
        """Polypolish reports per contig, not per run. 11 + 9, not 11 -- a
        parser taking the first block would understate a two-contig polish by
        exactly the amount that makes the fact useful."""
        self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path)

        facts = handlers.polish_assembly(_ctx(payload))["facts"]

        assert facts["polish_changed_positions"] == 20
        assert facts["polish_contigs"] == 2

    def test_the_aligner_is_recorded_because_nothing_else_witnesses_it(self, monkeypatch, tmp_path):
        """This handler aligns internally rather than taking a BAM, so no
        object in the graph records which aligner ran. These facts are the
        only provenance that step gets -- the module docstring's "recorded as
        facts rather than checked at launch"."""
        self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path)

        facts = handlers.polish_assembly(_ctx(payload))["facts"]

        assert facts["polish_aligner"] == "bwa-mem2"
        assert facts["polish_aligner_version"] == "2.2.1"
        assert facts["polish_tool_version"] == "0.7.1"

    def test_low_depth_turns_on_careful_mode_and_records_the_estimate(self, monkeypatch, tmp_path):
        """`--careful` is decided from the pre-run depth estimate, and the
        estimate is recorded next to the decision: a run whose measured depth
        disagrees with it is a run whose careful decision was made on bad
        input, which nothing else would show."""
        from app.pipelines import polypolish_runner

        self._run(monkeypatch, tmp_path)
        depth = 5.0
        payload = self._payload(tmp_path, depth=depth)

        facts = handlers.polish_assembly(_ctx(payload))["facts"]

        assert facts["polish_careful_mode"] is polypolish_runner.params_for_depth(depth).careful
        assert facts["polish_estimated_depth"] == 5.0

    def test_a_zero_exit_with_no_output_is_an_error(self, monkeypatch, tmp_path):
        """Same trust posture as its two siblings: the file, not the code."""
        self._run(monkeypatch, tmp_path, produce=False)
        payload = self._payload(tmp_path)

        with pytest.raises(RetryableError, match="no sequence"):
            handlers.polish_assembly(_ctx(payload))

    def test_an_aligner_failure_names_the_aligner_not_the_polisher(self, monkeypatch, tmp_path):
        """A large draft can exhaust memory inside bwa-mem2's index build,
        before Polypolish is ever reached. "polypolish failed" would send the
        user to the wrong binary and the wrong resource budget."""
        self._run(monkeypatch, tmp_path, produce=False, codes=[0, 1])
        payload = self._payload(tmp_path)

        with pytest.raises((PermanentError, RetryableError), match="bwa-mem2 mem"):
            handlers.polish_assembly(_ctx(payload))

    def test_the_result_carries_the_keys_the_applier_reads(self, monkeypatch, tmp_path):
        """`_apply_polish_assembly` bails without `output` and
        `draft_object_id`, and builds parents from the two read ids. Dropping
        a read id costs the provenance graph the only thing that tells two
        polishes of one draft apart."""
        self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path)

        result = handlers.polish_assembly(_ctx(payload))

        assert result["draft_object_id"] == "64b000000000000000000011"
        assert result["reads_object_id"] == "64b000000000000000000012"
        assert result["mate_object_id"] == "64b000000000000000000013"
        assert result["output"]["name"] == "polished.fasta"
        assert Path(result["output"]["tmp_path"]).exists()


class TestScaffoldAssembly:
    def _payload(self, tmp_path, **extra) -> dict:
        payload = {
            "draft_path": str(_source(tmp_path, "draft.fasta")),
            "draft_name": "draft.fasta",
            "draft_object_id": "64b000000000000000000021",
            "reference_path": str(_source(tmp_path, "ref.fasta")),
            "reference_name": "ref.fasta",
            "reference_object_id": "64b000000000000000000022",
        }
        payload.update(extra)
        return payload

    def _run(
        self,
        monkeypatch,
        tmp_path,
        *,
        produce=True,
        agp=True,
        stats=RAGTAG_STATS,
        confidence=RAGTAG_CONFIDENCE,
        code=0,
    ):
        monkeypatch.setattr(handlers.tools, "ragtag", lambda: _tool("ragtag", "2.1.0"))

        def write(n, cmd):
            out_dir = None
            for i, part in enumerate(cmd):
                if part == "-o":
                    out_dir = Path(cmd[i + 1])
            if out_dir is None:
                return
            out_dir.mkdir(parents=True, exist_ok=True)
            if produce:
                (out_dir / "ragtag.scaffold.fasta").write_text(
                    ">chr1_RagTag\nACGT\n>chr2_RagTag\nACGT\n>ctg9\nACGT\n"
                )
            if agp:
                (out_dir / "ragtag.scaffold.agp").write_text("##agp-version 2.1\n")
            if stats:
                (out_dir / "ragtag.scaffold.stats").write_text(stats)
            if confidence:
                (out_dir / "ragtag.scaffold.confidence.txt").write_text(confidence)

        recorder = _Recorder(write=write, codes=[code])
        monkeypatch.setattr(handlers, "run_subprocess", recorder)
        return recorder

    def test_scaffolding_is_a_single_subprocess(self, monkeypatch, tmp_path):
        """RagTag invokes minimap2 itself, so unlike polish_assembly there is
        no separate alignment stage to sequence."""
        recorder = self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path)

        handlers.scaffold_assembly(_ctx(payload))

        assert len(recorder.cmds) == 1

    def test_a_zero_exit_with_no_fasta_is_permanent_not_retryable(self, monkeypatch, tmp_path):
        """The failure this handler exists to catch. Given an unrelated
        reference RagTag raises internally, writes no scaffolded FASTA, and
        still exits 0 -- so the return code is not evidence in either
        direction and the file is the only success signal.

        Permanent, not retryable: "no useful alignments" is a statement about
        these two inputs and the same pair fails identically next time.
        Retrying would burn the attempt budget on a verdict already reached.
        """
        self._run(monkeypatch, tmp_path, produce=False)
        payload = self._payload(tmp_path)

        with pytest.raises(PermanentError, match="no useful alignments"):
            handlers.scaffold_assembly(_ctx(payload))

    def test_the_failure_message_points_at_the_log(self, monkeypatch, tmp_path):
        """RagTag's own diagnosis is the thing that tells a user what to
        change -- a closer reference, or coarser --mm2-params. The message
        must carry them to it rather than restating the exit code."""
        self._run(monkeypatch, tmp_path, produce=False)
        payload = self._payload(tmp_path)

        with pytest.raises(PermanentError) as excinfo:
            handlers.scaffold_assembly(_ctx(payload))

        assert "job-ref-1.log" in str(excinfo.value)

    def test_an_empty_fasta_fails_the_same_way_a_missing_one_does(self, monkeypatch, tmp_path):
        monkeypatch.setattr(handlers.tools, "ragtag", lambda: _tool("ragtag", "2.1.0"))

        def write(n, cmd):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "ragtag.scaffold.fasta").write_text("")

        monkeypatch.setattr(handlers, "run_subprocess", _Recorder(write=write))

        with pytest.raises(PermanentError, match="no useful alignments"):
            handlers.scaffold_assembly(_ctx(self._payload(tmp_path)))

    def test_a_nonzero_exit_that_still_produced_a_fasta_succeeds(self, monkeypatch, tmp_path):
        """The mirror of the case above, and the reason the check is phrased
        against the file rather than the code: RagTag's exit status is not
        trustworthy evidence *either* way, so a scaffolded assembly on disk
        must not be discarded because of it."""
        self._run(monkeypatch, tmp_path, code=1)
        payload = self._payload(tmp_path)

        result = handlers.scaffold_assembly(_ctx(payload))

        assert Path(result["output"]["tmp_path"]).exists()

    def test_the_scaffold_count_comes_from_the_fasta_not_the_stats(self, monkeypatch, tmp_path):
        """`.stats` counts input contigs placed and unplaced; the deliverable
        counts output sequences. They differ whenever two contigs join one
        scaffold, which is the normal case -- 7 placed + 2 unplaced here
        against 3 actual scaffolds."""
        self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path)

        facts = handlers.scaffold_assembly(_ctx(payload))["facts"]

        assert facts["scaffold_count"] == 3
        assert facts["scaffold_placed_sequences"] == 7
        assert facts["scaffold_unplaced_sequences"] == 2

    def test_confidence_is_the_minimum_across_contigs(self, monkeypatch, tmp_path):
        """0.4, not the 0.7 mean. The one badly-placed contig is precisely
        what a user needs to see before trusting the arrangement, and a mean
        averages it away."""
        self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path)

        facts = handlers.scaffold_assembly(_ctx(payload))["facts"]

        assert facts["scaffold_min_grouping_confidence"] == 0.4

    def test_the_reference_identity_is_recorded_on_the_facts(self, monkeypatch, tmp_path):
        """The obligation neither sibling carries. RagTag names scaffolds
        after the reference's own sequences, so the output's structure is
        partly a claim about the reference -- and `scaffold_reference_name`
        is what a human reads before trusting it."""
        self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path)

        facts = handlers.scaffold_assembly(_ctx(payload))["facts"]

        assert facts["scaffold_reference_object_id"] == "64b000000000000000000022"
        assert facts["scaffold_reference_name"] == "ref.fasta"
        assert facts["scaffold_aligner"] == "minimap2"
        assert facts["scaffold_tool_version"] == "2.1.0"

    def test_missing_stats_files_do_not_fail_a_run_that_produced_a_fasta(
        self, monkeypatch, tmp_path
    ):
        """Both summaries are read only if present. A summary that cannot be
        read costs a blank field; raising here would discard a scaffolded
        assembly that already exists on disk."""
        self._run(monkeypatch, tmp_path, stats="", confidence="")
        payload = self._payload(tmp_path)

        facts = handlers.scaffold_assembly(_ctx(payload))["facts"]

        assert "scaffold_placed_sequences" not in facts
        assert "scaffold_min_grouping_confidence" not in facts
        assert facts["scaffold_count"] == 3

    def test_the_agp_rides_along_when_ragtag_wrote_one(self, monkeypatch, tmp_path):
        """The one intermediate this slice keeps: the only record of which
        contig went where and in what orientation. `_apply_scaffold_assembly`
        ingests it as its own visible object, keyed on exactly this name."""
        self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path)

        result = handlers.scaffold_assembly(_ctx(payload))

        assert result["agp"]["name"] == "scaffolds.agp"
        assert Path(result["agp"]["tmp_path"]).exists()

    def test_a_missing_agp_costs_a_sidecar_not_the_job(self, monkeypatch, tmp_path):
        """The FASTA is the deliverable. The applier reads `agp` with .get(),
        so its absence must be an absent key rather than a failed job."""
        self._run(monkeypatch, tmp_path, agp=False)
        payload = self._payload(tmp_path)

        result = handlers.scaffold_assembly(_ctx(payload))

        assert "agp" not in result
        assert Path(result["output"]["tmp_path"]).exists()

    def test_the_result_carries_the_keys_the_applier_reads(self, monkeypatch, tmp_path):
        """`_apply_scaffold_assembly` returns early without `output` and
        `draft_object_id`, and builds the two-parent provenance from
        `reference_object_id`. Losing the reference there would make the
        scaffolding's central claim unrecoverable."""
        self._run(monkeypatch, tmp_path)
        payload = self._payload(tmp_path)

        result = handlers.scaffold_assembly(_ctx(payload))

        assert result["draft_object_id"] == "64b000000000000000000021"
        assert result["reference_object_id"] == "64b000000000000000000022"
        assert result["output"]["name"] == "scaffolds.fasta"
        assert Path(result["output"]["tmp_path"]).exists()


class TestHandlerRegistration:
    """All three are registered by importing the module for its side effects.

    `handlers.py` imports this module for exactly that reason, so a handler
    that stopped registering would leave jobs of that type sitting unclaimed
    with nothing raising anywhere.
    """

    @pytest.mark.parametrize(
        "job_type",
        ["consensus_from_alignment", "polish_assembly", "scaffold_assembly"],
    )
    def test_the_handler_is_registered(self, job_type):
        from app.queue.registry import get_handler

        assert get_handler(job_type) is not None

    @pytest.mark.parametrize(
        "job_type",
        ["consensus_from_alignment", "polish_assembly", "scaffold_assembly"],
    )
    def test_every_slice_has_an_applier(self, job_type):
        """A handler with no applier runs, succeeds, and stores nothing --
        the registry-skip failure mode, one layer up from the roles dict."""
        from app.queue import results

        assert job_type in results._APPLIERS
