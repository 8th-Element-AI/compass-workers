"""
FastAPI service for de-identification.

Loads PresidioEngine once at startup (pays the 7s cold-start cost once),
then handles every request in ~0.33s.

Run:
    uvicorn deidentifier.api:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import time
import logging
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel

from .presidio.engine import PresidioEngine

logger = logging.getLogger(__name__)

_engine: Optional[PresidioEngine] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine
    logger.info("Loading PresidioEngine...")
    t0 = time.perf_counter()
    _engine = PresidioEngine.get_instance(
        spacy_model="en_core_web_sm",
        hf_model="obi/deid_roberta_i2b2",
    )
    logger.info("PresidioEngine ready in %.2fs", time.perf_counter() - t0)
    yield
    _engine = None


app = FastAPI(
    title="De-identification API",
    description="PHI/PII de-identification backed by Microsoft Presidio + spaCy en_core_web_trf",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response schemas
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
        "engine": "presidio",
        "spacy_model": "en_core_web_trf",
        "ready": _engine is not None,
    }


@app.post("/deidentify", response_model=DeidentifyResponse)
def deidentify(req: DeidentifyRequest):
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not ready")

    t0 = time.perf_counter()
    result = _engine.process(req.text, document_id=req.document_id)
    elapsed_ms = (time.perf_counter() - t0) * 1000

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
    results = []
    for text, doc_id in zip(req.texts, doc_ids):
        t_doc = time.perf_counter()
        result = _engine.process(text, document_id=doc_id)
        elapsed_ms = (time.perf_counter() - t_doc) * 1000
        results.append(
            DeidentifyResponse(
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
        )

    total_ms = (time.perf_counter() - t0) * 1000
    return BatchResponse(results=results, total_processing_time_ms=round(total_ms, 2))


@app.post(
    "/deidentify/plain",
    response_model=DeidentifyResponse,
    summary="Deidentify plain text (paste multiline text directly)",
)
def deidentify_plain(text: str = Body(..., media_type="text/plain")):
    """
    Accepts raw text/plain body — no JSON encoding needed.
    Paste any multiline text directly in the Swagger text area.
    """
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not ready")

    t0 = time.perf_counter()
    result = _engine.process(text)
    elapsed_ms = (time.perf_counter() - t0) * 1000

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
