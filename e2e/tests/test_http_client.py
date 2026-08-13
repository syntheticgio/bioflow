import asyncio

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
        assert False, "expected HttpUploadError"
    except http_client.HttpUploadError as e:
        assert "422" in str(e)
