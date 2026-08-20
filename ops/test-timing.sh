#!/usr/bin/env bash
# Interactive TUI: pick a test set, run it in isolation, report wall time and
# the 5 slowest individual tests. Backend and frontend runs reuse the same
# isolation this repo already has (run-worktree-tests.sh / make test, and the
# web container) rather than inventing a new one.
#
# Usage: ./ops/test-timing.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HISTORY_FILE="$REPO_ROOT/ops/.test-timing-history.jsonl"

# --- TUI menu -----------------------------------------------------------
#
# No external deps (dialog/fzf aren't guaranteed installed on a bare dev
# machine), so the menu is a small arrow-key reader over raw escape
# sequences. Up/Down or j/k move, Enter selects, q/Ctrl-C quits.
MENU_OPTIONS=("Backend (pytest)" "Frontend (vitest)" "Ruff lint" "All")

select_menu() {
  local selected=0
  local key esc
  local n=${#MENU_OPTIONS[@]}

  # Draw once, then redraw in place using cursor-up rather than clearing the
  # whole screen, so a resize or scrollback isn't fought.
  draw() {
    local i
    for ((i = 0; i < n; i++)); do
      if [ "$i" -eq "$selected" ]; then
        printf '  \033[1;36m> %s\033[0m\n' "${MENU_OPTIONS[$i]}"
      else
        printf '    %s\n' "${MENU_OPTIONS[$i]}"
      fi
    done
  }

  printf 'Select a test set (arrows/jk, enter to run, q to quit):\n\n'
  draw

  while true; do
    IFS= read -rsn1 key
    if [ "$key" = $'\x1b' ]; then
      # -t wants a whole second on bash 3.2 (macOS's default /bin/bash), which
      # doesn't accept fractional timeouts. A real arrow key's escape sequence
      # arrives as one buffered chunk, so this never waits the full second --
      # it only bounds a bare Escape keypress, which has no follow-up bytes.
      read -rsn2 -t 1 esc || true
      key="$key$esc"
    fi
    case "$key" in
      $'\x1b[A' | k) ((selected > 0)) && ((selected--)) ;;
      $'\x1b[B' | j) ((selected < n - 1)) && ((selected++)) ;;
      "") break ;; # Enter
      q | $'\x03') echo "cancelled" >&2; exit 130 ;;
    esac
    printf '\033[%dA' "$n"
    draw
  done

  printf '\n'
  echo "$selected"
}

# --- helpers --------------------------------------------------------------

in_worktree() {
  # A worktree's git-dir lives under the main repo's .git/worktrees/, not at
  # <repo>/.git itself.
  local common_dir
  common_dir="$(git -C "$REPO_ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
  local git_dir
  git_dir="$(git -C "$REPO_ROOT" rev-parse --git-dir 2>/dev/null || true)"
  [ -n "$common_dir" ] && [ "$common_dir" != "$git_dir" ]
}

# Seconds elapsed between two `date +%s.%N` readings, formatted to 3 decimal
# places with a leading zero -- `bc` prints ".08" rather than "0.08".
elapsed_seconds() {
  python3 -c "print(f'{$2 - $1:.3f}')"
}

record_history() {
  local suite="$1" wall_seconds="$2" top5_json="$3"
  mkdir -p "$(dirname "$HISTORY_FILE")"
  printf '{"suite":"%s","timestamp":"%s","wall_seconds":%s,"top5":%s}\n' \
    "$suite" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$wall_seconds" "$top5_json" \
    >>"$HISTORY_FILE"
}

print_trend() {
  local suite="$1"
  [ -f "$HISTORY_FILE" ] || return 0
  local times
  times="$(grep "\"suite\":\"$suite\"" "$HISTORY_FILE" | tail -3 \
    | sed -n 's/.*"wall_seconds":\([0-9.]*\).*/\1/p' | paste -sd, -)"
  [ -n "$times" ] && echo "  last runs (s): $times"
}

# Parses pytest's "N.NNs call  path::test" duration lines, prints a human
# table to stdout, and writes a JSON array of the top 5 to $2.
report_pytest_durations() {
  local log_file="$1" json_out="$2"
  local lines
  lines="$(grep -E '^[0-9]+\.[0-9]+s (call|setup|teardown) ' "$log_file" | head -5 || true)"
  if [ -z "$lines" ]; then
    echo "  (no per-test durations captured)"
    echo "[]" >"$json_out"
    return
  fi
  echo "Top 5 slowest tests:"
  while IFS= read -r prefix_line; do
    echo "  $prefix_line"
  done <<<"$lines"
  {
    echo -n '['
    local first=1
    while IFS= read -r line; do
      local dur name
      dur="$(echo "$line" | sed -E 's/^([0-9]+\.[0-9]+)s.*/\1/')"
      name="$(echo "$line" | sed -E 's/^[0-9]+\.[0-9]+s [a-z]+ +//')"
      [ "$first" -eq 1 ] || echo -n ','
      first=0
      printf '{"seconds":%s,"name":%s}' "$dur" "$(printf '%s' "$name" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().rstrip()))')"
    done <<<"$lines"
    echo ']'
  } >"$json_out"
}

# Parses vitest's verbose-reporter "  <glyph> name (NNNms)" lines the same
# way, writing the top-5 JSON array to $2. Extraction is done in awk/python
# rather than sed, since the pass/fail glyph is multi-byte UTF-8 and sed
# bracket-expressions mishandle it under an unset locale.
report_vitest_durations() {
  local log_file="$1" json_out="$2"
  local lines
  lines="$(grep -E '\([0-9]+ms\)[[:space:]]*$' "$log_file" \
    | awk 'match($0, /\(([0-9]+)ms\)[[:space:]]*$/) {
        ms = substr($0, RSTART+1, RLENGTH-4);
        name = substr($0, 1, RSTART-1);
        sub(/[[:space:]]+$/, "", name);
        sub(/^[[:space:]]*[^ ]*[[:space:]]+/, "", name);
        print ms "\t" name;
      }' \
    | sort -rn -t $'\t' -k1,1 | head -5 || true)"
  if [ -z "$lines" ]; then
    echo "  (no per-test durations captured)"
    echo "[]" >"$json_out"
    return
  fi
  echo "Top 5 slowest tests:"
  echo "$lines" | awk -F'\t' '{printf "  %s (%sms)\n", $2, $1}'
  {
    echo -n '['
    local first=1
    while IFS=$'\t' read -r ms name; do
      [ "$first" -eq 1 ] || echo -n ','
      first=0
      printf '{"seconds":%s,"name":%s}' "$(python3 -c "print($ms/1000)")" \
        "$(printf '%s' "$name" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().rstrip()))')"
    done <<<"$lines"
    echo ']'
  } >"$json_out"
}

# --- suite runners ----------------------------------------------------------

run_backend() {
  echo "=== Backend (pytest) ==="
  local workers="${PYTEST_WORKERS:-8}"
  local log_file
  log_file="$(mktemp)"
  local start end wall

  start=$(date +%s.%N)
  if in_worktree; then
    echo "Worktree detected -- running via run-worktree-tests.sh (isolated Mongo + this worktree's source)."
    "$REPO_ROOT/backend/run-worktree-tests.sh" -m "not heavy" -n "$workers" --dist loadgroup \
      --durations=0 -v 2>&1 | tee "$log_file"
  else
    echo "Running via the running stack's api container (docker compose exec)."
    docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.override.yml" \
      exec -T api pytest -m "not heavy" -n "$workers" --dist loadgroup --durations=0 -v \
      2>&1 | tee "$log_file"
  fi
  end=$(date +%s.%N)
  wall=$(elapsed_seconds "$start" "$end")

  echo
  echo "Wall time: ${wall}s (PYTEST_WORKERS=$workers)"
  local json_file
  json_file="$(mktemp)"
  report_pytest_durations "$log_file" "$json_file"
  record_history "backend" "$wall" "$(cat "$json_file")"
  print_trend "backend"
  rm -f "$log_file" "$json_file"
}

run_frontend() {
  echo "=== Frontend (vitest) ==="
  local log_file
  log_file="$(mktemp)"
  local start end wall

  start=$(date +%s.%N)
  if docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.override.yml" \
      config --services 2>/dev/null | grep -q '^web$'; then
    echo "Running in the web container (docker compose run, isolated from the host node_modules)."
    docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.override.yml" \
      run --rm -T web npx vitest run --reporter=verbose 2>&1 | tee "$log_file"
  else
    echo "web service unavailable -- falling back to a local run in frontend/."
    (cd "$REPO_ROOT/frontend" && npx vitest run --reporter=verbose) 2>&1 | tee "$log_file"
  fi
  end=$(date +%s.%N)
  wall=$(elapsed_seconds "$start" "$end")

  echo
  echo "Wall time: ${wall}s"
  local json_file
  json_file="$(mktemp)"
  report_vitest_durations "$log_file" "$json_file"
  record_history "frontend" "$wall" "$(cat "$json_file")"
  print_trend "frontend"
  rm -f "$log_file" "$json_file"
}

run_ruff() {
  echo "=== Ruff lint ==="
  local start end wall
  start=$(date +%s.%N)
  ruff check --config "$REPO_ROOT/backend/pyproject.toml" \
    "$REPO_ROOT/backend/app" "$REPO_ROOT/backend/tests" "$REPO_ROOT/ops" "$REPO_ROOT/e2e" || true
  end=$(date +%s.%N)
  wall=$(elapsed_seconds "$start" "$end")

  echo
  echo "Wall time: ${wall}s"
  record_history "ruff" "$wall" "[]"
  print_trend "ruff"
}

# --- main -------------------------------------------------------------------

main() {
  local choice
  choice="$(select_menu)"

  case "$choice" in
    0) run_backend ;;
    1) run_frontend ;;
    2) run_ruff ;;
    3) run_backend; echo; run_frontend; echo; run_ruff ;;
  esac
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
