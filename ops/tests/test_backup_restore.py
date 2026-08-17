"""`ops/backup.sh`: the backup, restore, and verify subcommands.

Split by what each test needs. Everything here except the round-trip test is
pure shell logic -- sourced functions, tmp_path fixtures, no Docker -- so it
runs in the `backend-smoke` CI job alongside the other ops tests. The
round-trip test needs a real Mongo and carries the `docker` marker; see the
note above it.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "ops" / "backup.sh"


def sh(script: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    """Runs a bash snippet with the script's functions sourced.

    The script dispatches on $1 and does real work otherwise, so tests source
    only the definitions: everything above the dispatch `case`. Same approach
    as test_worktree_prune.py.
    """
    text = SCRIPT.read_text()
    marker = "# --- dispatch ---"
    assert marker in text, "dispatch marker moved; update this test"
    preamble = text.split(marker)[0]
    preamble = preamble.replace("set -euo pipefail", "set -uo pipefail")

    return subprocess.run(
        ["bash", "-c", preamble + "\n" + script],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **(env or {})},
    )


def test_stamp_is_utc_and_filesystem_safe(tmp_path):
    result = sh("backup_stamp", tmp_path)
    assert result.returncode == 0, result.stderr
    stamp = result.stdout.strip()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{6}Z", stamp), stamp
    assert ":" not in stamp
