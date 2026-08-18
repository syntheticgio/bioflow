"""#560: a gzipped reference must not reach a builder that cannot read one.

The registry declares which builders accept gzip
(`tests/pipelines/test_aligner_registry.py`) and `build_index_command` knows
how to read from one path while writing under another
(`tests/pipelines/test_align_runner.py`). This file covers the wiring between
them, which is where the bug actually lived: `build_index` decompressed only
for STAR, so `hisat2-build` was handed `GCF_000146045.2_R64_genomic.fna.gz`
directly and exited 1 minutes in, deleting the .ht2 files it had already
written.

The assertions are on the argv the handler issues rather than on a real index
build, so they hold without hisat2-build being installed -- and they fail in
the direction that matters: a compressed path reaching the builder.
"""

import gzip
from pathlib import Path

import pytest

from app.pipelines.aligners import Aligner
from app.queue import align_handlers
from app.queue.registry import JobContext

FASTA = b">chr1\nACGTACGTACGTACGTACGTACGTACGTACGT\n"


class _Ctx(JobContext):
    """Only the fields build_index touches; progress is a no-op sink."""

    def __init__(self, payload):
        self.job_id = "job560"
        self.payload = payload
        self.epoch = 1
        self.attempts = 1
        self.owner = "tester"

    def progress(self, **kwargs):
        return None


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Run build_index far enough to capture the argv it would execute."""
    calls: list[list[str]] = []

    def fake_run_subprocess(ctx, cmd, log_path=None, **kwargs):
        calls.append([str(c) for c in cmd])
        # faidx is the first call and the handler requires its output to
        # exist for STAR's sizing; create it so the run reaches the builder.
        if "faidx" in cmd:
            ref = Path(cmd[-1])
            (ref.parent / f"{ref.name}.fai").write_text("chr1\t32\t6\t32\t33\n")
        return 0

    monkeypatch.setattr(align_handlers, "run_subprocess", fake_run_subprocess)
    return calls


def _gzipped_reference(tmp_path: Path) -> Path:
    blob = tmp_path / "blob.gz"
    with gzip.open(blob, "wb") as f:
        f.write(FASTA)
    return blob


def _run(tmp_path, monkeypatch, captured, aligner: Aligner):
    from app.config import settings

    # Redirects tmp_dir and logs_dir, which are derived from it.
    monkeypatch.setattr(type(settings), "bioinfo_home", property(lambda _: tmp_path), raising=False)
    blob = _gzipped_reference(tmp_path)
    ctx = _Ctx(
        {
            "aligner": aligner.value,
            "reference_path": str(blob),
            "reference_name": "GCF_000146045.2_R64_genomic.fna.gz",
            "threads": 4,
        }
    )
    try:
        align_handlers.build_index(ctx)
    except Exception:
        # The handler continues past the builder into sidecar collection,
        # which needs a real index on disk. The argv is already captured.
        pass
    return captured


class TestHisat2GetsPlainText:
    def test_builder_never_receives_the_compressed_path(self, tmp_path, monkeypatch, captured):
        calls = _run(tmp_path, monkeypatch, captured, Aligner.HISAT2)
        builder = [c for c in calls if "hisat2-build" in c[0]]
        assert builder, f"hisat2-build was never invoked; calls={calls}"

        # argv is [tool, <input it reads>, <basename it writes>].
        read_path = builder[0][1]
        assert not read_path.endswith(".gz"), (
            f"hisat2-build was handed a compressed reference: {read_path}"
        )
        assert Path(read_path).read_bytes() == FASTA

    def test_index_is_still_written_under_the_stored_name(self, tmp_path, monkeypatch, captured):
        """The decompressed copy lives in scratch, but the .ht2 files must
        land beside the materialized reference -- that is where the layout
        looks for them when it collects sidecars."""
        calls = _run(tmp_path, monkeypatch, captured, Aligner.HISAT2)
        builder = [c for c in calls if "hisat2-build" in c[0]][0]
        read_path, basename = builder[1], builder[2]

        assert basename.endswith("GCF_000146045.2_R64_genomic.fna.gz")
        assert Path(basename).parent.name == "ref"
        assert read_path != basename


class TestBuildersThatAcceptGzipAreLeftAlone:
    def test_bowtie2_reads_the_compressed_reference_directly(self, tmp_path, monkeypatch, captured):
        """bowtie2-build does accept gzip, so decompressing for it would be
        pure copying -- a full human genome written twice per build."""
        calls = _run(tmp_path, monkeypatch, captured, Aligner.BOWTIE2)
        builder = [c for c in calls if "bowtie2-build" in c[0]]
        assert builder, f"bowtie2-build was never invoked; calls={calls}"
        assert builder[0][1].endswith(".gz")
