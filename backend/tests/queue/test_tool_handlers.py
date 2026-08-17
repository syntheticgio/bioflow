"""Install/uninstall job handlers for ON_DEMAND_IMAGE tools."""

import pytest
from app.errors import PermanentError, RetryableError
from app.pipelines import tools
from app.queue import tool_handlers
from app.queue.registry import JobContext


def make_ctx(**kw) -> JobContext:
    return JobContext(
        job_id=kw.pop("job_id", "job-1"), payload=kw.pop("payload", {}), epoch=1,
        attempts=0, owner="local", **kw,
    )


class TestToolAndImage:
    def test_resolves_an_on_demand_tool(self):
        name, image = tool_handlers._tool_and_image({"tool": "deepvariant"})
        assert name == "deepvariant"
        assert image == tools.TOOL_META["deepvariant"].image

    def test_missing_tool_key_is_permanent(self):
        with pytest.raises(PermanentError):
            tool_handlers._tool_and_image({})

    def test_bundled_tool_is_refused(self):
        """The service layer (task 4's other half) is expected to refuse
        this before a job ever exists, but a handler must not assume its
        only caller is the one that currently exists -- fastp has no image
        to pull, and shelling out for one would be nonsensical."""
        with pytest.raises(PermanentError, match="on-demand"):
            tool_handlers._tool_and_image({"tool": "fastp"})

    def test_unknown_tool_is_refused(self):
        with pytest.raises(PermanentError):
            tool_handlers._tool_and_image({"tool": "not-a-real-tool"})


class TestDockerClient:
    def test_resolves_a_real_client(self, monkeypatch):
        monkeypatch.setattr(tool_handlers.shutil, "which", lambda _: "/usr/local/bin/docker")
        assert tool_handlers._docker_client() == "/usr/local/bin/docker"

    def test_missing_client_is_permanent(self, monkeypatch):
        monkeypatch.setattr(tool_handlers.shutil, "which", lambda _: None)
        with pytest.raises(PermanentError):
            tool_handlers._docker_client()


class TestPullProgress:
    """`docker pull`'s piped (non-TTY) output collapses to one line per
    layer-state-change, confirmed against a real pull rather than assumed --
    see the module docstring on _PullProgress."""

    def test_a_non_layer_line_starts_the_pulling_phase_once(self):
        progress = tool_handlers._PullProgress()
        assert progress.feed("latest: Pulling from library/nginx") is True
        assert progress.phase == "pulling"
        # A second banner-style line is not a phase change any more.
        assert progress.feed("Digest: sha256:abc") is False

    def test_a_new_layer_line_is_a_change(self):
        progress = tool_handlers._PullProgress()
        assert progress.feed("26c307b5e35a: Pulling fs layer") is True

    def test_a_repeated_status_for_the_same_layer_is_not_a_change(self):
        progress = tool_handlers._PullProgress()
        progress.feed("26c307b5e35a: Pulling fs layer")
        assert progress.feed("26c307b5e35a: Pulling fs layer") is False

    def test_pct_counts_completed_layers_against_layers_seen(self):
        progress = tool_handlers._PullProgress()
        progress.feed("aaaaaaaaaaaa: Pulling fs layer")
        progress.feed("bbbbbbbbbbbb: Pulling fs layer")
        assert progress.pct == 0.0

        progress.feed("aaaaaaaaaaaa: Pull complete")
        assert progress.pct == 0.5

        progress.feed("bbbbbbbbbbbb: Pull complete")
        assert progress.pct == 1.0

    def test_already_exists_counts_as_done(self):
        """A layer shared with an image already on disk skips straight to
        this status and never says Pull complete -- must still count."""
        progress = tool_handlers._PullProgress()
        progress.feed("aaaaaaaaaaaa: Already exists")
        assert progress.pct == 1.0

    def test_pct_is_none_before_any_layer_line(self):
        progress = tool_handlers._PullProgress()
        assert progress.pct is None

    def test_message_reports_the_fraction(self):
        progress = tool_handlers._PullProgress()
        progress.feed("aaaaaaaaaaaa: Pulling fs layer")
        progress.feed("bbbbbbbbbbbb: Pulling fs layer")
        progress.feed("aaaaaaaaaaaa: Pull complete")
        assert "1/2" in progress.message()

    def test_pct_never_regresses(self):
        """A layer cycling through Downloading -> Verifying Checksum ->
        Download complete -> Pull complete must not make the fraction dip
        back down partway through -- only Pull complete / Already exists
        count toward the numerator, so intermediate states are simply not
        counted rather than miscounted."""
        progress = tool_handlers._PullProgress()
        progress.feed("aaaaaaaaaaaa: Pulling fs layer")
        progress.feed("aaaaaaaaaaaa: Pull complete")
        before = progress.pct
        progress.feed("bbbbbbbbbbbb: Downloading")
        assert progress.pct is not None and before is not None
        assert progress.pct <= before or progress.pct == 0.5


class TestProgressReporter:
    def test_reports_through_ctx_progress_only_on_change(self):
        calls = []
        ctx = make_ctx()
        ctx._progress_cb = calls.append

        on_line = tool_handlers._progress_reporter(ctx)
        on_line("aaaaaaaaaaaa: Pulling fs layer")
        on_line("aaaaaaaaaaaa: Pulling fs layer")  # repeat, no new call
        on_line("aaaaaaaaaaaa: Pull complete")

        assert len(calls) == 2


class TestInstallHandler:
    def test_installs_and_invalidates_on_success(self, monkeypatch):
        monkeypatch.setattr(tool_handlers.shutil, "which", lambda _: "/usr/local/bin/docker")
        monkeypatch.setattr(tool_handlers, "run_subprocess", lambda *a, **k: 0)
        invalidated = []
        monkeypatch.setattr(tool_handlers, "_invalidate", invalidated.append)

        ctx = make_ctx(payload={"tool": "deepvariant"})
        result = tool_handlers.install_tool(ctx)

        assert result["tool"] == "deepvariant"
        assert invalidated == ["deepvariant"]

    def test_a_nonzero_exit_is_retryable(self, monkeypatch, tmp_path):
        """Unlike a missing binary, a pull failure is often transient --
        RetryableError, not PermanentError, matching the handler's own
        higher max_attempts."""
        monkeypatch.setattr(tool_handlers.shutil, "which", lambda _: "/usr/local/bin/docker")
        monkeypatch.setattr(tool_handlers, "run_subprocess", lambda *a, **k: 1)
        # logs_dir is a derived read-only property (bioinfo_home / "logs"),
        # not a settable field -- patch what it derives from.
        monkeypatch.setattr(tool_handlers.settings, "bioinfo_home", tmp_path)

        ctx = make_ctx(payload={"tool": "deepvariant"})
        with pytest.raises(RetryableError):
            tool_handlers.install_tool(ctx)

    def test_does_not_invalidate_on_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tool_handlers.shutil, "which", lambda _: "/usr/local/bin/docker")
        monkeypatch.setattr(tool_handlers, "run_subprocess", lambda *a, **k: 1)
        # logs_dir is a derived read-only property (bioinfo_home / "logs"),
        # not a settable field -- patch what it derives from.
        monkeypatch.setattr(tool_handlers.settings, "bioinfo_home", tmp_path)
        invalidated = []
        monkeypatch.setattr(tool_handlers, "_invalidate", invalidated.append)

        ctx = make_ctx(payload={"tool": "deepvariant"})
        with pytest.raises(RetryableError):
            tool_handlers.install_tool(ctx)

        assert invalidated == []


class TestUninstallHandler:
    def test_uninstalls_and_invalidates_on_success(self, monkeypatch):
        monkeypatch.setattr(tool_handlers.shutil, "which", lambda _: "/usr/local/bin/docker")
        monkeypatch.setattr(tool_handlers, "run_subprocess", lambda *a, **k: 0)
        invalidated = []
        monkeypatch.setattr(tool_handlers, "_invalidate", invalidated.append)

        ctx = make_ctx(payload={"tool": "deepvariant"})
        result = tool_handlers.uninstall_tool(ctx)

        assert result["tool"] == "deepvariant"
        assert invalidated == ["deepvariant"]

    def test_a_nonzero_exit_is_permanent(self, monkeypatch, tmp_path):
        """`docker image rm` fails deterministically -- image in use, image
        already gone -- unlike a pull's transient network errors."""
        monkeypatch.setattr(tool_handlers.shutil, "which", lambda _: "/usr/local/bin/docker")
        monkeypatch.setattr(tool_handlers, "run_subprocess", lambda *a, **k: 1)
        # logs_dir is a derived read-only property (bioinfo_home / "logs"),
        # not a settable field -- patch what it derives from.
        monkeypatch.setattr(tool_handlers.settings, "bioinfo_home", tmp_path)

        ctx = make_ctx(payload={"tool": "deepvariant"})
        with pytest.raises(PermanentError):
            tool_handlers.uninstall_tool(ctx)


class TestInvalidate:
    def test_a_failure_is_logged_not_raised(self, monkeypatch):
        """Same discipline as tool_cache.publish_invalidation itself: a
        missed invalidation means a stale badge, not a failed install that
        already succeeded on disk."""

        def broken_run_from_thread(coro):
            coro.close()  # avoid the "coroutine was never awaited" warning
            raise RuntimeError("no event loop")

        import app.db.client as db_client

        monkeypatch.setattr(db_client, "run_from_thread", broken_run_from_thread)

        tool_handlers._invalidate("deepvariant")  # must not raise
