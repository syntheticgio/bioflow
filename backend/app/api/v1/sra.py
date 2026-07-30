"""Backwards-compatible `/sra/*` paths.

The implementation moved to `ncbi.py` when assemblies joined it. These paths
are kept because the frontend and any bookmarked request still use them.
"""

from fastapi import APIRouter, status

from app.api.v1.ncbi import SraAccepted, SraResolveResponse, sra_download, sra_resolve

router = APIRouter(prefix="/sra", tags=["sra"])
router.post("/resolve", response_model=SraResolveResponse)(sra_resolve)
router.post(
    "/download", response_model=SraAccepted, status_code=status.HTTP_202_ACCEPTED
)(sra_download)
