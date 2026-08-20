"""The binning job handler at the seam.

The runner underneath (`metabat_runner`) is pure functions and tested as such
(test_metabat_runner.py). This file exercises the handler: payload validation,
blob resolution, the depth-then-bin subprocess sequence, and the manifest it
hands the applier.

The assertion that matters most here is which depth tool runs. Design decision
B1 turns on MetaBAT2's own summarizer being used rather than this app's
existing mosdepth output, and getting that wrong produces worse bins with no
error anywhere -- so the command shape is pinned, not merely the end result.
"""

from pathlib import Path

import pytest

from app.errors import PermanentError
from app.pipelines import tools
from app.queue import binning_handlers
from app.queue.registry import JobContext

# From a real `jgi_summarize_bam_contig_depths` run on 2026-08-20.
_REAL_DEPTH_TSV = (
    "contigName\tcontigLen\ttotalAvgDepth\taln.bam\taln.bam-var\n"
    "orgA_ctg0\t200000\t30.013\t30.013\t30.5048\n"
    "orgB_ctg0\t200000\t8.00278\t8.00278\t8.0047\n"
)


def _ctx(payload: dict) -> JobContext:
    return JobContext(
        job_id="job-1", payload=payload, epoch=1, attempts=1, owner="local"
    )


def _fake_tool(name: str, version: str) -> tools.Tool:
    return tools.Tool(name=name, path=f"/usr/bin/{name}", version=version)


@pytest.fixture
def metabat_available(monkeypatch):
    """Pin both probes so require() passes whether or not the binaries exist
    in the test image."""
    monkeypatch.setattr(
        binning_handlers.tools, "metabat2", lambda: _fake_tool("metabat2", "2.18")
    )
    monkeypatch.setattr(
        binning_handlers.tools, "jgi_depths", lambda: _fake_tool("jgi_depths", "2.18")
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Send tmp/ and logs/ under the test's own directory so the handler
    cannot write to the host's /data."""
    monkeypatch.setattr(binning_handlers.settings, "bioinfo_home", tmp_path)
    return tmp_path


def _inputs(tmp_path) -> dict:
    contigs = tmp_path / "community.assembly.fasta"
    contigs.write_text(">orgA_ctg0\nACGT\n")
    bam = tmp_path / "reads.bam"
    bam.write_bytes(b"not-a-real-bam")
    bai = tmp_path / "reads.bam.bai"
    bai.write_bytes(b"not-a-real-index")
    return {
        "contigs_id": "asm-1",
        "contigs_name": "community.assembly.fasta",
        "contigs_path": str(contigs),
        "bam_object_id": "bam-1",
        "bam_name": "reads.bam",
        "bam_path": str(bam),
        "bai_path": str(bai),
        "project_id": "proj-1",
    }


def _stub_run(monkeypatch, *, bins=(1, 2), unbinned=None, excluded=()):
    """Stand in for both subprocesses, writing what the real ones write."""
    calls = []

    def fake_run(ctx, cmd, **kw):
        calls.append(cmd)
        if "jgi_summarize_bam_contig_depths" in cmd[0]:
            out = Path(cmd[cmd.index("--outputDepth") + 1])
            out.write_text(_REAL_DEPTH_TSV)
        else:
            prefix = Path(cmd[cmd.index("-o") + 1])
            prefix.parent.mkdir(parents=True, exist_ok=True)
            for index in bins:
                (prefix.parent / f"{prefix.name}.{index}.fa").write_text(
                    f">ctg{index}\n" + "ACGT" * 30 + "\n"
                )
            # MetaBAT2 always writes these two, usually empty.
            for label in ("tooShort", "lowDepth"):
                body = ">x\n" + "ACGT" * 10 + "\n" if label in excluded else ""
                (prefix.parent / f"{prefix.name}.{label}.fa").write_text(body)
            (prefix.parent / f"{prefix.name}.unbinned.fa").write_text(
                unbinned or ""
            )
        Path(kw["log_path"]).write_text("")
        return 0

    monkeypatch.setattr(binning_handlers, "run_subprocess", fake_run)
    return calls


class TestValidation:
    def test_a_missing_contigs_id_is_permanent(self, metabat_available, home):
        with pytest.raises(PermanentError, match="contigs_id"):
            binning_handlers.run_binning(_ctx({}))

    def test_a_missing_contigs_blob_is_permanent(self, metabat_available, home):
        with pytest.raises(PermanentError, match="contigs"):
            binning_handlers.run_binning(_ctx({"contigs_id": "asm-1"}))


class TestTheDepthStep:
    def test_it_runs_metabat2s_own_depth_summarizer(
        self, metabat_available, home, tmp_path, monkeypatch
    ):
        """The B1 guard, asserted on the command rather than the result.

        The tempting shortcut is this app's existing mosdepth output, which
        already carries per-contig mean depth. MetaBAT2 would accept a depth
        file built from those means and bin from it -- worse bins, no error,
        nothing anywhere to say the result was degraded -- so an end-to-end
        assertion would pass either way. What has to be pinned is that the
        variance-carrying summarizer is the thing invoked.
        """
        calls = _stub_run(monkeypatch)
        binning_handlers.run_binning(_ctx(_inputs(tmp_path)))

        depth_cmd = calls[0]
        assert "jgi_summarize_bam_contig_depths" in depth_cmd[0]
        assert "--outputDepth" in depth_cmd
        # And nothing reads a mosdepth report.
        assert not any("mosdepth" in part for part in depth_cmd)

    def test_the_binner_reads_the_depth_file_the_first_step_wrote(
        self, metabat_available, home, tmp_path, monkeypatch
    ):
        calls = _stub_run(monkeypatch)
        binning_handlers.run_binning(_ctx(_inputs(tmp_path)))

        depth_out = calls[0][calls[0].index("--outputDepth") + 1]
        bin_cmd = calls[1]
        assert bin_cmd[bin_cmd.index("-a") + 1] == depth_out

    def test_an_empty_depth_file_names_the_likely_cause(
        self, metabat_available, home, tmp_path, monkeypatch
    ):
        """Exit zero with no depth means the BAM matched none of the contigs --
        an alignment against a different reference. MetaBAT2's own error for
        this says nothing about which input was wrong."""

        def fake_run(ctx, cmd, **kw):
            if "jgi_summarize" in cmd[0]:
                Path(cmd[cmd.index("--outputDepth") + 1]).write_text("")
            Path(kw["log_path"]).write_text("")
            return 0

        monkeypatch.setattr(binning_handlers, "run_subprocess", fake_run)
        with pytest.raises(PermanentError, match="different reference"):
            binning_handlers.run_binning(_ctx(_inputs(tmp_path)))


class TestTheManifest:
    def test_every_bin_is_listed_for_the_applier(
        self, metabat_available, home, tmp_path, monkeypatch
    ):
        _stub_run(monkeypatch, bins=(1, 2, 3))
        out = binning_handlers.run_binning(_ctx(_inputs(tmp_path)))

        assert [b["index"] for b in out["bins"]] == [1, 2, 3]
        assert out["object_id"] == "asm-1"
        assert out["bam_object_id"] == "bam-1"

    def test_bins_are_named_after_their_assembly(
        self, metabat_available, home, tmp_path, monkeypatch
    ):
        """A project binning two communities would otherwise hold two sets of
        identically-named MAGs. Zero-padded so a listing sorts 2 before 10."""
        _stub_run(monkeypatch, bins=(2, 10))
        out = binning_handlers.run_binning(_ctx(_inputs(tmp_path)))

        names = [b["name"] for b in out["bins"]]
        assert names == [
            "community.assembly.bin.002.fasta",
            "community.assembly.bin.010.fasta",
        ]

    def test_the_unbinned_contigs_ride_along_when_present(
        self, metabat_available, home, tmp_path, monkeypatch
    ):
        _stub_run(monkeypatch, unbinned=">leftover\n" + "ACGT" * 25 + "\n")
        out = binning_handlers.run_binning(_ctx(_inputs(tmp_path)))

        assert out["unbinned"]["name"] == "community.assembly.unbinned.fasta"
        assert out["unbinned"]["contig_count"] == 1
        assert out["binning_facts"]["binning_unbinned_bases"] == 100

    def test_an_empty_unbinned_file_is_not_offered_as_an_object(
        self, metabat_available, home, tmp_path, monkeypatch
    ):
        """MetaBAT2 writes a zero-byte unbinned.fa when everything was placed;
        ingesting it would put an empty, unopenable object in the project."""
        _stub_run(monkeypatch)
        out = binning_handlers.run_binning(_ctx(_inputs(tmp_path)))
        assert out["unbinned"] is None

    def test_excluded_contigs_are_counted_but_not_ingested(
        self, metabat_available, home, tmp_path, monkeypatch
    ):
        """A contig below --minContig was never a binning candidate, so it is
        reported separately from the unbinned fraction rather than folded into
        it -- which would overstate how unresolvable the community is."""
        _stub_run(monkeypatch, excluded=("tooShort",))
        out = binning_handlers.run_binning(_ctx(_inputs(tmp_path)))

        facts = out["binning_facts"]
        assert facts["binning_tooshort_bases"] == 40
        assert facts["binning_unbinned_bases"] == 0


class TestOutcomesThatAreNotCrashes:
    def test_no_bins_at_all_explains_the_lever_that_changes_it(
        self, metabat_available, home, tmp_path, monkeypatch
    ):
        """A low-diversity or shallow sample can leave MetaBAT2 with nothing
        clearing its cluster-size floor. That is a real outcome, and the error
        should name what the user can change."""
        _stub_run(monkeypatch, bins=())
        with pytest.raises(PermanentError, match="minimum contig length"):
            binning_handlers.run_binning(_ctx(_inputs(tmp_path)))

    def test_over_the_cap_fails_before_anything_is_handed_back(
        self, metabat_available, home, tmp_path, monkeypatch
    ):
        """Refuses rather than truncating (B4/R5). The manifest is never
        returned, so the applier never sees a partial set."""
        monkeypatch.setattr(binning_handlers.settings, "metagenome_bin_cap", 2)
        _stub_run(monkeypatch, bins=(1, 2, 3))

        with pytest.raises(PermanentError) as exc:
            binning_handlers.run_binning(_ctx(_inputs(tmp_path)))
        assert "3" in str(exc.value) and "2" in str(exc.value)
