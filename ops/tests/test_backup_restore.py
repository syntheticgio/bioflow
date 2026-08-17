"""`ops/backup.sh`: the backup, restore, and verify subcommands.

Split by what each test needs. Everything here except the round-trip test is
pure shell logic -- sourced functions, tmp_path fixtures, no Docker -- so it
runs in the `backend-smoke` CI job alongside the other ops tests. The
round-trip test needs a real Mongo and carries the `docker` marker; see the
note above it.
"""

import json
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


def test_counts_tsv_becomes_a_json_object(tmp_path):
    counts = tmp_path / "counts.tsv"
    counts.write_text("projects\t3\nobjects\t128\njob_timings\t9\n")
    result = sh(f"counts_to_json {counts}", tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"projects": 3, "objects": 128, "job_timings": 9}


def test_counts_to_json_of_nothing_is_an_empty_object(tmp_path):
    counts = tmp_path / "counts.tsv"
    counts.write_text("")
    result = sh(f"counts_to_json {counts}", tmp_path)
    assert json.loads(result.stdout) == {}


def test_version_matches_on_identical_versions(tmp_path):
    assert sh('version_matches "0.5.1" "0.5.1"', tmp_path).returncode == 0


def test_version_matches_rejects_a_different_version(tmp_path):
    assert sh('version_matches "0.5.1" "0.6.0"', tmp_path).returncode == 1


def test_version_matches_rejects_an_unknown_version(tmp_path):
    assert sh('version_matches "unknown" "0.5.1"', tmp_path).returncode == 1


def test_restore_doc_states_what_is_not_recovered(tmp_path):
    out = tmp_path / "RESTORE.md"
    result = sh(f"write_restore_doc {out}", tmp_path)
    assert result.returncode == 0, result.stderr
    text = out.read_text()
    assert "--force" in text
    assert "provider keys" in text.lower()
    assert "no migration" in text.lower()


def test_json_field_reads_a_string(tmp_path):
    m = tmp_path / "manifest.json"
    m.write_text('{\n  "version": "0.5.1",\n  "blob_count": 42\n}\n')
    assert sh(f'json_field {m} version', tmp_path).stdout.strip() == "0.5.1"


def test_json_field_reads_a_number(tmp_path):
    m = tmp_path / "manifest.json"
    m.write_text('{\n  "version": "0.5.1",\n  "blob_count": 42\n}\n')
    assert sh(f'json_field {m} blob_count', tmp_path).stdout.strip() == "42"


def test_restore_refuses_a_directory_missing_files(tmp_path):
    incomplete = tmp_path / "backup"
    (incomplete / "dump").mkdir(parents=True)
    result = sh(f"preflight_backup_dir {incomplete}", tmp_path)
    assert result.returncode != 0
    assert "manifest.json" in result.stderr


def test_preflight_accepts_a_complete_directory(tmp_path):
    good = tmp_path / "backup"
    (good / "dump").mkdir(parents=True)
    for name in ("data-manifest.tsv", "providers.txt", "manifest.json", "RESTORE.md"):
        (good / name).write_text("x")
    result = sh(f"preflight_backup_dir {good}", tmp_path)
    assert result.returncode == 0, result.stderr


def test_verify_reports_a_missing_blob(tmp_path):
    data = tmp_path / "data"
    (data / "ab").mkdir(parents=True)
    (data / "ab" / "present").write_text("hello")

    manifest = tmp_path / "data-manifest.tsv"
    manifest.write_text(
        "blob_id\tsize\tpath\tcontent_sha256\tstate\n"
        "present\t5\tab/present\tsha-a\tactive\n"
        "gone\t9\tab/gone\tsha-b\tactive\n"
    )
    result = sh(f"check_manifest_against_data {manifest} {data}", tmp_path)
    assert "ab/gone" in result.stdout
    assert "ab/present" not in result.stdout


def test_verify_is_silent_when_everything_is_present(tmp_path):
    data = tmp_path / "data"
    (data / "ab").mkdir(parents=True)
    (data / "ab" / "present").write_text("hello")

    manifest = tmp_path / "data-manifest.tsv"
    manifest.write_text(
        "blob_id\tsize\tpath\tcontent_sha256\tstate\n"
        "present\t5\tab/present\tsha-a\tactive\n"
    )
    result = sh(f"check_manifest_against_data {manifest} {data}", tmp_path)
    assert result.stdout.strip() == ""
