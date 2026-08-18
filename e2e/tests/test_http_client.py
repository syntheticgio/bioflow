import asyncio
import json

import httpx

from e2e.backend import http_client


def _patch(monkeypatch, handler):
    RealClient = httpx.AsyncClient
    monkeypatch.setattr(
        http_client.httpx, "AsyncClient",
        lambda *a, **k: RealClient(transport=httpx.MockTransport(handler)),
    )


def test_upload_sends_correct_request(monkeypatch, tmp_path):
    fixture = tmp_path / "reads.fastq"
    fixture.write_bytes(b"DATA")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["fn"] = request.headers["X-Filename"]
        seen["profile"] = request.headers.get("X-BioFlow-Profile")
        seen["body"] = request.content
        return httpx.Response(201, json={"id": "o1", "format": "fastq"})

    _patch(monkeypatch, handler)

    result = asyncio.run(
        http_client.upload_object("http://bf:8000", "prof1", "p1", str(fixture))
    )
    assert result == {"id": "o1", "format": "fastq"}
    assert seen["url"] == "http://bf:8000/api/v1/projects/p1/objects/upload"
    assert seen["fn"] == "reads.fastq"
    assert seen["profile"] == "prof1"
    assert seen["body"] == b"DATA"


def test_upload_omits_profile_header_when_empty(monkeypatch, tmp_path):
    fixture = tmp_path / "r.fastq"
    fixture.write_bytes(b"x")
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(201, json={"id": "o1"})

    _patch(monkeypatch, handler)

    asyncio.run(http_client.upload_object("http://bf:8000", "", "p1", str(fixture)))
    assert "X-BioFlow-Profile" not in seen["headers"]


def test_upload_non_201_raises(monkeypatch, tmp_path):
    fixture = tmp_path / "r.fastq"
    fixture.write_bytes(b"x")

    def handler(request):
        return httpx.Response(422, text="too large")

    _patch(monkeypatch, handler)

    try:
        asyncio.run(http_client.upload_object("http://bf:8000", "", "p1", str(fixture)))
        raise AssertionError("expected HttpUploadError")
    except http_client.HttpUploadError as e:
        assert "422" in str(e)


def test_launch_posts_card_body_under_the_api_prefix(monkeypatch):
    """A card's endpoint is router-relative; the harness must add /api/v1."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["profile"] = request.headers.get("X-BioFlow-Profile")
        return httpx.Response(201, json={"id": "j1", "state": "queued"})

    _patch(monkeypatch, handler)

    result = asyncio.run(
        http_client.launch_pipeline(
            "http://bf:8000", "prof1", "/pipelines/align", {"object_id": "o1"}
        )
    )
    assert result == {"id": "j1", "state": "queued"}
    assert seen["url"] == "http://bf:8000/api/v1/pipelines/align"
    # Parsed, not byte-compared: the separator spacing is httpx's business.
    assert json.loads(seen["body"]) == {"object_id": "o1"}
    assert seen["profile"] == "prof1"


def test_launch_does_not_double_prefix_an_absolute_endpoint(monkeypatch):
    """A card that already carries /api/v1 must not become /api/v1/api/v1."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(201, json={"id": "j1"})

    _patch(monkeypatch, handler)

    asyncio.run(
        http_client.launch_pipeline("http://bf:8000", "", "/api/v1/pipelines/qc", {})
    )
    assert seen["url"] == "http://bf:8000/api/v1/pipelines/qc"


def test_launch_non_201_raises_with_the_body(monkeypatch):
    """The endpoint's own validation message is the useful part of a failure."""

    def handler(request):
        return httpx.Response(422, text="object_id: field required")

    _patch(monkeypatch, handler)

    try:
        asyncio.run(
            http_client.launch_pipeline("http://bf:8000", "", "/pipelines/qc", {})
        )
        raise AssertionError("expected HttpLaunchError")
    except http_client.HttpLaunchError as e:
        assert "422" in str(e)
        assert "field required" in str(e)
