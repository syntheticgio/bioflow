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
