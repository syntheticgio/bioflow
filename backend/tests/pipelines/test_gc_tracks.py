import threading

import pytest
from app.errors import JobCancelled
from app.models import Compression
from app.pipelines import gc_tracks


class TestComputeGcTracks:
    def test_gc_percent_per_window(self, tmp_path):
        p = tmp_path / "a.fasta"
        p.write_text(">c1\n" + ("GC" * 5000) + "\n")  # 100% GC
        out = gc_tracks.compute_gc_tracks(p, Compression.NONE)
        gc_vals = out["contigs"][0]["gc"]
        assert all(v == 100.0 for v in gc_vals if v is not None)

    def test_skew_sign_flips_where_composition_flips(self, tmp_path):
        """(G-C)/(G+C) changing sign is the whole diagnostic — it locates the
        origin of replication. A G/C transposition in the formula still
        produces a plausible-looking ring, so assert the sign, not just the
        magnitude."""
        p = tmp_path / "a.fasta"
        p.write_text(">c1\n" + ("G" * 50_000) + ("C" * 50_000) + "\n")
        out = gc_tracks.compute_gc_tracks(p, Compression.NONE)
        skew = [v for v in out["contigs"][0]["skew"] if v is not None]
        assert skew[0] > 0  # G-rich half
        assert skew[-1] < 0  # C-rich half

    def test_all_n_window_is_null_not_zero(self, tmp_path):
        """Zero GC and 'no sequence here' are different facts. A gap plotted
        as 0% draws a cliff that reads as a real compositional feature."""
        p = tmp_path / "a.fasta"
        p.write_text(">c1\n" + ("N" * 100_000) + "\n")
        out = gc_tracks.compute_gc_tracks(p, Compression.NONE)
        assert all(v is None for v in out["contigs"][0]["gc"])

    def test_lowercase_sequence_counts(self, tmp_path):
        """Soft-masked FASTA is common. A scanner that forgets case silently
        halves GC on a masked genome."""
        p = tmp_path / "a.fasta"
        p.write_text(">c1\n" + ("gc" * 5000) + "\n")
        out = gc_tracks.compute_gc_tracks(p, Compression.NONE)
        gc_vals = [v for v in out["contigs"][0]["gc"] if v is not None]
        assert gc_vals and all(v == 100.0 for v in gc_vals)

    def test_short_contig_gets_fewer_windows_not_tiny_ones(self, tmp_path):
        """A 2kb plasmid divided 500 ways is 4bp per window, where skew is
        noise."""
        p = tmp_path / "a.fasta"
        p.write_text(">small\n" + ("ACGT" * 500) + "\n")  # 2000 bp
        out = gc_tracks.compute_gc_tracks(p, Compression.NONE)
        c = out["contigs"][0]
        assert len(c["gc"]) == 20  # 2000 // 100
        assert c["window_bases"] >= gc_tracks.MIN_WINDOW_BASES

    def test_keeps_longest_contigs_and_flags_partial(self, tmp_path):
        p = tmp_path / "a.fasta"
        body = "".join(f">c{i}\n" + "ACGT" * 250 + "\n" for i in range(60))
        body += ">longest\n" + "ACGT" * 5000 + "\n"
        p.write_text(body)
        out = gc_tracks.compute_gc_tracks(p, Compression.NONE)
        assert len(out["contigs"]) == 50
        assert out["gc_tracks_partial"] is True
        assert any(c["name"] == "longest" for c in out["contigs"])

    def test_cancel_propagates(self, tmp_path):
        p = tmp_path / "a.fasta"
        p.write_text(">c1\n" + ("ACGT" * 500_000) + "\n")  # 2M chars — triggers cancel check
        ev = threading.Event()
        ev.set()
        with pytest.raises(JobCancelled):
            gc_tracks.compute_gc_tracks(p, Compression.NONE, cancel_event=ev)
