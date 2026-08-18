"""ESMFold structure prediction sidecar.

FastAPI service that loads an ESMFold model at startup and exposes
POST /predict for single-sequence structure prediction.
"""
import logging
import time
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

_VALID_AA = set("ACDEFGHIKLMNPQRSTVWY")

model = None
model_version = "unknown"


class PredictRequest(BaseModel):
    sequence: str = Field(..., min_length=20, max_length=2000)

    @field_validator("sequence")
    @classmethod
    def validate_sequence(cls, v):
        v = v.strip().upper()
        invalid = set(v) - _VALID_AA
        if invalid:
            raise ValueError(f"Invalid amino acids: {''.join(sorted(invalid))}")
        return v


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, model_version
    log.info("Loading ESMFold model…")
    start = time.time()
    try:
        import esm

        model = esm.pretrained.esmfold_v1()
        model = model.eval()
        model_version = getattr(esm, "__version__", "unknown")
        if torch.cuda.is_available():
            model = model.cuda()
            log.info("Using CUDA GPU")
        else:
            log.info("Using CPU (no CUDA detected)")
        log.info(f"Model loaded in {time.time() - start:.1f}s, version={model_version}")
    except Exception as e:
        log.error(f"Failed to load ESMFold model: {e}")
        raise
    yield


app = FastAPI(title="ESMFold Sidecar", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model_loaded": model is not None,
        "model_version": model_version,
    }


@app.post("/predict", response_class=Response)
async def predict(request: PredictRequest):
    if model is None:
        raise HTTPException(status_code=503, detail="Model is still loading")

    seq = request.sequence.strip().upper()
    log.info(f"Predicting structure for sequence of length {len(seq)}")

    start = time.time()
    try:
        with torch.no_grad():
            output = model.infer(seq)

        pdb_str = model.output_to_pdb(output)
        inference_time = time.time() - start
        log.info(f"Prediction complete in {inference_time:.1f}s, length={len(seq)}")

        return Response(
            content=pdb_str,
            media_type="chemical/x-pdb",
            headers={
                "X-Inference-Time-S": f"{inference_time:.1f}",
                "X-Model-Version": model_version,
                "X-Sequence-Length": str(len(seq)),
            },
        )
    except Exception as e:
        log.error(f"Prediction failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
