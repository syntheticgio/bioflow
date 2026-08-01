#!/usr/bin/env bash
# PreToolUse(Bash) guard: refuse `docker compose` when the working directory is
# a git worktree rather than the main checkout.
#
# The failure this prevents is silent, which is the whole reason it is worth a
# hook rather than a note. docker-compose.yml pins `name: biopipe` and the
# override's bind mounts are relative, so `docker compose up` from a worktree
# recreates *the* stack pointing at that worktree -- port 5173 quietly starts
# serving a branch. `docker compose exec api pytest` has the mirror-image
# problem: the api container mounts the main checkout, so a worktree's tests
# silently describe main's code. Neither reports an error.
#
# An explicit project name (-p, --project-name, COMPOSE_PROJECT_NAME=) means
# the caller has already thought about which stack they are addressing, so it
# passes through. That is also what makes ops/worktree-up.sh's own compose
# calls work.
#
# Exit 2 blocks the call and feeds stderr back to the model.
set -uo pipefail

payload="$(cat)"
command="$(printf '%s' "$payload" | jq -r '.tool_input.command // ""')"
cwd="$(printf '%s' "$payload" | jq -r '.cwd // ""')"

[ -n "$command" ] || exit 0
[ -n "$cwd" ] || exit 0

# Not a compose invocation at all.
printf '%s' "$command" | grep -Eq '(^|[;&|[:space:]])docker[[:space:]]+compose([[:space:]]|$)|(^|[;&|[:space:]])docker-compose([[:space:]]|$)' || exit 0

# Deliberately addressed to a named project; the caller knows which stack.
printf '%s' "$command" | grep -Eq 'COMPOSE_PROJECT_NAME=|[[:space:]]-p[[:space:]]|--project-name' && exit 0

toplevel="$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null)" || exit 0
common="$(git -C "$cwd" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || exit 0
main_root="$(dirname "$common")"

# Main checkout: nothing to guard.
[ "$toplevel" = "$main_root" ] && exit 0

cat >&2 <<EOF
Blocked: this is a git worktree ($toplevel), not the main checkout ($main_root).

Compose would not create a second stack here. docker-compose.yml pins
'name: biopipe' and the override's bind mounts are relative paths, so this
would recreate *the* stack with its source pointing at this worktree -- port
5173 would start serving this branch, silently.

Use one of these instead:

  ./ops/worktree-up.sh            start a separate stack for this worktree
                                  (ports 5273 / 8100, its own database)
  ./backend/run-worktree-tests.sh tests/ -q
                                  run this worktree's pytest suite

To act on the main stack, run docker compose from $main_root.
If you really mean to address a specific project from here, name it
explicitly (-p <project>) and this guard will step aside.
EOF
exit 2
