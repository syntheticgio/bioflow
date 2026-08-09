import gzip
import threading

import pytest

from app.errors import JobCancelled
from app.models import Compression
from app.pipelines import contamination_stats as cs


def test_known_probes_are_twelve_bases():
    """FastQC matches the first 12bp of each adapter; a longer or shorter
    probe would silently change sensitivity."""
    for name, seq in cs.KNOWN_ADAPTERS:
        assert len(seq) == cs.PROBE_LENGTH, name


def test_build_probes_without_detection_returns_known_set():
    probes = cs.build_probes([])
    assert [p[0] for p in probes] == [n for n, _ in cs.KNOWN_ADAPTERS]


def test_build_probes_appends_detected_sequence():
    probes = cs.build_probes(["TTTTCCCCGGGGAAAA"])
    assert probes[-1] == ("Detected", "TTTTCCCCGGGG")


def test_build_probes_drops_detected_duplicate_of_known_kit():
    """A detected sequence that IS a known kit must not draw a second,
    identical curve on the chart."""
    nextera = dict(cs.KNOWN_ADAPTERS)["Nextera Transposase"]
    probes = cs.build_probes([nextera + "ACGTACGT"])
    assert [p[0] for p in probes] == [n for n, _ in cs.KNOWN_ADAPTERS]


def test_build_probes_ignores_short_or_empty_detection():
    assert cs.build_probes([""]) == cs.build_probes([])
    assert cs.build_probes(["ACGT"]) == cs.build_probes([])
    assert cs.build_probes([None]) == cs.build_probes([])


def test_corrected_count_returns_observations_when_nothing_was_missed():
    """When the dictionary never froze (count_at_limit == total), every
    sequence was seen, so there is nothing to correct."""
    assert cs.get_corrected_count(1000, 1000, 1, 500) == 500


def test_corrected_count_returns_observations_when_no_room_to_hide():
    """If fewer sequences remain than the freeze point, another sequence at
    this level could not have been missed."""
    assert cs.get_corrected_count(900, 1000, 1, 500) == 500


def test_corrected_count_scales_up_when_sequences_were_missed():
    """A dictionary that froze early saw a small slice of the file, so the
    observed count under-counts and the correction must exceed it."""
    corrected = cs.get_corrected_count(1_000, 1_000_000, 1, 100)
    assert corrected > 100


def test_corrected_count_grows_as_freeze_point_shrinks():
    """The earlier the freeze, the more was missed, so the larger the
    correction -- this is the direction that makes the estimate meaningful."""
    early = cs.get_corrected_count(1_000, 1_000_000, 1, 100)
    late = cs.get_corrected_count(500_000, 1_000_000, 1, 100)
    assert early > late


def test_corrected_count_is_near_observed_for_high_duplication():
    """A sequence appearing very often is almost certain to have been caught
    before the freeze, so its count needs little correction."""
    corrected = cs.get_corrected_count(1_000, 1_000_000, 50_000, 10)
    assert 10 <= corrected < 11


@pytest.mark.parametrize(
    "level,expected_slot",
    [
        (1, 0),      # seen once -> first slot
        (9, 8),      # last exact slot
        (10, 9),     # ">10" begins
        (50, 9),     # tempDupSlot 49, still ">10"
        (51, 10),    # tempDupSlot 50 -> ">50"
        (100, 10),
        (101, 11),   # ">100"
        (500, 11),
        (501, 12),   # ">500"
        (1000, 12),
        (1001, 13),  # ">1k"
        (5000, 13),
        (5001, 14),  # ">5k"
        (10000, 14),
        (10001, 15), # ">10k"
        (99999, 15),
    ],
)
def test_slot_boundaries(level, expected_slot):
    """Boundaries are off-by-one traps: FastQC bins on `level - 1`, so the
    ">50" slot actually starts at level 51."""
    assert cs.slot_for_level(level) == expected_slot


def test_slot_labels_match_slot_count():
    assert len(cs.DUPLICATION_LABELS) == cs.DUPLICATION_SLOTS


def test_duplication_tracker_freezes_at_the_unique_limit(monkeypatch):
    """Past the limit, new sequences are dropped but existing ones keep
    counting -- that is what keeps total_count a true whole-file count."""
    monkeypatch.setattr(cs, "OBSERVATION_CUTOFF", 3)
    tracker = cs.DuplicationTracker()

    for seq in ("AAA", "CCC", "GGG"):
        tracker.add(seq)
    tracker.add("TTT")   # dropped: dictionary is frozen
    tracker.add("AAA")   # counted: already present

    assert tracker.total_count == 5
    assert tracker.sequences == {"AAA": 2, "CCC": 1, "GGG": 1}


def test_duplication_tracker_truncates_to_fifty_bases():
    """Reads differing only past 50bp are the same fragment for duplication
    purposes -- this tolerates end-of-read quality decay."""
    tracker = cs.DuplicationTracker()
    tracker.add("A" * 50 + "CCCC")
    tracker.add("A" * 50 + "GGGG")

    assert tracker.sequences == {"A" * 50: 2}


def test_duplication_tracker_records_count_at_unique_limit(monkeypatch):
    monkeypatch.setattr(cs, "OBSERVATION_CUTOFF", 2)
    tracker = cs.DuplicationTracker()

    tracker.add("AAA")
    tracker.add("CCC")   # freezes here, at 2 reads
    tracker.add("GGG")
    tracker.add("TTT")

    assert tracker.count_at_unique_limit == 2
    assert tracker.total_count == 4


def test_duplication_tracker_advances_unique_limit_on_repeat_while_open(monkeypatch):
    """count_at_unique_limit must advance on a repeat too, as long as the
    dictionary hasn't frozen yet -- this is what keeps it a true snapshot of
    total_count rather than only tracking the newest-key case.

    The second half re-adds a key *after* freezing and pins the opposite
    direction: once frozen, a repeat must NOT advance the limit. Without
    this half, deleting or inverting the `if not self._frozen:` guard in
    the existing-key branch would still pass -- the first half alone can't
    tell "always advance" apart from "advance only while open," since the
    dictionary never freezes before its assertions run.
    """
    monkeypatch.setattr(cs, "OBSERVATION_CUTOFF", 3)
    tracker = cs.DuplicationTracker()

    tracker.add("AAA")
    tracker.add("AAA")   # repeat, but dictionary still open (1 distinct key < cutoff of 3)
    tracker.add("CCC")

    assert tracker.count_at_unique_limit == 3
    assert tracker.total_count == 3

    tracker.add("GGG")   # 3rd distinct key -> freezes here, limit advances to 4
    assert tracker.count_at_unique_limit == 4
    assert tracker.total_count == 4

    tracker.add("AAA")   # repeat while frozen: must NOT advance the limit
    assert tracker.count_at_unique_limit == 4
    assert tracker.total_count == 5


def test_duplication_result_on_a_fully_unique_library():
    tracker = cs.DuplicationTracker()
    for i in range(100):
        tracker.add(f"SEQ{i:04d}")

    result = tracker.result()

    assert result["percent_unique"] == pytest.approx(100.0)
    # Everything sits in the "seen once" slot.
    assert result["percentages"][0] == pytest.approx(100.0)
    assert sum(result["percentages"][1:]) == pytest.approx(0.0)


def test_duplication_result_on_a_fully_duplicated_library():
    """One fragment, 100 copies: 1% unique, and all of the library sits in
    the >50 slot (level 100 -> tempDupSlot 99 -> slot 10)."""
    tracker = cs.DuplicationTracker()
    for _ in range(100):
        tracker.add("AAAA")

    result = tracker.result()

    assert result["percent_unique"] == pytest.approx(1.0)
    assert result["percentages"][10] == pytest.approx(100.0)


def test_duplication_result_is_empty_for_no_reads():
    assert cs.DuplicationTracker().result() == {}


def test_adapter_tracker_marks_every_position_from_the_match_onward():
    """The cumulative rule: once a read has run into adapter it stays in
    adapter, which is what makes the plotted curve monotonic."""
    probes = [("Test", "ACGTACGTACGT")]
    tracker = cs.AdapterTracker(probes, max_positions=20)
    tracker.add("TTTTT" + "ACGTACGTACGT")

    counts = tracker.counts["Test"]
    assert counts[:5] == [0, 0, 0, 0, 0]
    assert all(c == 1 for c in counts[5:17])
    # The read is 17bp long; positions past its own length must not be
    # marked, even though max_positions=20 leaves room for them.
    assert all(c == 0 for c in counts[17:20])


def test_adapter_tracker_counts_only_the_earliest_match():
    """A probe occurring twice must not double-count the read."""
    probes = [("Test", "AAAAAAAAAAAA")]
    tracker = cs.AdapterTracker(probes, max_positions=40)
    tracker.add("A" * 12 + "CGT" + "A" * 12)

    assert tracker.counts["Test"][0] == 1
    assert max(tracker.counts["Test"]) == 1


def test_adapter_tracker_result_is_percentage_of_reads():
    probes = [("Test", "ACGTACGTACGT")]
    tracker = cs.AdapterTracker(probes, max_positions=20)
    tracker.add("ACGTACGTACGT")
    tracker.add("TTTTTTTTTTTT")
    tracker.add("TTTTTTTTTTTT")
    tracker.add("TTTTTTTTTTTT")

    result = tracker.result()
    series = result["series"][0]

    assert series["name"] == "Test"
    assert series["values"][0] == pytest.approx(25.0)


def test_adapter_tracker_keeps_all_zero_series():
    """Dropping empty series is the frontend's job -- the facts record what
    was probed for, so 'we looked and found none' stays distinguishable from
    'we never looked'."""
    probes = [("Test", "ACGTACGTACGT")]
    tracker = cs.AdapterTracker(probes, max_positions=10)
    tracker.add("TTTTTTTTTT")

    result = tracker.result()

    assert result["series"][0]["name"] == "Test"
    assert all(v == 0.0 for v in result["series"][0]["values"])


def test_adapter_tracker_result_is_empty_for_no_reads():
    assert cs.AdapterTracker([("Test", "ACGTACGTACGT")]).result() == {}


def test_adapter_tracker_truncates_positions_to_the_cap():
    probes = [("Test", "ACGTACGTACGT")]
    tracker = cs.AdapterTracker(probes, max_positions=5)
    tracker.add("T" * 200)

    assert len(tracker.result()["positions"]) == 5


def test_adapter_tracker_does_not_fill_past_a_short_reads_own_length():
    probes = [("Test", "ACGTACGTACGT")]
    tracker = cs.AdapterTracker(probes, max_positions=50)
    tracker.add("ACGTACGTACGT")          # 12bp, matches at 0
    tracker.add("T" * 40)                 # 40bp, sets longest_read = 40

    result = tracker.result()
    values = result["series"][0]["values"]
    # the 12bp read cannot have "entered adapter" at position 39
    assert values[39] == pytest.approx(0.0)


def _write_fastq(path, reads):
    lines = []
    for i, seq in enumerate(reads):
        lines += [f"@read{i}", seq, "+", "I" * len(seq)]
    path.write_text("\n".join(lines) + "\n")


def test_scan_reports_both_statistics(tmp_path):
    path = tmp_path / "reads.fastq"
    _write_fastq(path, ["ACGT" * 15] * 10)

    facts = cs.scan_contamination(path, Compression.NONE)

    assert facts["qc_duplication_scanned_reads"] == 10
    assert facts["qc_percent_unique"] == pytest.approx(10.0)
    assert facts["qc_duplication_levels"]["labels"][0] == "1"
    assert facts["qc_adapter_content"]["series"]


def test_scan_detects_adapter_read_through(tmp_path):
    """A fragment shorter than the read: the tail is Nextera adapter."""
    path = tmp_path / "reads.fastq"
    _write_fastq(path, ["TTTTTTTT" + "CTGTCTCTTATA"] * 4)

    facts = cs.scan_contamination(path, Compression.NONE)
    series = {s["name"]: s["values"] for s in facts["qc_adapter_content"]["series"]}

    assert series["Nextera Transposase"][8] == pytest.approx(100.0)
    assert series["Nextera Transposase"][0] == pytest.approx(0.0)
    assert all(v == 0.0 for v in series["Illumina Universal"])


def test_scan_includes_detected_adapter_probe(tmp_path):
    path = tmp_path / "reads.fastq"
    _write_fastq(path, ["TTTTCCCCGGGGAAAA"] * 3)

    facts = cs.scan_contamination(
        path, Compression.NONE, detected_adapters=["TTTTCCCCGGGG"]
    )
    names = [s["name"] for s in facts["qc_adapter_content"]["series"]]

    assert "Detected" in names


def test_scan_reads_gzipped_input(tmp_path):
    path = tmp_path / "reads.fastq.gz"
    body = "\n".join(["@r", "ACGT" * 15, "+", "I" * 60]) + "\n"
    path.write_bytes(gzip.compress(body.encode()))

    facts = cs.scan_contamination(path, Compression.GZIP)

    assert facts["qc_duplication_scanned_reads"] == 1


def test_scan_returns_empty_for_an_empty_file(tmp_path):
    path = tmp_path / "empty.fastq"
    path.write_text("")

    assert cs.scan_contamination(path, Compression.NONE) == {}


def test_scan_returns_empty_rather_than_raising_on_unreadable_input(tmp_path):
    """A scan failure must not fail the QC job -- same contract as FastQC's."""
    assert cs.scan_contamination(tmp_path / "missing.fastq", Compression.NONE) == {}


def test_scan_honours_cancellation(tmp_path):
    path = tmp_path / "reads.fastq"
    _write_fastq(path, ["ACGT" * 15] * 50)

    cancel = threading.Event()
    cancel.set()

    with pytest.raises(JobCancelled):
        cs.scan_contamination(
            path, Compression.NONE, cancel_event=cancel, cancel_check_reads=1
        )


def test_compression_of_sniffs_gzip(tmp_path):
    """BGZF is gzip with an extra subfield, and `detect_compression`
    distinguishes them -- both must open with the gzip reader, so the scan
    treats them the same and this test accepts either."""
    path = tmp_path / "reads.fastq.gz"
    path.write_bytes(gzip.compress(b"@r\nACGT\n+\nIIII\n"))

    assert cs.compression_of(path) in (Compression.GZIP, Compression.BGZF)


def test_compression_of_sniffs_plain_text(tmp_path):
    path = tmp_path / "reads.fastq"
    path.write_text("@r\nACGT\n+\nIIII\n")

    assert cs.compression_of(path) == Compression.NONE


def test_compression_of_defaults_to_none_when_unreadable(tmp_path):
    assert cs.compression_of(tmp_path / "missing.fastq") == Compression.NONE
