# CheckM2 arm64 un-skip — implementation plan

Issue: [#784](https://github.com/syntheticgio/bioflow/issues/784)
Status: **BLOCKED — do not implement yet.** Written ahead of the trigger so the
work is mechanical when it fires.

## Trigger

Do not start until this solves cleanly:

```bash
docker run --rm --platform linux/arm64 mambaorg/micromamba:latest \
  micromamba create -y -n t --dry-run -c conda-forge -c bioconda checkm2
```

Last run 2026-08-23: still fails on both branches (`tensorflow =2.17` and
`tensorflow >=2.1.0,<2.6.0`, neither published for `linux-aarch64`, which has
only 2.18.0/2.19.1). Upstream `checkm2.yml` still pins `tensorflow=2.17`;
newest release is 1.1.0 (2025-02-20), last commit 2025-03-20.

tensorflow is the **sole** blocker. The `scikit-learn==0.23.2` claim that once
appeared in the issue and in code comments was stale and is corrected as of
commit `bda86721`; upstream pins `scikit-learn=1.6.1` and `python>3.12`, both
fine on aarch64.

## Gate before touching code

A clean solve is necessary but **not sufficient**. CheckM2 scores by unpickling
a scikit-learn/Keras model, so a solvable env can still load that model wrong
and return plausible numbers instead of erroring — the exact failure the skip
exists to avoid.

So before any edit, run CheckM2's own test run on real arm64 and compare
against a linux-64 control **in the same session**:

```bash
checkm2 testrun
```

Requires the 1.7 GB DIAMOND database (`download_checkm2_db`), which is fetched
at runtime, not baked into the image. Scores must match the control's to
within floating-point noise. **If they diverge at all, stop and re-close the
issue** — a solvable-but-wrong env is strictly worse than the honest skip.

## Changes, once the gate passes

The issue names three sites. Two more exist that it does not name — items 4
and 5 below. Neither fails at import; both go quietly wrong.

1. **`backend/scripts/install-checkm2.sh:48`** — remove the
   `[ "$TARGETARCH" = "arm64" ]` early-exit block (4 lines). Rewrite the header
   comment (lines ~13-40): it is a long, specific argument for *why* the skip
   exists and becomes actively misleading once it is gone. Replace with the
   verification that unblocked it — dates, versions, the testrun comparison.

2. **`backend/app/pipelines/tools.py:968`** — remove the
   `if tool.error and is_arm64():` branch in `checkm2()` (13 lines), leaving
   `return tool`. Rewrite the docstring (lines ~941-965), same reasoning.
   Check whether `is_arm64` is still used in this module: line 729 is
   `polypolish()`, which keeps its own guard, so the import stays.

3. **`backend/tests/pipelines/test_tools.py:325`** — update the comment above
   `"checkm2"` in `test_all_tools_covers_every_probed_binary`. The entry itself
   stays; only the "x86-64 only in practice" caveat goes. The `megahit` comment
   at line 301 explicitly contrasts *against* checkm2 ("unlike checkm2 below"),
   so it must be updated in the same commit or it starts lying.

4. **`backend/tests/services/test_suggestion_service.py:4161`** — NOT in the
   issue's list. `test_unavailable_when_checkm2_is_not_installed` hardcodes the
   arm64 skip message and asserts `"arm64" in card.reason`. It patches the tool
   to unavailable, so it still passes with the guard gone — while asserting a
   message the code can no longer produce. Repoint it at a generic
   not-installed error, keeping the unavailable-card behaviour it actually
   covers. `suggestion_service.py` itself needs no change: it relays
   `tool.error` verbatim and is architecture-agnostic.

5. **`backend/Dockerfile:706-727`** — NOT in the issue's list. The CheckM2
   block's comment says "Deliberately SKIPPED on arm64" and explains the
   tensorflow bind. `TARGETARCH` is already passed through and can stay
   (harmless once the script ignores it); the comment must be rewritten.

## Verification

- `./backend/run-worktree-tests.sh tests/pipelines/test_tools.py tests/services/test_suggestion_service.py -q`
- Full `TestExhaustiveness` class, not just the named test — per CLAUDE.md,
  registry-pair tests must run together.
- `ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e`
- **An arm64 image build**, since items 1 and 5 only take effect there —
  the test suite cannot catch a broken install layer.
- Real-data check: score bins from a #728 run on arm64 and confirm a
  deliberately merged bin still scores high contamination.

## Out of scope

- The stale-pin comment corrections — already landed in `bda86721`.
- `docs/superpowers/specs/2026-08-20-checkm2-bin-qc-design.md` — checked
  2026-08-23, carries no stale pin claim; it lists aarch64 availability as an
  open pre-implementation question and needs no edit.
- Polypolish's separate arm64 guard (`tools.py:729`), unrelated blocker.
