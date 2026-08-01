"""The UniProt download handler, with the transport stubbed.

Deliberately unlike `test_assembly_download.py`: there is no zip, no
checksum manifest, and no path-traversal check, because the response is a
gzipped FASTA stream rather than an archive that writes files.
"""

import gzip
import threading
from pathlib import Path

import pytest

from app.errors import PermanentError, RetryableError
from app.queue import uniprot_handlers
from app.queue.registry import JobContext

FASTA = (
    ">sp|P0DTC2|SPIKE_SARS2 Spike glycoprotein OS=SARS-CoV-2 OX=2697049\n"
    "MFVFLVLLPLVSSQCVNLTTRTQLPPAYTNSFTRGVYYPDKVFRSSVLHS\n"
    ">sp|P00533|EGFR_HUMAN Epidermal growth factor receptor OS=Homo sapiens\n"
    "MRPSGTAGAALLALLAALCPASRALEEKKVCQGTSNKLTQLGTFEDHFLS\n"
)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """A JobContext whose scratch directory is a tmp_path."""
    monkeypatch.setattr(
        uniprot_handlers, "_prepare_workdir", lambda ctx, kind: tmp_path
    )
    return JobContext(
        job_id="job-1",
        payload={
            "project_id": "507f1f77bcf86cd799439011",
            "query": "accession:P0DTC2 OR accession:P00533",
            "filename": "uniprot_2_proteins.fasta",
            "accessions": ["P0DTC2", "P00533"],
            "reviewed_only": True,
        },
        epoch=1,
        attempts=1,
        cancel_event=threading.Event(),
    )


@pytest.fixture
def stub_fetch(monkeypatch):
    """Replace the network with a gzipped FASTA body and a release header."""
    state = {"body": gzip.compress(FASTA.encode()), "release": "2026_02"}

    def fake_fetch(url, *, timeout=0.0):
        return state["body"], {"X-UniProt-Release": state["release"]}

    monkeypatch.setattr(uniprot_handlers, "_fetch", fake_fetch)
    return state


class TestDownload:
    def test_it_writes_the_fasta_and_reports_it(self, ctx, stub_fetch, tmp_path):
        result = uniprot_handlers.download_uniprot(ctx)

        staged = result["staged"]
        assert len(staged) == 1
        assert staged[0]["name"] == "uniprot_2_proteins.fasta"
        written = Path(staged[0]["path"]).read_text()
        assert written.startswith(">sp|P0DTC2|")
        assert written.count(">") == 2

    def test_it_counts_the_records_it_actually_got(self, ctx, stub_fetch):
        """Counted from the file rather than trusted from the request,
        because `X-Total-Results` and the delivered count differ slightly --
        human reviewed reported 20,416 and delivered 20,427."""
        result = uniprot_handlers.download_uniprot(ctx)
        assert result["protein_count"] == 2

    def test_it_records_the_uniprot_release(self, ctx, stub_fetch):
        """Real provenance about specific bytes: which release these
        sequences came from."""
        result = uniprot_handlers.download_uniprot(ctx)
        assert result["release"] == "2026_02"

    def test_a_missing_release_header_is_not_fatal(self, ctx, stub_fetch):
        def no_header(url, *, timeout=0.0):
            return gzip.compress(FASTA.encode()), {}

        import app.queue.uniprot_handlers as mod

        mod._fetch = no_header
        result = uniprot_handlers.download_uniprot(ctx)
        assert result["release"] is None

    def test_an_empty_response_is_retryable(self, ctx, stub_fetch):
        """Zero records where records were requested. Better caught here
        than as an ingest of nothing several steps later."""
        stub_fetch["body"] = gzip.compress(b"")

        with pytest.raises(RetryableError, match="no sequences"):
            uniprot_handlers.download_uniprot(ctx)

    def test_a_response_that_is_not_fasta_is_retryable(self, ctx, stub_fetch):
        """UniProt returns an HTML error page under load. Ingesting it would
        create an object that looks like a FASTA and is not."""
        stub_fetch["body"] = gzip.compress(b"<html><body>Service busy</body></html>")

        with pytest.raises(RetryableError, match="no sequences"):
            uniprot_handlers.download_uniprot(ctx)

    def test_a_missing_query_is_permanent(self, ctx):
        """A retry cannot fix a payload with no query in it."""
        ctx.payload.pop("query")

        with pytest.raises(PermanentError, match="query"):
            uniprot_handlers.download_uniprot(ctx)

    def test_a_missing_project_is_permanent(self, ctx):
        ctx.payload.pop("project_id")

        with pytest.raises(PermanentError, match="project_id"):
            uniprot_handlers.download_uniprot(ctx)

    def test_a_client_error_is_permanent(self, ctx, monkeypatch):
        """A 400 means the query is wrong. Measured: UniProt answers a
        malformed query with 400 in about a second, so three retries would
        spend up to fifteen minutes rediscovering it."""
        import io
        import urllib.error

        def bad_request(url, *, timeout=0.0):
            raise urllib.error.HTTPError(
                url, 400, "Bad Request", {},
                io.BytesIO(b"Error messages\nquery parameter has an invalid syntax"),
            )

        monkeypatch.setattr(uniprot_handlers, "_fetch", bad_request)

        with pytest.raises(PermanentError, match="rejected"):
            uniprot_handlers.download_uniprot(ctx)

    def test_a_server_error_is_retryable(self, ctx, monkeypatch):
        """503 is UniProt being busy, which the next attempt may not hit."""
        import io
        import urllib.error

        def unavailable(url, *, timeout=0.0):
            raise urllib.error.HTTPError(
                url, 503, "Service Unavailable", {}, io.BytesIO(b"busy")
            )

        monkeypatch.setattr(uniprot_handlers, "_fetch", unavailable)

        with pytest.raises(RetryableError):
            uniprot_handlers.download_uniprot(ctx)

    def test_cancellation_is_observed(self, ctx, stub_fetch):
        from app.errors import JobCancelled

        ctx.cancel_event.set()

        with pytest.raises(JobCancelled):
            uniprot_handlers.download_uniprot(ctx)


class TestUncompressed:
    def test_a_plain_body_is_handled(self, ctx, monkeypatch):
        """`compressed=true` is a request, not a guarantee; a proxy may hand
        back plain text."""
        monkeypatch.setattr(
            uniprot_handlers,
            "_fetch",
            lambda url, *, timeout=0.0: (FASTA.encode(), {}),
        )

        result = uniprot_handlers.download_uniprot(ctx)

        assert result["protein_count"] == 2
