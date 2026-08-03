"""Payload sanitization for stored computation records.

An allowlist, not a denylist: these records are meant to be uploadable one
day, and a denylist silently ships every field nobody thought of.
"""

from app.services.params_sanitizer import sanitize


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
