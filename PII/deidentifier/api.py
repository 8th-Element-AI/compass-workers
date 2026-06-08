"""
FastAPI service for de-identification.

Loads PresidioEngine once at startup, then handles every request with no
cold-start delay.

Environment variables:
    DEIDENTIFIER_NER_MODEL  NER model to use. Can be a HuggingFace model ID
                            (e.g. gravitee-io/bert-small-pii-detection) or a
                            spaCy model name (e.g. en_core_web_lg).
                            Default: gravitee-io/bert-small-pii-detection.
                            Set to empty string for regex-only mode.

Run:
    uvicorn deidentifier.api:app --host 0.0.0.0 --port 8000 --reload

With a different model:
    DEIDENTIFIER_NER_MODEL=en_core_web_lg uvicorn deidentifier.api:app --port 8000
    DEIDENTIFIER_NER_MODEL= uvicorn deidentifier.api:app --port 8000  # regex-only
"""
from __future__ import annotations

import os
import time
import logging
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Empty string env var → regex-only (ner_model=None); unset → gravitee-io default
_raw = os.getenv("DEIDENTIFIER_NER_MODEL", "gravitee-io/bert-small-pii-detection")
_NER_MODEL: Optional[str] = _raw if _raw else None

_engine = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine
    from .presidio.engine import PresidioEngine
    logger.info("Loading PresidioEngine (ner_model=%s)…", _NER_MODEL or "regex-only")
    t0 = time.perf_counter()
    _engine = PresidioEngine.get_instance(
        ner_model=_NER_MODEL,
    )
    logger.info("Engine ready in %.2fs", time.perf_counter() - t0)
    yield
    _engine = None


app = FastAPI(
    title="De-identification API",
    description="PHI/PII de-identification backed by Presidio + optional HuggingFace NER",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class DeidentifyRequest(BaseModel):
    text: str
    document_id: Optional[str] = None


class AuditEntry(BaseModel):
    entity_type: str
    strategy: str
    start: int
    end: int
    score: float


class DeidentifyResponse(BaseModel):
    document_id: str
    deidentified_text: str
    entities_found: int
    entities_processed: int
    processing_time_ms: float
    audit_entries: List[AuditEntry]


class BatchRequest(BaseModel):
    texts: List[str]
    document_ids: Optional[List[str]] = None


class BatchResponse(BaseModel):
    results: List[DeidentifyResponse]
    total_processing_time_ms: float


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "ner_model": _NER_MODEL or "regex-only",
        "ready": _engine is not None,
    }


def _build_response(result, elapsed_ms: float) -> DeidentifyResponse:
    return DeidentifyResponse(
        document_id=result.document_id,
        deidentified_text=result.deidentified_text,
        entities_found=result.audit_record.entities_found,
        entities_processed=result.audit_record.entities_processed,
        processing_time_ms=round(elapsed_ms, 2),
        audit_entries=[
            AuditEntry(
                entity_type=e.entity_type,
                strategy=e.strategy,
                start=e.start,
                end=e.end,
                score=e.score,
            )
            for e in result.audit_record.entries
        ],
    )


@app.post("/deidentify", response_model=DeidentifyResponse)
def deidentify(req: DeidentifyRequest):
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not ready")
    t0 = time.perf_counter()
    result = _engine.process(req.text, document_id=req.document_id)
    return _build_response(result, (time.perf_counter() - t0) * 1000)


@app.post("/deidentify/batch", response_model=BatchResponse)
def deidentify_batch(req: BatchRequest):
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not ready")
    if not req.texts:
        raise HTTPException(status_code=422, detail="texts list must not be empty")

    doc_ids = req.document_ids or [None] * len(req.texts)
    if len(doc_ids) != len(req.texts):
        raise HTTPException(
            status_code=422,
            detail="document_ids length must match texts length",
        )

    t0 = time.perf_counter()
    responses = []
    for text, doc_id in zip(req.texts, doc_ids):
        t_doc = time.perf_counter()
        result = _engine.process(text, document_id=doc_id)
        responses.append(_build_response(result, (time.perf_counter() - t_doc) * 1000))

    return BatchResponse(
        results=responses,
        total_processing_time_ms=round((time.perf_counter() - t0) * 1000, 2),
    )


@app.post(
    "/deidentify/plain",
    response_model=DeidentifyResponse,
    summary="Deidentify plain text (paste multiline text directly)",
)
def deidentify_plain(text: str = Body(..., media_type="text/plain")):
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not ready")
    t0 = time.perf_counter()
    result = _engine.process(text)
    return _build_response(result, (time.perf_counter() - t0) * 1000)
