"""What of a job payload is safe to keep forever.

This module is a boundary, not ordinary validation, and the distinction is
worth stating because nothing in the code below announces it. Everything
`sanitize` returns is written to `JobTiming.params` (`models/timing.py`) by
`executor._record_timing`, and that corpus is built to be uploaded to an
aggregation server one day -- see
`docs/superpowers/specs/2026-08-03-computation-records-design.md`. A value that
stays on this machine is harmless; a value that gets uploaded cannot be
recalled. That asymmetry is what the rules below are shaped around, and it is
why loosening any of them is widening a boundary rather than fixing a false
positive.

What it defends against is disclosure, not injection. The runners build their
tool invocations from `job.payload` directly, upstream of here and
unsanitized; `sanitize` only ever sees a copy on its way into a record.
Reading this as command-line hardening both over-trusts it and mis-weighs
changes to it.

An allowlist rather than a denylist, deliberately, because a denylist ships
every field nobody remembered to think about. A new payload key defaults to
being dropped, which costs a missing predictor; the reverse defaults to
leaking a path, which cannot be undone once uploaded.

Values are checked as well as keys: a key can be perfectly safe while its
value is a filesystem path.

`machine_profile.py` writes to the same record under the same constraint, and
its `_machine_id` docstring is the worked example of the reasoning -- hash the
MAC address because it is identifying, rather than record the hostname.
"""

# Fields that explain a run's cost without saying anything about the machine
# or the data's provenance.
ALLOWED_KEYS = frozenset(
    {
        "threads",
        "preset",
        "aligner",
        "assembler",
        "trimmer",
        "caller",
        "mode",
        "sort_memory_mb",
        "building_index",
        "min_length",
        "quality_cutoff",
        "kmer_size",
        "paired",
        "layout",
    }
)

# Every value the allowlist admits is a short token -- a preset name, a tool
# name, a mode. Anything longer is a path or free text (a project title, a
# sample description) that happened to land under an allowed key, and that
# leaks whether or not it contains a separator. So the cap is part of the
# boundary, not a storage limit.
MAX_STRING_LENGTH = 64

# Substrings that mark a value as local rather than descriptive. All three
# are load-bearing: `/` and `\` because a path names directories and usually
# a username, and `~` because `~alice` names a person outright while `~/`
# marks the value as meaningful only on the machine that produced it.
#
# The test is `marker in value`, not `value.startswith(marker)`, and that is
# deliberate: `--extra=/data/alice/ref.fa` discloses exactly as much as
# `/data/alice/ref.fa` does. Narrowing this to a prefix check would widen the
# boundary, not tighten a false positive.
PATH_MARKERS = ("/", "\\", "~")


def _is_safe_value(value) -> bool:
    if isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            return False
        return not any(marker in value for marker in PATH_MARKERS)
    # Nested structures can hide anything and are not worth walking.
    return False


def sanitize(payload: dict | None) -> dict:
    """The subset of a payload safe to persist and eventually upload."""
    if not payload:
        return {}
    return {
        key: value
        for key, value in payload.items()
        if key in ALLOWED_KEYS and _is_safe_value(value)
    }
