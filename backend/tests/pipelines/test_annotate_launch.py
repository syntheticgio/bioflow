"""Wiring for the annotation run.

The handler shells out and is verified manually against real data; what is
worth testing here is that it refuses a payload missing an input, since a
missing one otherwise fails thirty seconds into a job rather than at launch.
"""

import pytest

from app.errors import PermanentError
from app.queue import registry, results, variant_handlers
from app.queue.registry import JobContext


def _ctx(payload: dict) -> JobContext:
    return JobContext(job_id="job-1", payload=payload, epoch=1, attempts=1)


class TestAnnotateVariantsRegistered:
    # The specific omission that would otherwise ship silently green: without
    # an _APPLIERS entry, annotate_variants runs, the job goes green, and the
    # produced VCF just sits in tmp/annotate/<job_id>/ until the scratch
    # reaper deletes it -- nothing is ever ingested or shown in the UI.
    def test_has_a_result_applier(self):
        assert "annotate_variants" in results._APPLIERS

    # The name being registered is not enough -- it has to be registered to
    # the *handler*. A helper defined between the decorator and its function
    # silently captures the decorator instead, and everything still imports,
    # every payload test still passes, and the worker still logs the name in
    # handlers_loaded. The job then runs the helper, which returns a closure,
    # and the applier dies on `'function' object has no attribute 'get'`.
    # That is exactly what happened here; this pins it.
    def test_the_name_is_registered_to_the_handler_itself(self):
        spec = registry.get_handler("annotate_variants")
        assert spec is not None
        assert spec.fn is variant_handlers.annotate_variants


class TestAnnotateVariantsPayload:
    def test_requires_an_object_id(self):
        with pytest.raises(PermanentError, match="object_id"):
            variant_handlers.annotate_variants(_ctx({}))

    def test_requires_a_reference(self):
        with pytest.raises(PermanentError, match="reference"):
            variant_handlers.annotate_variants(_ctx({"object_id": "abc"}))

    def test_requires_an_annotation(self):
        with pytest.raises(PermanentError, match="annotation"):
            variant_handlers.annotate_variants(
                _ctx({"object_id": "abc", "reference_sha256": "d" * 64})
            )
