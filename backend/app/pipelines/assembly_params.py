"""Per-assembler parameter sets.

The same split as `align_params`: validation here, command construction in
`assembly_runner`. One assembler is installed today, so a single flat class
would work -- it is shaped per-assembler anyway because the second one is
already specced, and a class that has to be split later is one whose fields
have already leaked into a runner.

`genome_size` is the field that does not behave like the others. It is not
passed to Flye at all in the default case (Flye has not required it since
2.8) and exists here for BioFlow's own memory estimate. See `assembly_runner`
for where that asymmetry is enforced.
"""

from dataclasses import dataclass

from app.errors import ValidationError
from app.pipelines.assemblers import Assembler

# Flye's own floor. Fewer than this and the repeat graph has nothing to work
# with; the tool fails late and unhelpfully rather than refusing up front.
MIN_THREADS = 1
MAX_THREADS = 128

# Polishing rounds. 0 is legal and means "skip polishing", which is what you
# want for HiFi input where the reads are already accurate enough that
# polishing costs time for no gain.
MIN_ITERATIONS = 0
MAX_ITERATIONS = 10

# k-mer length. ABySS's own practical range; below 16 the graph is noise and
# above 127 the build is not compiled for it.
MIN_K = 16
MAX_K = 127


def parse_genome_size(value) -> int | None:
    """`4.6m` / `3.1g` / `4600000` -> bases. None when unset.

    Accepts the shorthand Flye's own `--genome-size` takes, because a user who
    knows that flag will type `4.6m` here and a field that rejected it would
    read as broken. Returns bases so everything downstream -- the estimate,
    the stored params -- deals in one unit.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        size = int(value)
    else:
        text = str(value).strip().lower().replace(",", "")
        suffixes = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}
        multiplier = 1
        if text and text[-1] in suffixes:
            multiplier = suffixes[text[-1]]
            text = text[:-1]
        # Two separate failures with one message: `4.6mb` (trailing unit) and
        # `big` (not a number) are both "that is not a size", and telling them
        # apart would be a distinction without a fix.
        try:
            size = int(float(text) * multiplier)
        except ValueError:
            raise ValidationError(
                f"{value!r} is not a genome size. Use a number of bases, or "
                "shorthand like 4.6m or 3.1g."
            ) from None
    if size <= 0:
        raise ValidationError("genome_size must be greater than zero")
    return size


@dataclass
class BaseAssemblyParams:
    """The knobs every assembler here shares."""

    assembler: Assembler
    threads: int = 8
    # Bases. None means "unknown", which is a legitimate and common answer --
    # de novo assembly is what you do when there is no reference to measure
    # against. It costs the memory estimate, not the run.
    genome_size: int | None = None
    # Where the number came from, carried so the dialog and the run's recorded
    # parameters can both say. An inferred number presented as a measured one
    # is the thing this field exists to prevent.
    genome_size_source: str = "unset"  # unset | user | inferred

    def as_dict(self) -> dict:
        return {
            "assembler": self.assembler.value,
            "threads": self.threads,
            "genome_size": self.genome_size,
            "genome_size_source": self.genome_size_source,
        }

    @staticmethod
    def _shared(data: dict) -> dict:
        threads = int(data.get("threads", 8))
        if not MIN_THREADS <= threads <= MAX_THREADS:
            raise ValidationError(
                f"threads must be between {MIN_THREADS} and {MAX_THREADS}"
            )

        genome_size = parse_genome_size(data.get("genome_size"))
        source = data.get("genome_size_source") or (
            "unset" if genome_size is None else "user"
        )
        if source not in ("unset", "user", "inferred"):
            raise ValidationError(f"Unknown genome_size_source {source!r}")
        # A source without a size is a claim about nothing, and would render
        # as "inferred from ..." beside an empty field.
        if genome_size is None and source != "unset":
            raise ValidationError(
                "genome_size_source cannot be set without a genome_size"
            )

        return {
            "threads": threads,
            "genome_size": genome_size,
            "genome_size_source": source,
        }


@dataclass
class FlyeParams(BaseAssemblyParams):
    assembler: Assembler = Assembler.FLYE
    # The input mode, as a ReadChemistry-independent string so the dialog can
    # override what chemistry inferred. Validated against the registry's
    # declared modes rather than a list here, so adding a mode is one edit.
    mode: str = "nano-raw"
    iterations: int = 1

    def as_dict(self) -> dict:
        return {**super().as_dict(), "mode": self.mode, "iterations": self.iterations}

    @classmethod
    def from_dict(cls, data: dict) -> "FlyeParams":
        from app.pipelines import assembler_registry

        mode = data.get("mode") or "nano-raw"
        valid = assembler_registry.modes_for(Assembler.FLYE)
        if mode not in valid:
            raise ValidationError(
                f"Unknown Flye input mode {mode!r}",
                details={"valid": sorted(valid)},
            )

        iterations = int(data.get("iterations", 1))
        if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
            raise ValidationError(
                f"iterations must be between {MIN_ITERATIONS} and {MAX_ITERATIONS}"
            )

        return cls(
            assembler=Assembler.FLYE,
            mode=mode,
            iterations=iterations,
            **cls._shared(data),
        )


@dataclass
class AbyssParams(BaseAssemblyParams):
    """Short-read assembly parameters.

    Only `k` is exposed beyond the shared fields. ABySS's Bloom filter budget
    `B` is mandatory but deliberately *not* a user field: it is derived from
    the memory estimate in `assembly_runner`, so the number the guard used to
    decide the run can proceed is the same number the tool is given. Two
    independent memory figures that are supposed to agree is a bug with a
    delay fuse.
    """

    assembler: Assembler = Assembler.ABYSS
    k: int = 51

    def as_dict(self) -> dict:
        return {**super().as_dict(), "k": self.k}

    @classmethod
    def from_dict(cls, data: dict) -> "AbyssParams":
        k = int(data.get("k", 51))
        if not MIN_K <= k <= MAX_K:
            raise ValidationError(f"k must be between {MIN_K} and {MAX_K}")
        return cls(assembler=Assembler.ABYSS, k=k, **cls._shared(data))


_BY_ASSEMBLER = {Assembler.FLYE: FlyeParams, Assembler.ABYSS: AbyssParams}


def from_dict(data: dict | None) -> BaseAssemblyParams:
    """Dispatch on the `assembler` key, defaulting to the installed one."""
    data = data or {}
    raw = data.get("assembler") or Assembler.FLYE.value
    try:
        assembler = Assembler(raw)
    except ValueError:
        raise ValidationError(
            f"Unknown assembler {raw!r}",
            details={"valid": [a.value for a in Assembler]},
        ) from None

    params_class = _BY_ASSEMBLER.get(assembler)
    # Reachable: `Assembler` declares hifiasm and SPAdes so the registry can
    # describe them, and neither has a params class. The message says what is
    # true -- the name is known, the tool is not here -- rather than "unknown
    # assembler", which would send someone looking for a typo.
    if params_class is None:
        raise ValidationError(
            f"{assembler.value} is not installed in this build",
            details={"assembler": assembler.value},
        )
    return params_class.from_dict(data)
