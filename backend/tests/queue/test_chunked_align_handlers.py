"""What the chunked merge handler actually runs, and what it refuses to.

`merge_chunked_buckets` is the last step of a chunked alignment: it takes the
per-bucket BAMs, concatenates them with `samtools merge`, re-sorts the result,
and reports flagstat. Everything downstream treats its output as an ordinary
alignment.

The assertions are on the argv the handler issues rather than on a real merge,
so they hold without samtools installed -- and they cover the failure this
module shipped with: `align_runner.build_sort_command` did not exist, so every
merge job raised AttributeError after a successful `samtools merge`, wasting the
whole alignment at the last step. Nothing caught it because nothing imported
this module in a test.

The re-sort is not optional and is the reason a bare `samtools merge` is not
enough: merge preserves each input's ordering rather than producing a globally
coordinate-sorted file, and `index_bam` -- chained immediately by the applier --
requires coordinate order.
"""

from pathlib import Path

import pytest

from app.errors import RetryableError
from app.pipelines import align_runner
from app.pipelines.tools import Tool
from app.queue import chunked_align_handlers as mod
from app.queue.registry import JobContext

FLAGSTAT = """100 + 0 in total (QC-passed reads + QC-failed reads)
90 + 0 mapped (90.00% : N/A)
80 + 0 properly paired (80.00% : N/A)
4 + 0 duplicates
"""


class _Ctx(JobContext):
    """Only the fields merge_chunked_buckets touches; progress is a sink."""

    def __init__(self, payload):
        self.job_id = "job584"
        self.payload = payload
        self.epoch = 1
        self.attempts = 1
        self.owner = "tester"

    def progress(self, **kwargs):
        return None


@pytest.fixture
def run_merge(monkeypatch, tmp_path):
    """Run merge_chunked_buckets, capturing argv instead of spawning samtools."""
    calls: list[list[str]] = []
    exit_codes: dict[str, int] = {}

    samtools = Tool(name="samtools", path="/usr/bin/samtools", version="1.20")
    monkeypatch.setattr(mod.tools, "samtools", lambda: samtools)
    # Redirects logs_dir and tmp_dir, which are read-only properties derived from it.
    monkeypatch.setattr(
        type(mod.settings), "bioinfo_home", property(lambda _: tmp_path), raising=False
    )

    def fake_run_subprocess(ctx, cmd, log_path=None, **kwargs):
        argv = [str(c) for c in cmd]
        calls.append(argv)
        # The handler unlinks the unsorted BAM after sorting; create what it
        # expects so the run reaches flagstat rather than dying on a stat.
        for i, tok in enumerate(argv):
            if tok == "-o" and i + 1 < len(argv):
                Path(argv[i + 1]).parent.mkdir(parents=True, exist_ok=True)
                Path(argv[i + 1]).touch()
        if "merge" in argv:
            Path(argv[argv.index("merge") + 3]).touch()
            return exit_codes.get("merge", 0)
        return exit_codes.get("sort", 0)

    def fake_private_run(ctx, cmd, log_path, capture_stdout=False):
        calls.append([str(c) for c in cmd])
        return FLAGSTAT if capture_stdout else 0

    monkeypatch.setattr(mod, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(mod, "_run_subprocess", fake_private_run)

    def _run(payload=None, **codes):
        exit_codes.update(codes)
        full = {
            "bucket_bam_paths": [str(tmp_path / f"b{i}.bam") for i in range(3)],
            "output_name": "sample.bam",
            "workdir": str(tmp_path / "work"),
            "bucket_count": 3,
            "project_id": "p1",
            "reference_id": "r1",
            "reads_object_id": "o1",
            "aligner": "bwa-mem2",
        }
        full.update(payload or {})
        return mod.merge_chunked_buckets(_Ctx(full)), calls

    return _run


class TestTheMergeRunsEndToEnd:
    def test_the_handler_completes_without_an_attribute_error(self, run_merge):
        """The regression this file exists for: build_sort_command lived in
        ivar_runner, not align_runner, so this raised before samtools sort ever
        ran -- after the merge had already succeeded."""
        result, _ = run_merge()
        assert result["output_path"].endswith("sample.bam")

    def test_every_bucket_bam_reaches_the_merge_command(self, run_merge, tmp_path):
        """A bucket dropped between the payload and the argv is the same silent
        partial success `_apply_align_reads_chunked` guards against."""
        _, calls = run_merge()
        merge_cmd = next(c for c in calls if "merge" in c)
        for i in range(3):
            assert str(tmp_path / f"b{i}.bam") in merge_cmd

    def test_the_merged_bam_is_re_sorted(self, run_merge):
        """merge preserves per-input order; index_bam needs coordinate order."""
        _, calls = run_merge()
        assert any("sort" in c for c in calls)

    def test_the_sort_reads_the_unsorted_merge_output(self, run_merge):
        """The sort must consume what merge wrote, not the buckets again."""
        _, calls = run_merge()
        merge_cmd = next(c for c in calls if "merge" in c)
        sort_cmd = next(c for c in calls if "sort" in c)
        unsorted = merge_cmd[merge_cmd.index("merge") + 3]
        assert unsorted.endswith(".unsorted.bam")
        assert sort_cmd[-1] == unsorted

    def test_flagstat_is_parsed_onto_the_result(self, run_merge):
        """The applier stores these on the object; a merged BAM reports its own
        numbers rather than any single bucket's."""
        result, _ = run_merge()
        assert result["flagstat"]["total_reads"] == 100
        assert result["flagstat"]["mapped_reads"] == 90

    def test_the_result_carries_the_bucket_count_forward(self, run_merge):
        """apply_chunked_alignment writes this into the object's facts."""
        result, _ = run_merge()
        assert result["bucket_count"] == 3


class TestAFailedStepDoesNotProduceAnOutput:
    def test_a_failed_merge_raises(self, run_merge):
        with pytest.raises(RetryableError, match="merge exited"):
            run_merge(merge=1)

    def test_a_failed_sort_raises(self, run_merge):
        """An unsorted BAM would index-fail later; better to fail here, where
        the log still says which step went wrong."""
        with pytest.raises(RetryableError, match="sort exited"):
            run_merge(sort=1)


class TestSortCommand:
    def test_the_sort_matches_the_single_shot_alignment_flags(self):
        """Chunked and single-shot produce the same artifact, so they must sort
        with the same memory and thread budget -- a divergence here would make
        one path quietly slower or hungrier than the user configured."""
        argv = align_runner.build_sort_command(
            samtools_path="/usr/bin/samtools",
            bam=Path("/t/in.bam"),
            output=Path("/t/out.bam"),
            threads=4,
            sort_memory_mb=1024,
        )
        assert argv[:2] == ["/usr/bin/samtools", "sort"]
        assert argv[argv.index("-@") + 1] == "3"
        assert argv[argv.index("-m") + 1] == "1024M"
        assert argv[argv.index("-o") + 1] == "/t/out.bam"
        assert argv[-1] == "/t/in.bam"

    def test_a_single_thread_request_never_asks_for_zero(self):
        """`-@ 0` is samtools' own default rather than an error, but the align
        path already clamps to 1; matching it keeps the two readable together."""
        argv = align_runner.build_sort_command(
            samtools_path="/usr/bin/samtools",
            bam=Path("/t/in.bam"),
            output=Path("/t/out.bam"),
            threads=1,
            sort_memory_mb=768,
        )
        assert argv[argv.index("-@") + 1] == "1"

    def test_spill_files_stay_in_the_job_scratch_when_asked(self):
        """Same reason the align pipe passes -T: a crashed sort otherwise
        leaves temp files in the system tmp dir for nobody to reap."""
        argv = align_runner.build_sort_command(
            samtools_path="/usr/bin/samtools",
            bam=Path("/t/in.bam"),
            output=Path("/t/out.bam"),
            threads=2,
            sort_memory_mb=512,
            tmp_prefix=Path("/scratch/sort"),
        )
        assert argv[argv.index("-T") + 1] == "/scratch/sort"
