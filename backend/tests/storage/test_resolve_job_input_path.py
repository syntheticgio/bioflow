"""Job-payload `*_path` values are held to the register-root allowlist (#873).

`_resolve_blob` used to do `Path(path_str)` with only an `.exists()` check, so
anything that could enqueue a job could name any file the worker can read --
`/data/.biopipe/secret.key`, `/etc/passwd` -- and have a pipeline consume it,
with the content potentially surfacing in outputs. The reachable route is the
MCP `run_pipeline` tool, whose `params` become the payload verbatim, so a
prompt-injected agent (file content is untrusted) could drive it.
"""

import pytest

from app.config import settings
from app.errors import PermanentError, ValidationError
from app.queue.align_handlers import _resolve_blob
from app.queue.pipeline_handlers import _resolve_input
from app.storage.paths import resolve_job_input_path


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """A register root and a BIOINFO_HOME, both real directories."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    (home / "objects").mkdir()
    (home / "staging").mkdir()

    monkeypatch.setattr(settings, "bioinfo_register_roots", str(allowed))
    monkeypatch.setattr(settings, "bioinfo_home", home)
    return allowed, home


class TestResolveJobInputPath:
    def test_a_file_under_a_register_root_is_accepted(self, roots):
        allowed, _ = roots
        f = allowed / "reads.fastq"
        f.write_text("x")
        assert resolve_job_input_path(str(f)) == f.resolve()

    def test_a_file_outside_every_root_is_refused(self, roots, tmp_path):
        outside = tmp_path / "outside.fastq"
        outside.write_text("x")
        with pytest.raises(ValidationError, match="outside the allowed roots"):
            resolve_job_input_path(str(outside))

    def test_a_relative_path_is_refused(self, roots):
        with pytest.raises(ValidationError, match="must be absolute"):
            resolve_job_input_path("reads.fastq")

    def test_a_symlink_escaping_a_root_is_refused(self, roots, tmp_path):
        """The reason resolution happens before the containment check: a link
        inside an allowed root pointing out of it must not be followed."""
        allowed, _ = roots
        secret = tmp_path / "secret.key"
        secret.write_text("s3cret")
        link = allowed / "innocent.fastq"
        link.symlink_to(secret)

        with pytest.raises(ValidationError, match="outside the allowed roots"):
            resolve_job_input_path(str(link))

    def test_traversal_out_of_a_root_is_refused(self, roots, tmp_path):
        allowed, _ = roots
        secret = tmp_path / "secret.key"
        secret.write_text("s3cret")
        with pytest.raises(ValidationError, match="outside the allowed roots"):
            resolve_job_input_path(str(allowed / ".." / "secret.key"))

    @pytest.mark.parametrize("subdir", ["tmp", "objects", "staging"])
    def test_bioflows_own_directories_are_allowed(self, roots, subdir):
        """Wider than resolve_registerable by exactly these: a handler that fans
        out sub-jobs writes intermediate inputs here and passes them by path --
        align_reads_chunked does it with per-bucket reference FASTAs. Refusing
        them would break chunked alignment for any non-default
        BIOINFO_REGISTER_ROOTS.
        """
        _, home = roots
        f = home / subdir / "bucket_0.fa"
        f.write_text(">x\nACGT\n")
        assert resolve_job_input_path(str(f)) == f.resolve()


class TestHandlersEnforceIt:
    """Both resolvers, because a payload path is untrusted whichever one it
    reaches -- align_handlers._resolve_digest_or_path was the one the report
    named, and pipeline_handlers._resolve_input had the identical hole."""

    def test_align_refuses_an_out_of_root_reference_path(self, roots, tmp_path):
        secret = tmp_path / "secret.key"
        secret.write_text("s3cret")
        with pytest.raises(PermanentError, match="outside the allowed roots"):
            _resolve_blob({"reference_path": str(secret)}, "reference")

    def test_reads_resolver_refuses_an_out_of_root_path(self, roots, tmp_path):
        secret = tmp_path / "secret.key"
        secret.write_text("s3cret")
        with pytest.raises(PermanentError, match="outside the allowed roots"):
            _resolve_input({"r1_path": str(secret)}, "r1")

    def test_the_refusal_is_permanent_not_retryable(self, roots, tmp_path):
        """A retryable error would re-run the same refused payload five times.
        PermanentError is the correct class: the payload will not become
        allowed on a later attempt."""
        secret = tmp_path / "secret.key"
        secret.write_text("s3cret")
        with pytest.raises(PermanentError):
            _resolve_blob({"reference_path": str(secret)}, "reference")

    def test_an_allowed_path_still_resolves(self, roots):
        """The check must not break the legitimate register-in-place case,
        which is the only reason payloads carry paths at all."""
        allowed, _ = roots
        f = allowed / "ref.fa"
        f.write_text(">x\nACGT\n")
        assert _resolve_blob({"reference_path": str(f)}, "reference") == f.resolve()
        assert _resolve_input({"r1_path": str(f)}, "r1") == f.resolve()
