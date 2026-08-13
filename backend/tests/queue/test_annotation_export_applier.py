"""The applier that turns an export job into an object.

The registry assertion is the point of this file. _APPLIERS silently skips
unknown job types, so a missing entry means the job succeeds and nothing is
ever created -- no error, no log, no object.
"""

from app.queue.results import _APPLIERS


class TestApplierIsRegistered:
    def test_export_job_type_has_an_applier(self):
        assert "annotation_subset_export" in _APPLIERS

    def test_the_applier_is_callable(self):
        assert callable(_APPLIERS["annotation_subset_export"])
