"""Serving generated QC reports.

Exercised through the real route rather than a reimplementation of its path
handling: the containment check is the only thing standing between a report URL
and the object store beside it, and a test that recomputed the same join would
keep passing after the route stopped doing it.

Mounted on a bare app so these run without a database -- the endpoint touches
only the filesystem, and the rest of the API's startup is irrelevant to it.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import pipelines as pipelines_api
from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers
from tests.api.bare_app import override_owner, stub_get_object

OBJECT_ID = "507f1f77bcf86cd799439011"
OTHER_ID = "507f191e810c19729de860ea"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client whose report root is a temporary directory.

    `qc_reports_dir` is a property derived from `bioinfo_home`, so the home is
    what gets redirected -- patching the property itself would diverge from how
    the route resolves it.

    Both ids resolve through the stubbed ownership lookup. These tests are
    about path containment, not about isolation, and a stub that refused
    OTHER_ID would let the traversal tests pass for the wrong reason -- 404
    from the lookup rather than from the check they exist to exercise.
    Isolation itself is covered in test_pipelines_profiles.py against a real
    database.
    """
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)

    reports = tmp_path / "qc_reports"
    (reports / OBJECT_ID / "fastqc").mkdir(parents=True)
    (reports / OBJECT_ID / "fastp.html").write_text("<html>fastp</html>")
    (reports / OBJECT_ID / "fastqc" / "reads_fastqc.html").write_text("<html>fastqc</html>")
    (reports / OTHER_ID).mkdir(parents=True)
    (reports / OTHER_ID / "fastp.html").write_text("<html>someone else</html>")

    # A file outside the report tree entirely -- what a traversal would be
    # reaching for.
    (tmp_path / "secret.txt").write_text("blob bytes")

    stub_get_object(monkeypatch, pipelines_api, known={OBJECT_ID, OTHER_ID})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


def get(client, path: str, object_id: str = OBJECT_ID):
    # raw_path so the client does not normalize `..` away before the server
    # sees it; that normalization is the browser's behaviour, not an attacker's.
    return client.get(
        f"/pipelines/qc/report/{object_id}/{path}", follow_redirects=False
    )


class TestServingReports:
    def test_serves_a_report_at_the_root(self, client):
        r = get(client, "fastp.html")
        assert r.status_code == 200
        assert "fastp" in r.text

    def test_serves_a_report_in_a_subdirectory(self, client):
        """FastQC writes into its own directory, so the route has to accept a
        multi-segment path -- which is what makes the traversal check load-bearing."""
        r = get(client, "fastqc/reads_fastqc.html")
        assert r.status_code == 200
        assert "fastqc" in r.text

    def test_a_missing_report_is_a_404(self, client):
        assert get(client, "never_ran.html").status_code == 404

    def test_a_directory_is_not_served(self, client):
        """`is_file()` rather than `exists()`: FileResponse on a directory
        raises rather than 404s."""
        assert get(client, "fastqc").status_code == 404


class TestUntrustedContent:
    """FastQC embeds overrepresented sequences taken verbatim from the reads,
    so the page can contain attacker-chosen bytes. It is served from the API's
    own origin, which makes the response headers the whole defence."""

    def test_the_page_is_sandboxed(self, client):
        """`sandbox` drops it into a unique opaque origin with scripting off,
        so it cannot reach the API session despite being same-origin."""
        csp = get(client, "fastp.html").headers["content-security-policy"]
        assert "sandbox" in csp

    def test_the_page_cannot_fetch_anything(self, client):
        csp = get(client, "fastp.html").headers["content-security-policy"]
        assert "default-src 'none'" in csp

    def test_content_type_is_not_sniffed(self, client):
        assert get(client, "fastp.html").headers["x-content-type-options"] == "nosniff"


class TestPathTraversal:
    @pytest.mark.parametrize(
        "attack",
        [
            "../../secret.txt",
            "../../../etc/passwd",
            "fastqc/../../../secret.txt",
            "fastqc/../fastp.html/../../secret.txt",
            "/etc/passwd",
        ],
    )
    def test_traversal_out_of_the_report_tree_serves_nothing(self, client, attack):
        """Whatever else happens, no file outside the report tree comes back.

        Asserted on the *content* rather than only the status, because these
        requests do not all reach the handler by the same route: the ASGI layer
        collapses some of them into a different URL first (see below). What
        must hold for all of them is that the secret is not served."""
        r = get(client, attack)
        assert "blob bytes" not in r.text
        assert "root:" not in r.text

    @pytest.mark.parametrize(
        "attack", ["fastqc/../fastp.html", "../secret.txt", "/etc/passwd"]
    )
    async def test_the_handler_itself_refuses_a_dotdot_segment(self, client, attack):
        """Called directly, not over HTTP, and deliberately so.

        Every `..` is collapsed by the ASGI transport before routing, so this
        guard is unreachable through the test client -- which is exactly why it
        is worth having and worth testing here. It is what holds if this
        function is ever called from another route, mounted under a server that
        normalizes differently, or reached by a client that sends the path
        pre-encoded."""
        from app.api.v1.pipelines import get_qc_report
        from app.errors import NotFoundError
        from tests.api.bare_app import TEST_OWNER

        with pytest.raises(NotFoundError):
            await get_qc_report(OBJECT_ID, attack, TEST_OWNER)

    def test_the_asgi_layer_rewrites_the_object_id_before_routing(self, client):
        """Documenting a real hazard rather than blessing it.

        `/report/<a>/../<b>/fastp.html` never reaches the handler as a
        traversal: the transport collapses it and the route matches with
        object_id already rewritten to <b>. So <b>'s own report is served,
        correctly and from its own directory -- but the id in the URL is not
        evidence of which directory was read.

        The ownership check this route now runs satisfies the requirement this
        docstring used to state as a warning: it resolves the `object_id`
        parameter, which is the *rewritten* one, so it authorizes the directory
        actually read rather than the id the URL appears to name. Both ids
        resolve for this client, which is why <b>'s report still comes back."""
        r = get(client, f"../{OTHER_ID}/fastp.html")
        assert r.status_code == 200
        assert "someone else" in r.text
