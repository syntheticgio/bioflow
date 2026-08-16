"""What of a job payload is safe to keep forever.

**This module is a security boundary between user-supplied parameter values
and tool command lines.** The allowlist and path-marker rejection together
prevent a parameter value from being interpreted as a filesystem path or
shell special character by the tool it is passed to.

An allowlist rather than a denylist, deliberately. These records are designed
to be uploadable to an aggregation server later, and a denylist ships every
field nobody remembered to think about. A new payload key defaults to being
dropped, which costs a missing predictor; the reverse defaults to leaking a
path, which cannot be undone once uploaded.

Values are checked as well as keys: a key can be perfectly safe while its
value is a filesystem path.

## Path marker rejection

The markers in `PATH_MARKERS` are checked as a substring match anywhere in
the value, not only at the start. This is deliberate — a value containing a
separator at any position is rejected, not just one that begins with it.
Narrowing this to a prefix check would widen the security boundary.

- `/` — POSIX path separator.
- `\\` — Windows path separator and shell escape character.
- `~` — Shell home-directory expansion.

## Where sanitized values are consumed

- `app/queue/executor.py` — the sanitized payload is merged into the
  subprocess environment when running pipeline tools.
- `app/models/timing.py` — the sanitized payload is stored in computation
  records as a description of the parameters used.
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
