"""`ops/backup.sh`: the backup, restore, and verify subcommands.

Split by what each test needs, in three layers:

1. Pure shell logic -- sourced functions, tmp_path fixtures, no Docker.
2. Locale-pinned execution of `verify`, which runs the real subcommand
   without a Mongo. This layer exists because #492 found that layer 1 sources
   only the preamble above the dispatch `case`, so nothing *inside* a
   subcommand was executed by any test that runs in CI.
3. The round trip, which needs a real Mongo and is skipped without Docker.

Layers 1 and 2 run wherever pytest does. Layer 3 skips when no Docker daemon
is reachable; whether that includes CI depends on the runner, not on
anything in this repo -- see the note above `docker_required`.
"""

import json
import os
import re
import shutil
import subprocess
import time
import uuid
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


def test_no_bare_expansion_is_glued_to_a_non_ascii_character():
    """`log "$dir…"` is a crash, not a cosmetic issue.

    Under a UTF-8 LC_CTYPE, bash reads the bytes of a multibyte character as
    identifier continuation characters, so "$dir…" expands ${dir…} and
    `set -u` kills the script. It cost a broken `restore` and a broken
    `verify` that no pure-logic test could see, because sourcing the preamble
    never reaches those lines. Brace the expansion: "${dir}…".

    The rule is any non-ASCII character, not the ellipsis specifically. An
    em-dash, an arrow, or a curly quote pasted into a log line fails the same
    way, and the original form of this test matched "…" alone -- which would
    have passed every one of them.

    Note this cannot be delegated to shellcheck: shellcheck reports the buggy
    line clean (verified against 0.11.0), because the expansion is valid
    syntax whose meaning depends on the runtime locale.
    """
    offenders = [
        (n, line)
        for n, line in enumerate(SCRIPT.read_text().splitlines(), 1)
        # Comments are prose; the note explaining this rule spells the bad
        # form out on purpose.
        if not line.lstrip().startswith("#")
        and re.search(r"\$[A-Za-z_][A-Za-z_0-9]*[^\x00-\x7f]", line)
    ]
    assert not offenders, f"unbraced expansion before a non-ASCII character: {offenders}"


# --- locale-pinned execution ----------------------------------------------
#
# The gap the ellipsis bug lived in: every test above sources the preamble and
# never *executes* cmd_restore or cmd_verify, and the round-trip tests below
# need Docker. So the lines where the bug actually was ran in no CI test.
#
# Reaching them without a Mongo takes some care, because both sit late in
# their subcommand:
#
#   cmd_restore  preflight -> version check -> confirmation -> MONGO probe
#                -> line 346 -> mongorestore
#   cmd_verify   preflight -> data-root check -> line 389
#
# So a test that passes a bogus directory dies at the preflight and proves
# nothing -- which is exactly what the first version of these tests did, and
# it passed against a deliberately re-broken script. Each test below builds a
# backup directory complete enough to get past every guard, and asserts on
# the message the script emits *at* the line under test.

UTF8_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
    "LC_ALL": "en_US.UTF-8",
    "LANG": "en_US.UTF-8",
}


def make_backup_dir(tmp_path: Path, version: str = "unknown") -> Path:
    """A directory complete enough to satisfy preflight_backup_dir."""
    d = tmp_path / "backup"
    (d / "dump").mkdir(parents=True)
    (d / "dump" / "biopipe.archive").write_bytes(b"")
    (d / "manifest.json").write_text(
        json.dumps(
            {
                "version": version,
                "database": "biopipe",
                "blob_count": 0,
                "collection_counts": {},
            },
            indent=2,
        )
    )
    (d / "data-manifest.tsv").write_text(
        "blob_id\tsize\tpath\tcontent_sha256\tstate\n"
    )
    (d / "providers.txt").write_text("(none)\n")
    (d / "RESTORE.md").write_text("# Restoring this backup\n")
    return d


def run_utf8(args: list[str], tmp_path: Path, **extra_env) -> subprocess.CompletedProcess:
    """Runs a subcommand under the locale that breaks an unbraced expansion.

    MONGO_CONTAINER is pinned at a name that cannot exist, so this can never
    reach the running stack however the guards are later reordered. That is
    also what stops cmd_restore before it runs mongorestore -- but only after
    it has crossed the line under test.
    """
    env = {
        **UTF8_ENV,
        "MONGO_CONTAINER": f"bioflow-nonexistent-{uuid.uuid4().hex[:8]}",
        "BACKUP_DIR": str(tmp_path / "backups"),
        **extra_env,
    }
    # errors="replace", not text=True: the bug under test makes bash write a
    # mangled variable name that is not valid UTF-8, and a strict decode turns
    # this test's finding into a UnicodeDecodeError traceback instead.
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        env=env,
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_verify_reaches_its_log_line_under_a_utf8_locale(tmp_path):
    """cmd_verify's `${data_root}…` line, executed rather than pattern-matched.

    An unbound-variable crash says so on stderr and never prints "Checking",
    so the two outcomes are distinguishable.
    """
    backup = make_backup_dir(tmp_path)
    data_root = tmp_path / "data"
    data_root.mkdir()

    result = run_utf8(["verify", str(backup)], tmp_path, BIOINFO_HOME=str(data_root))

    assert "unbound variable" not in result.stderr, result.stderr
    assert "Checking" in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr


# cmd_restore has no equivalent execution test, deliberately. Its
# `${dir}…` line sits *after* an unconditional `docker exec ... || die` Mongo
# probe, so no fixture reaches it without a real container -- which is why the
# original bug survived in the first place. The round-trip tests below cover
# it when Docker is present; the static test above is what covers it in CI.


@pytest.mark.parametrize("subcommand", ["restore", "verify"])
def test_subcommand_requires_a_directory_argument(subcommand, tmp_path):
    """The zero-argument path through the while-loop, under the same locale."""
    result = run_utf8([subcommand], tmp_path)

    assert "unbound variable" not in result.stderr, result.stderr
    assert "usage:" in result.stderr, result.stderr


# --- the round trip -------------------------------------------------------
#
# Everything below drops a database and reloads it. Pointed at the running
# stack it would destroy the user's research record while reporting a pass,
# so every invocation goes through run_script(), which pins MONGO_CONTAINER
# to the scratch_mongo fixture's own throwaway container.

# The round-trip needs a real Mongo, which this fixture starts itself with
# `docker run mongo:7` -- so the gate is a reachable Docker daemon, nothing
# more. It is NOT the commented-out `services:` block in build-check.yml:
# that belongs to the `backend-full-test` job, which ops/tests has never run
# in, and enabling it would not affect these tests. #492 originally read the
# skip the other way round; correcting that is why this note is long.
#
# The skip names its reason rather than passing quietly.
docker_required = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "info"], capture_output=True).returncode != 0,
    reason="needs a reachable Docker daemon; the CI ops-test job has none",
)


@pytest.fixture
def scratch_mongo():
    """A throwaway Mongo container, never the running stack's.

    The name is randomised so a leftover container from a crashed run cannot
    be reused by accident, and the container is removed on teardown.
    """
    name = f"bioflow-backup-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "mongo:7"],
        check=True,
        capture_output=True,
    )
    try:
        for _ in range(30):
            probe = subprocess.run(
                ["docker", "exec", name, "mongosh", "--quiet", "--eval", "db.hello().ok"],
                capture_output=True,
                text=True,
            )
            if probe.returncode == 0 and "1" in probe.stdout:
                break
            time.sleep(1)
        else:
            pytest.fail("scratch Mongo never became ready")
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def seed(container: str, db: str = "biopipe") -> None:
    """A fixture spanning the shapes that matter."""
    script = """
    db.projects.insertOne({_id: "proj1", name: "Test Project"});
    db.objects.insertMany([
      {_id: "obj1", project_id: "proj1", role: "reads", size: 100, blob_id: "blob1"},
      {_id: "obj2", project_id: "proj1", role: "reference", size: 200, blob_id: "blob2"}
    ]);
    db.blobs.insertMany([
      {_id: "blob1", size: 100, rel_path: "ab/blob1", content_sha256: "sha1", state: "active"},
      {_id: "blob2", size: 200, external_path: "/ext/ref.fa", content_sha256: "sha2",
        state: "active"}
    ]);
    db.pipeline_runs.insertOne({_id: "run1", project_id: "proj1", status: "complete"});
    db.run_jobs.insertOne({_id: "job1", run_id: "run1", object_id: "obj1"});
    db.job_timings.insertOne({_id: "t1", job_id: "job1", duration_seconds: 12.5});
    db.ai_providers.insertOne({
      _id: "prov1", name: "anthropic", model: "claude-opus-5",
      api_key_enc: BinData(0, "Z0FBQUFBQm1abT")
    });
    """
    subprocess.run(
        ["docker", "exec", "-i", container, "mongosh", db, "--quiet", "--eval", script],
        check=True,
        capture_output=True,
    )


def counts(container: str, db: str = "biopipe") -> dict:
    out = subprocess.run(
        [
            "docker", "exec", "-i", container, "mongosh", db, "--quiet", "--eval",
            'db.getCollectionNames().sort().forEach('
            'n => print(n + "\\t" + db.getCollection(n).countDocuments({})))',
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {
        line.split("\t")[0]: int(line.split("\t")[1])
        for line in out.strip().splitlines()
        if "\t" in line
    }


def run_script(args: list[str], container: str, backup_dir: Path, **kw):
    """Every call into backup.sh goes through here, pinned to the scratch Mongo.

    API_CONTAINER is pinned too, at a name that cannot exist. Left at its
    default, write_provider_summary would reach the *real* api container --
    which connects to the real Mongo -- and copy the live provider list into
    a test backup. It is a read, so nothing is destroyed, but the summary
    would describe the wrong stack. Unreachable is the correct answer here,
    and the script degrades to "(could not reach the api container)".
    """
    env = {
        **os.environ,
        "MONGO_CONTAINER": container,
        "MONGO_DB": "biopipe",
        "API_CONTAINER": f"bioflow-backup-test-no-api-{uuid.uuid4().hex[:8]}",
        "BACKUP_DIR": str(backup_dir),
    }
    return subprocess.run(
        [str(SCRIPT), *args], capture_output=True, text=True, env=env, **kw
    )


def drop_db(container: str, db: str = "biopipe") -> None:
    subprocess.run(
        ["docker", "exec", "-i", container, "mongosh", db, "--quiet",
         "--eval", "db.dropDatabase()"],
        check=True,
        capture_output=True,
    )


@docker_required
def test_the_fixture_is_not_the_running_stack(scratch_mongo):
    """The guard the rest of this file rests on.

    If the fixture ever hands back the live container name, every test below
    becomes a mongorestore --drop against the research record.
    """
    assert scratch_mongo.startswith("bioflow-backup-test-")
    assert scratch_mongo != "biopipe-mongo-1"
    # A fresh mongo:7 has no biopipe database. The live one does.
    assert counts(scratch_mongo) == {}


@docker_required
def test_backup_restore_round_trip(scratch_mongo, tmp_path):
    seed(scratch_mongo)
    before = counts(scratch_mongo)
    assert before["projects"] == 1 and before["objects"] == 2

    result = run_script(["backup"], scratch_mongo, tmp_path)
    assert result.returncode == 0, result.stderr

    made = sorted(tmp_path.iterdir())
    assert len(made) == 1
    backup = made[0]
    for name in ("dump", "data-manifest.tsv", "providers.txt", "manifest.json", "RESTORE.md"):
        assert (backup / name).exists(), f"{name} missing from the backup"

    drop_db(scratch_mongo)
    assert counts(scratch_mongo) == {}

    result = run_script(["restore", str(backup), "--force"], scratch_mongo, tmp_path)
    assert result.returncode == 0, result.stderr

    after = counts(scratch_mongo)
    assert after == before, f"counts differ after restore: {before} -> {after}"


@docker_required
def test_provenance_chain_survives_the_round_trip(scratch_mongo, tmp_path):
    seed(scratch_mongo)
    run_script(["backup"], scratch_mongo, tmp_path)
    backup = sorted(tmp_path.iterdir())[0]
    drop_db(scratch_mongo)
    run_script(["restore", str(backup), "--force"], scratch_mongo, tmp_path)

    chain = subprocess.run(
        ["docker", "exec", "-i", scratch_mongo, "mongosh", "biopipe", "--quiet", "--eval",
         'const j = db.run_jobs.findOne({_id: "job1"});'
         'const o = db.objects.findOne({_id: j.object_id});'
         'const b = db.blobs.findOne({_id: o.blob_id});'
         'print([j.run_id, o.role, b.content_sha256].join("|"));'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert chain == "run1|reads|sha1"


@docker_required
def test_backup_contains_no_secrets(scratch_mongo, tmp_path):
    """The assertion that keeps the security decision true after later edits.

    rglob("*") walks the raw mongodump archive too, not just the text files.
    That matters: the archive is where a provider document's ciphertext
    actually lands, so a future change that started shipping the Fernet key
    beside it would be caught here rather than in the text sidecars alone.
    """
    seed(scratch_mongo)
    run_script(["backup"], scratch_mongo, tmp_path)
    backup = sorted(tmp_path.iterdir())[0]

    blob = b""
    for path in backup.rglob("*"):
        if path.is_file():
            blob += path.read_bytes()

    # The dump is in there, so this is scanning the bytes that carry the
    # ciphertext -- not only the human-readable sidecars.
    assert b"anthropic" in blob, "scan did not reach the dump; the assertions below are vacuous"

    # The Fernet key file's own name and any Fernet token prefix.
    assert b"secret.key" not in blob
    assert b"BIOINFO_HOME/.biopipe" not in blob
    # A decrypted Anthropic-style key would start like this.
    assert b"sk-ant-" not in blob


@docker_required
def test_version_mismatch_without_force_writes_nothing(scratch_mongo, tmp_path):
    seed(scratch_mongo)
    run_script(["backup"], scratch_mongo, tmp_path)
    backup = sorted(tmp_path.iterdir())[0]

    manifest = backup / "manifest.json"
    manifest.write_text(manifest.read_text().replace('"version": "', '"version": "9.9.9-'))

    drop_db(scratch_mongo)
    result = run_script(["restore", str(backup)], scratch_mongo, tmp_path)
    assert result.returncode != 0
    assert "Version mismatch" in result.stderr
    assert counts(scratch_mongo) == {}, "restore wrote despite refusing"


@docker_required
def test_restore_rejects_a_database_mismatch_before_writing(scratch_mongo, tmp_path):
    """mongorestore --archive ignores $MONGO_DB and restores into the name

    recorded inside the dump itself. If the backup's recorded database
    disagrees with the current $MONGO_DB, restoring would silently write into
    the wrong database after prompting the user to confirm the one they
    expected -- so this must be caught before mongorestore ever runs.
    """
    seed(scratch_mongo)
    run_script(["backup"], scratch_mongo, tmp_path)
    backup = sorted(tmp_path.iterdir())[0]

    manifest = backup / "manifest.json"
    manifest.write_text(
        manifest.read_text().replace('"database": "biopipe"', '"database": "other"')
    )

    result = run_script(["restore", str(backup), "--force"], scratch_mongo, tmp_path)
    assert result.returncode != 0
    assert "other" in result.stderr
    assert "biopipe" in result.stderr
    assert counts(scratch_mongo) != {}, "restore should have refused before dropping anything"


@docker_required
def test_version_mismatch_with_force_completes(scratch_mongo, tmp_path):
    seed(scratch_mongo)
    before = counts(scratch_mongo)
    run_script(["backup"], scratch_mongo, tmp_path)
    backup = sorted(tmp_path.iterdir())[0]

    manifest = backup / "manifest.json"
    manifest.write_text(manifest.read_text().replace('"version": "', '"version": "9.9.9-'))

    drop_db(scratch_mongo)
    result = run_script(["restore", str(backup), "--force"], scratch_mongo, tmp_path)
    assert result.returncode == 0, result.stderr
    assert counts(scratch_mongo) == before


@docker_required
def test_restore_fails_loudly_on_a_count_mismatch(scratch_mongo, tmp_path):
    seed(scratch_mongo)
    run_script(["backup"], scratch_mongo, tmp_path)
    backup = sorted(tmp_path.iterdir())[0]

    manifest = backup / "manifest.json"
    manifest.write_text(manifest.read_text().replace('"projects": 1', '"projects": 7'))

    result = run_script(["restore", str(backup), "--force"], scratch_mongo, tmp_path)
    assert result.returncode != 0
    assert "do not match" in result.stderr
