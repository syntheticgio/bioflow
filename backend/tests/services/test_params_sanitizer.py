"""Payload sanitization for stored computation records.

An allowlist, not a denylist: these records are meant to be uploadable one
day, and a denylist silently ships every field nobody thought of.
"""

import pytest

from app.services.params_sanitizer import MAX_STRING_LENGTH, PATH_MARKERS, sanitize


class TestAllowlist:
    def test_keeps_tuning_parameters(self):
        """These are the fields that explain a duration, which is the whole
        point of storing params at all."""
        out = sanitize({"threads": 8, "preset": "sr", "aligner": "minimap2"})
        assert out == {"threads": 8, "preset": "sr", "aligner": "minimap2"}

    def test_drops_unknown_keys(self):
        out = sanitize({"threads": 4, "some_future_field": "value"})
        assert out == {"threads": 4}

    def test_drops_paths_and_local_identifiers(self):
        out = sanitize(
            {
                "threads": 4,
                "path": "/Users/alice/data/sample.fastq.gz",
                "output_path": "/data/out.bam",
                "project_name": "Alice's secret project",
                "object_id": "507f1f77bcf86cd799439011",
            }
        )
        assert out == {"threads": 4}

    def test_empty_payload_is_empty_not_none(self):
        assert sanitize({}) == {}
        assert sanitize(None) == {}


class TestValueSafety:
    def test_drops_allowlisted_keys_carrying_path_like_values(self):
        """A key can be safe while its value is not -- an aligner field set to
        a filesystem path should not survive on the strength of its name."""
        out = sanitize({"aligner": "/Users/alice/custom/minimap2", "threads": 4})
        assert out == {"threads": 4}

    def test_keeps_scalar_types_only(self):
        """Nested structures can hide anything; scalars are checkable."""
        out = sanitize({"threads": 4, "preset": {"nested": "/Users/alice"}})
        assert out == {"threads": 4}

    def test_long_strings_are_dropped(self):
        out = sanitize({"preset": "x" * 200})
        assert out == {}

    def test_none_under_an_allowed_key_is_dropped(self):
        """A key present but unset is not a usable predictor sample."""
        out = sanitize({"threads": 4, "aligner": None})
        assert out == {"threads": 4}


class TestPathMarkers:
    """Each marker on its own, so deleting one from the tuple fails loudly.

    Iterating `PATH_MARKERS` rather than listing the three literals means a
    fourth marker is covered the moment it is added -- and, more to the point,
    that dropping one cannot pass on the strength of the other two.
    """

    @pytest.mark.parametrize("marker", PATH_MARKERS)
    def test_each_marker_is_rejected_on_its_own(self, marker):
        assert sanitize({"preset": f"a{marker}b"}) == {}

    @pytest.mark.parametrize("marker", PATH_MARKERS)
    def test_a_marker_is_rejected_anywhere_in_the_value(self, marker):
        """Substring, not prefix. `--extra=/data/alice/ref.fa` discloses as
        much as `/data/alice/ref.fa`, so a trailing or embedded marker is
        rejected too -- narrowing this to a prefix check widens the boundary."""
        for value in (f"{marker}lead", f"mid{marker}dle", f"trail{marker}"):
            assert sanitize({"preset": value}) == {}, value

    def test_a_long_value_is_rejected_without_any_marker(self):
        """The length cap is part of the boundary, not a storage limit: a
        project title carries a name whether or not it holds a separator."""
        assert sanitize({"preset": "x" * (MAX_STRING_LENGTH + 1)}) == {}
        assert sanitize({"preset": "x" * MAX_STRING_LENGTH}) == {
            "preset": "x" * MAX_STRING_LENGTH
        }
