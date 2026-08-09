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
