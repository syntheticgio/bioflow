# History Summary Rail Design

## Goal

Restyle the History tab's right-hand summary rail so it matches the approved reference image while preserving all existing provenance-summary behavior.

## Scope

The rail keeps its two existing states. Before generation it shows a stable `Summarize` heading, an italic explanation with a thin cyan left rule, and the compact teal `Generate paragraph` button. After generation it retains that structure and adds a `Methods paragraph` heading, a cyan-ruled prose block, and compact `Copy` and `Regenerate` text actions.

## Implementation

`ProvenanceNarrative.tsx` will preserve the existing query, mutation, unavailable-reason, copy, and responsive layout behavior. It will render stable headings for the invitation and generated paragraph rather than using a single conditional title. Broadsheet-scoped CSS will replace the bordered invitation card with the reference's text-forward rail treatment and give generated prose the matching left rule.

## Verification

The repository has no React component-test harness. Verify the frontend build and manually inspect the History tab in the isolated worktree stack at port 5273, at both wide and narrow panel widths.
