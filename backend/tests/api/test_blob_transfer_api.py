"""Tests for blob-transfer API endpoints on the objects router.

Creates a proper FastAPI app that registers the AppError exception handler
so NotFoundError → 404 and ValidationError → 422 work correctly.
"""

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1.objects import router
from app.errors import AppError, JSONResponse


def _patch_objects_dir(objects_dir: Path):
    """Patch settings.objects_dir and clear node_shared_secret."""
    patches = [
        patch("app.api.v1.objects.settings"),
        patch("app.storage.paths.settings"),
    ]
    managers = [p.__enter__() for p in patches]
    for m in managers:
        m.objects_dir = objects_dir
        m.node_shared_secret = ""
    return patches, managers


def _app():
    """Bare FastAPI app with the AppError exception handler registered."""
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    app.include_router(router)
    return app


@pytest.fixture
def objects_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "objects"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.mark.asyncio
async def test_get_blob_404_for_unknown_digest(objects_dir):
    digest = "a" * 64
    patches, _ = _patch_objects_dir(objects_dir)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as c:
            resp = await c.get(f"/objects/blob/{digest}")
            assert resp.status_code == 404
    finally:
        for p in patches:
            p.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_get_blob_returns_content(objects_dir):
    content = b"hello blob"
    digest = hashlib.sha256(content).hexdigest()
    blob = objects_dir / digest[:2] / digest
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(content)

    patches, _ = _patch_objects_dir(objects_dir)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as c:
            resp = await c.get(f"/objects/blob/{digest}")
            assert resp.status_code == 200
            assert resp.read() == content
    finally:
        for p in patches:
            p.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_get_blob_rejects_invalid_sha256(objects_dir):
    patches, _ = _patch_objects_dir(objects_dir)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as c:
            resp = await c.get("/objects/blob/not-a-valid-sha256")
            # Invalid digest format → 422 from ValidationError (caught by validate_sha256)
            assert resp.status_code == 422
    finally:
        for p in patches:
            p.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_put_blob_creates_new(objects_dir):
    content = b"new blob content"
    digest = hashlib.sha256(content).hexdigest()

    patches, _ = _patch_objects_dir(objects_dir)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as c:
            resp = await c.put(
                f"/objects/blob/{digest}",
                content=content,
                headers={"Content-Type": "application/octet-stream"},
            )
            assert resp.status_code == 201

            blob = objects_dir / digest[:2] / digest
            assert blob.read_bytes() == content

            get_resp = await c.get(f"/objects/blob/{digest}")
            assert get_resp.status_code == 200
            assert get_resp.read() == content
    finally:
        for p in patches:
            p.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_put_blob_idempotent(objects_dir):
    content = b"idempotent"
    digest = hashlib.sha256(content).hexdigest()

    patches, _ = _patch_objects_dir(objects_dir)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as c:
            resp1 = await c.put(
                f"/objects/blob/{digest}",
                content=content,
                headers={"Content-Type": "application/octet-stream"},
            )
            assert resp1.status_code == 201
            
            # Verify the file is on disk at the expected path.
            blob = objects_dir / digest[:2] / digest
            assert blob.is_file(), f"Expected {blob} to exist after first PUT"

            resp2 = await c.put(
                f"/objects/blob/{digest}",
                content=content,
                headers={"Content-Type": "application/octet-stream"},
            )
            # Both 200 (already exists) and 201 (created) are acceptable;
            # the key property is idempotency — the blob exists after both.
            assert resp2.status_code in (200, 201)
    finally:
        for p in patches:
            p.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_put_blob_rejects_sha256_mismatch(objects_dir):
    content = b"actual content"
    wrong_digest = hashlib.sha256(b"different content").hexdigest()

    patches, _ = _patch_objects_dir(objects_dir)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as c:
            resp = await c.put(
                f"/objects/blob/{wrong_digest}",
                content=content,
                headers={"Content-Type": "application/octet-stream"},
            )
            # SHA-256 mismatch → 422 ValidationError
            assert resp.status_code == 422
    finally:
        for p in patches:
            p.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_blob_endpoints_require_secret_when_configured(objects_dir):
    digest = "a" * 64
    patches, managers = _patch_objects_dir(objects_dir)
    for m in managers:
        m.node_shared_secret = "test-secret"

    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as c:
            resp = await c.get(f"/objects/blob/{digest}")
            assert resp.status_code == 403
            assert "X-Node-Secret" in resp.text
    finally:
        for p in patches:
            p.__exit__(None, None, None)
