# Phase structure for assembly (Flye)

Closes [#55](https://github.com/syntheticgio/bioflow/issues/55), a follow-up
from [#24](https://github.com/syntheticgio/bioflow/issues/24)'s Task 7.

## The premise in #55 does not hold

#55 says Flye's stage list is "not closed", so there is "no honest
`phase_total` to declare today". That was read off `assembly_runner.py`'s own
docstring rather than off Flye, and Flye disagrees.

`flye/main.py:_create_job_list` (2.9.5-b1801, the version in the image) builds
the **entire** job list at launch, before any stage runs. Seven stage classes
exist -- `configure`, `assembly`, `consensus`, `repeat`, `contigger`,
`polishing`, `finalize` -- and exactly two conditionals affect which are
included:

- `consensus` is skipped when `read_type == "subasm"`.
- `polishing` is skipped when `--iterations 0`.

Both are decided by BioFlow's own command builder, so the sequence is knowable
before the process starts. `subasm` is not among the six modes
`assembler_registry` offers, so it cannot occur here. `iterations` **can** be
0: `assembly_params.MIN_ITERATIONS = 0`, and the dialog exposes it.

For every run BioFlow can launch:

```
configure -> assembly -> consensus -> repeat -> contigger -> [polishing] -> finalize
```

Seven stages, or six when `iterations == 0`.

There is also no `trestle`. `JobTrestle`'s registration is commented out in
2.9.5's `_create_job_list`, and `--trestle` is marked deprecated in `--help`.
`_STAGE_LABELS` carries a `trestle` key for a stage that cannot be emitted --
and because it maps to the same label as `repeat`, it is the sole reason the
label dict has fewer distinct values than keys.

The docstring's *other* argument survives: stages differ in duration by more
than an order of magnitude, so a **percentage** derived from stage position
would be a fabrication. "Step N of M" makes no duration claim, which is why it
is safe where a bar is not.

## Approach

Derive the stage list from params at construction and index into it. Rejected
alternatives:

- **A flat `PHASE_ORDER` constant**, matching `fastp_runner` and
  `align_runner` exactly. Simpler, but declares "of 7" on a run that will
  execute six stages, so the counter reads 6/7 at `contigger` and then jumps
  to 7/7 at `finalize`. Wrong only when `iterations == 0`, but wrong.
- **An index with no total.** `ActivityView.tsx:354` and `JobList.tsx:70`
  both render the counter only when index *and* total are non-null, so this
  displays nothing at all.

## Design

### `flye_stage_order`

A pure function over params in `assembly_runner.py`, mirroring
`_create_job_list`:

```python
_FLYE_STAGES: tuple[str, ...] = (
    "configure", "assembly", "consensus", "repeat",
    "contigger", "polishing", "finalize",
)

def flye_stage_order(params: FlyeParams) -> tuple[str, ...]:
    """The stages this run will actually execute, in order."""
    if params.iterations > 0:
        return _FLYE_STAGES
    return tuple(s for s in _FLYE_STAGES if s != "polishing")
```

A function rather than a module constant because the list is per-run. It stays
a pure function over params, testable without a container or a binary -- the
split the module docstring already commits to.

### `AssemblyProgress`

Gains `stage_order`, and tracks the raw stage name alongside the display
label:

```python
@dataclass
class AssemblyProgress:
    name: str = "flye"
    phase: str = "starting"
    stage_order: tuple[str, ...] = ()
    _stage: str | None = None
```

`stage_order` defaults to `()` so existing construction sites keep today's
behaviour (no step counter) rather than forcing edits to tests that are about
stage-label parsing.

`phase_index` keys on the **raw stage name**, not the display label. Labels
are not unique by construction -- `repeat` and `trestle` collided until
`trestle` is removed below, and a future duplicate would silently return the
first match. The stage name is the identity Flye itself uses, and the same
vocabulary `--resume-from` accepts.

```python
@property
def phase_index(self) -> int | None:
    if self._stage is None or self._stage not in self.stage_order:
        return None
    return self.stage_order.index(self._stage) + 1

@property
def phase_total(self) -> int | None:
    return len(self.stage_order) or None
```

`snapshot()` includes both keys only when `stage_order` is non-empty. This
honours the contract in `executor.py:85`: a parser omits keys it does not
know rather than passing `None`, which `ctx.progress()` would otherwise write
over a value it should have left alone.

### Unknown stages keep today's behaviour

A stage outside `stage_order` -- a future Flye adding one -- leaves `phase`
showing the raw name and `phase_index` at `None`. Both UI call sites require
index *and* total, so the display falls back to the phase name alone: exactly
what ships today, with no stale or invented number. This degradation is the
property that makes deriving a total safe at all, and it is what #55's
"omitted once an unknown stage appears" option was reaching for.

### Call site

`assembly_handlers.py:95`, where `params` is already in scope:

```python
progress = assembly_runner.AssemblyProgress(
    stage_order=assembly_runner.flye_stage_order(params)
)
```

No `isinstance` guard is needed: `build_assembly_command` raises for any
assembler other than Flye several lines earlier, so a non-`FlyeParams` never
reaches this line.

### Cleanups in the code this touches

- **Delete the `trestle` key** from `_STAGE_LABELS`. It maps a stage 2.9.5
  cannot emit.
- **Rewrite the `AssemblyProgress` docstring.** It says "five stages" (there
  are seven) and gives an open-ended stage list as the reason for no phase
  structure. Keep the surviving argument -- no percentage, because stage
  durations differ by orders of magnitude -- and drop the false one.
- **Update `models/job.py:114-118`**, which names `assembly_runner` as the
  runner that "deliberately cannot" declare a phase list. This change makes
  that comment false, and a comment explaining why code looks a certain way
  is the kind that misleads longest.

## Testing

In `backend/tests/pipelines/test_assembly_runner.py`:

- `flye_stage_order` returns seven stages for `iterations=1`; six, without
  `polishing`, for `iterations=0`.
- `phase_index` walks 1 through 7 across a real stage sequence: `configure`
  is 1, `finalize` is 7.
- With `iterations=0`, `finalize` reports 6 of 6 -- the case that decided the
  approach.
- An unrecognized stage sets `phase` to its raw name, leaves `phase_index`
  `None`, and keeps `phase_total` declared.
- A default-constructed `AssemblyProgress()` omits both keys from
  `snapshot()`.
- `set(_STAGE_LABELS) == set(_FLYE_STAGES)`. Two hand-maintained structures
  in parallel, where a member present in one and missing from the other is
  skipped silently rather than raised, is the shape CLAUDE.md flags -- the
  same trap `COMPONENT_ORDER` carried against `COMPONENTS`. This is the
  "genuinely derivable" case in that taxonomy, so the exhaustiveness
  assertion is the right fix rather than a written inclusion rule.

Fixture-level tests are not sufficient on their own here. Per CLAUDE.md, the
verification that counts is a real run: assemble a small read set through
`./ops/worktree-up.sh` and watch the step counter advance on the job card,
rather than trusting hand-built progress objects that already look the way
the code expects.

## Out of scope

- Any percentage for assembly. The duration argument against it is unchanged.
- Other assemblers. `build_assembly_command` raises for anything but Flye
  today; when a second assembler lands it brings its own stage order, and
  `stage_order` is already the seam for that.
