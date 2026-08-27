#!/usr/bin/env bash
# Print the `Closes #NN` lines a release branch's backflow PR needs.
#
# Why this exists: the label workflow marks an issue "fixed on a release
# branch", but the thing that finally *closes* it is GitHub's own auto-close,
# firing on the merge into main -- and that only reads the merging PR's body.
# So the whole scheme rests on the backflow PR carrying every `Closes #NN`
# from every fix that landed on the branch.
#
# Gathering those by hand is the step that will eventually be forgotten, and
# forgetting it fails silently: the merge succeeds, the issues stay open, and
# the only symptom is a backlog that quietly disagrees with the code. That is
# exactly what #936 found -- thirteen issues open with their fixes long since
# written.
#
# The lines come from the merged PRs' bodies rather than from commit messages,
# because the workflow labels from PR bodies too: same source, same set, so a
# labelled issue and a closed issue cannot drift apart.
#
# Usage:
#   ops/backflow-pr-body.sh beta/0.6.0
#   ops/backflow-pr-body.sh beta/0.6.0 --all      # include already-closed
#
# Typical use, writing the PR that merges a release branch back to main:
#   ops/backflow-pr-body.sh beta/0.6.0 >> /tmp/body.md
#   gh pr create --base main --body-file /tmp/body.md

set -euo pipefail

BRANCH="${1:-}"
INCLUDE_CLOSED="${2:-}"

if [[ -z "$BRANCH" ]]; then
    echo "usage: $0 <release-branch> [--all]" >&2
    echo "   e.g. $0 beta/0.6.0" >&2
    exit 2
fi

if ! git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    echo "error: no branch '$BRANCH' on origin" >&2
    exit 1
fi

# --limit is deliberately far above any real release branch's PR count: the
# default of 30 would silently truncate a busy beta, dropping issues from the
# body with nothing to indicate it happened.
# A read loop rather than `mapfile`: macOS ships bash 3.2, which has no
# mapfile, and this repo is developed on macOS.
ISSUES=()
while IFS= read -r n; do
    [[ -n "$n" ]] && ISSUES+=("$n")
done < <(
    gh pr list --state merged --base "$BRANCH" --limit 500 --json body \
        -q '.[].body' \
    | grep -oiE '(close[sd]?|fix(e[sd])?|resolve[sd]?) #[0-9]+' \
    | grep -oE '[0-9]+' \
    | sort -un
)

if [[ ${#ISSUES[@]} -eq 0 ]]; then
    echo "No issues referenced by PRs merged into $BRANCH." >&2
    exit 0
fi

emitted=0
skipped=0
lines=()

for n in "${ISSUES[@]}"; do
    # A PR body can cite a pull request by number as readily as an issue.
    # `gh issue view` refuses a PR number, which is the check we want.
    if ! state=$(gh issue view "$n" --json state -q .state 2>/dev/null); then
        echo "note: #$n is not an issue (or is unreadable); skipping" >&2
        continue
    fi
    if [[ "$state" == "CLOSED" && "$INCLUDE_CLOSED" != "--all" ]]; then
        skipped=$((skipped + 1))
        continue
    fi
    lines+=("Closes #$n")
    emitted=$((emitted + 1))
done

if [[ $emitted -eq 0 ]]; then
    echo "Every issue referenced by $BRANCH is already closed." >&2
    echo "Re-run with --all to list them anyway." >&2
    exit 0
fi

echo "## Issues closed"
echo
echo "Fixed on \`$BRANCH\` and closed by this merge into \`main\`."
echo
printf '%s\n' "${lines[@]}"

echo >&2
echo "$emitted issue(s) listed from $BRANCH." >&2
[[ $skipped -gt 0 ]] && echo "$skipped already closed (omitted; --all to include)." >&2
exit 0
