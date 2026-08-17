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


def test_manifest_row_count_ignores_the_header(tmp_path):
    manifest = tmp_path / "data-manifest.tsv"
    manifest.write_text(
        "blob_id\tsize\tpath\tcontent_sha256\tstate\n"
        "aaa\t10\tab/aaa\tsha-a\tactive\n"
        "bbb\t20\tcd/bbb\tsha-b\tactive\n"
    )
    result = sh(f"manifest_row_count {manifest}", tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"


def test_manifest_row_count_of_a_header_only_file_is_zero(tmp_path):
    manifest = tmp_path / "data-manifest.tsv"
    manifest.write_text("blob_id\tsize\tpath\tcontent_sha256\tstate\n")
    result = sh(f"manifest_row_count {manifest}", tmp_path)
    assert result.stdout.strip() == "0"


def test_key_digest_shows_only_the_last_four(tmp_path):
    result = sh('key_digest "sk-ant-api03-SECRETVALUE-f4a2"', tmp_path)
    assert result.stdout.strip() == "…f4a2"


def test_key_digest_of_an_absent_key_says_so(tmp_path):
    result = sh('key_digest ""', tmp_path)
    assert result.stdout.strip() == "(no key)"


def test_key_digest_never_echoes_the_whole_key(tmp_path):
    secret = "sk-ant-api03-DONOTLEAK-9c1d"
    result = sh(f'key_digest "{secret}"', tmp_path)
    assert "DONOTLEAK" not in result.stdout
    assert "DONOTLEAK" not in result.stderr
