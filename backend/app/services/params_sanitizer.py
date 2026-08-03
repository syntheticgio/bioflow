"""What of a job payload is safe to keep forever.

An allowlist rather than a denylist, deliberately. These records are designed
to be uploadable to an aggregation server later, and a denylist ships every
field nobody remembered to think about. A new payload key defaults to being
dropped, which costs a missing predictor; the reverse defaults to leaking a
path, which cannot be undone once uploaded.

Values are checked as well as keys: a key can be perfectly safe while its
value is a filesystem path.
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

MAX_STRING_LENGTH = 64

# Substrings that mark a value as local rather than descriptive.
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
