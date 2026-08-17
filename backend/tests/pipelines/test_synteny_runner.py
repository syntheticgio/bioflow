from pathlib import Path

from app.pipelines import ragtag_runner, synteny_runner

MIN = synteny_runner.MIN_SEGMENT_LENGTH
MAX = synteny_runner.MAX_SYNTENY_SEGMENTS


def _paf(query, qs, qe, strand, target, ts, te, qlen=100_000, tlen=100_000):
    return "\t".join(
        [query, str(qlen), str(qs), str(qe), strand,
         target, str(tlen), str(ts), str(te), "500", "500", "60"]
    )


def test_command_uses_preset_for_divergence():
    cmd = synteny_runner.build_synteny_command(
        minimap2_path="/usr/bin/minimap2",
        reference=Path("/ref.fa"),
        draft=Path("/draft.fa"),
        divergence=ragtag_runner.Divergence.SAME_GENUS,
        threads=4,
    )
    assert "-x" in cmd and "asm10" in cmd


def test_command_suppresses_secondary_alignments():
    """Secondary alignments render as off-diagonal scatter that reads as a
    translocation. Without this flag the plot invents findings."""
    cmd = synteny_runner.build_synteny_command(
        minimap2_path="/usr/bin/minimap2",
        reference=Path("/ref.fa"),
        draft=Path("/draft.fa"),
        divergence=ragtag_runner.Divergence.SAME_SPECIES,
        threads=4,
    )
    assert "--secondary=no" in cmd


def test_parses_strand_both_ways():
    text = "\n".join([
        _paf("c1", 0, 5000, "+", "chrI", 0, 5000),
        _paf("c2", 0, 5000, "-", "chrI", 9000, 14000),
    ])
    out = synteny_runner.parse_paf(text)
    assert [s[6] for s in out["segments"]] == ["+", "-"]


def test_ignores_trailing_tag_fields():
    line = _paf("c1", 0, 5000, "+", "chrI", 0, 5000) + "\tNM:i:12\ttp:A:P\tcm:i:100"
    out = synteny_runner.parse_paf(line)
    assert len(out["segments"]) == 1


def test_drops_blocks_below_minimum_length():
    text = "\n".join([
        _paf("c1", 0, MIN - 1, "+", "chrI", 0, MIN - 1),
        _paf("c1", 0, MIN + 1, "+", "chrI", 0, MIN + 1),
    ])
    out = synteny_runner.parse_paf(text)
    assert len(out["segments"]) == 1


def test_skips_malformed_lines_without_raising():
    text = "not\tenough\tcolumns\n" + _paf("c1", 0, 5000, "+", "chrI", 0, 5000)
    out = synteny_runner.parse_paf(text)
    assert len(out["segments"]) == 1


def test_cap_keeps_the_longest_not_the_first():
    """PAF is emitted in query order. Keeping the first N would keep everything
    from the first contigs and nothing from the rest -- a positional bias that
    renders as 'the genome aligns only at one end', which looks like a real
    finding. A count-only assertion passes under that bug, so assert on which
    segments survive."""
    short = [_paf(f"c{i}", 0, MIN + 10, "+", "chrI", i * 100, i * 100 + MIN + 10)
             for i in range(MAX)]
    long_one = _paf("zLast", 0, 900_000, "+", "chrI", 0, 900_000)
    out = synteny_runner.parse_paf("\n".join(short + [long_one]))

    assert len(out["segments"]) == MAX
    assert out["synteny_segments_partial"] is True
    assert any(s[3] == "zLast" for s in out["segments"])


def test_no_partial_flag_when_under_cap():
    out = synteny_runner.parse_paf(_paf("c1", 0, 5000, "+", "chrI", 0, 5000))
    assert "synteny_segments_partial" not in out


def test_collects_axis_lengths_from_records():
    """Axes must span the full genome even where nothing aligned -- an
    unaligned chromosome is a finding, and an axis scaled to the data alone
    crops it out of existence."""
    out = synteny_runner.parse_paf(
        _paf("c1", 0, 5000, "+", "chrI", 0, 5000, qlen=812430, tlen=230218)
    )
    assert out["target_lengths"]["chrI"] == 230218
    assert out["query_lengths"]["c1"] == 812430
