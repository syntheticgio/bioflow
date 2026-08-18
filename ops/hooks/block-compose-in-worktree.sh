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
# Whether a command *is* a compose invocation is decided by compose_target.py,
# not by a regex here. The regex this replaced matched the phrase anywhere in
# the command string, so it blocked writing a file whose text mentioned
# compose and blocked filing the issue that reported it (#549).
#
# Exit 2 blocks the call and feeds stderr back to the model.
set -uo pipefail

payload="$(cat)"
command="$(printf '%s' "$payload" | jq -r '.tool_input.command // ""')"
cwd="$(printf '%s' "$payload" | jq -r '.cwd // ""')"

[ -n "$command" ] || exit 0
[ -n "$cwd" ] || exit 0

# Not a host compose invocation: a mention in a quoted argument or heredoc
# body, a call addressed to a container, or a deliberately named project.
#
# Only exit 1 -- the helper's considered "no" -- lets the command through. Any
# other status means the helper never answered (no python3, a crash), and the
# guard falls back to the old phrase test rather than waving everything past:
# silently losing the guard is the failure it exists to prevent.
printf '%s' "$command" | python3 "$(dirname "$0")/compose_target.py"
case $? in
  0) ;;
  1) exit 0 ;;
  *) printf '%s' "$command" | grep -Eq 'docker[[:space:]]+compose|docker-compose' || exit 0 ;;
esac

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

If you were only writing *about* compose rather than running it, this is a
misfire -- use the Write tool for the file instead of a shell heredoc, and
please report it.
EOF
exit 2
