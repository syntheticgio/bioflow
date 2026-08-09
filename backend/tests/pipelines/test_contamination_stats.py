import pytest

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
