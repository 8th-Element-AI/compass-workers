"""
FastAPI service for PII detection.

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
from typing import Dict, List, Optional, Tuple

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
    title="PII Detection API",
    description="PHI/PII detection backed by Presidio + optional HuggingFace NER",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    text: str
    document_id: Optional[str] = None


class AnalyzeResponse(BaseModel):
    document_id: Optional[str] = None
    has_pii: bool
    entity_count: int
    entities: Dict[str, int]
    processing_time_ms: float


class BatchRequest(BaseModel):
    texts: List[str]
    document_ids: Optional[List[str]] = None


class BatchResponse(BaseModel):
    results: List[AnalyzeResponse]
    total_processing_time_ms: float


class ViolationModel(BaseModel):
    kind: str
    severity: str
    rule_name: str
    entity_types: List[str]
    span: Tuple[int, int]
    score: float


class EvaluateResponse(BaseModel):
    document_id: Optional[str] = None
    has_violation: bool
    max_severity: str
    violations: List[ViolationModel]
    processing_time_ms: float


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


def _build_response(result, elapsed_ms: float, document_id: Optional[str] = None) -> AnalyzeResponse:
    return AnalyzeResponse(
        document_id=document_id,
        has_pii=result.has_pii,
        entity_count=result.entity_count,
        entities=result.entities,
        processing_time_ms=round(elapsed_ms, 2),
    )


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not ready")
    t0 = time.perf_counter()
    result = _engine.analyze(req.text)
    return _build_response(result, (time.perf_counter() - t0) * 1000, req.document_id)


@app.post("/analyze/batch", response_model=BatchResponse)
def analyze_batch(req: BatchRequest):
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
    results = _engine.analyze_batch(req.texts)
    elapsed_total = (time.perf_counter() - t0) * 1000
    per_doc_ms = elapsed_total / len(req.texts)
    responses = [
        _build_response(result, per_doc_ms, doc_id)
        for result, doc_id in zip(results, doc_ids)
    ]

    return BatchResponse(
        results=responses,
        total_processing_time_ms=round(elapsed_total, 2),
    )


@app.post(
    "/analyze/plain",
    response_model=AnalyzeResponse,
    summary="Analyze plain text (paste multiline text directly)",
)
def analyze_plain(text: str = Body(..., media_type="text/plain")):
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not ready")
    t0 = time.perf_counter()
    result = _engine.analyze(text)
    return _build_response(result, (time.perf_counter() - t0) * 1000)


def _build_evaluate_response(result, elapsed_ms: float, document_id: Optional[str] = None) -> EvaluateResponse:
    return EvaluateResponse(
        document_id=document_id,
        has_violation=result.has_violation,
        max_severity=result.max_severity.value,
        violations=[
            ViolationModel(
                kind=v.kind.value,
                severity=v.severity.value,
                rule_name=v.rule_name,
                entity_types=[e.entity_type for e in v.entities],
                span=v.span,
                score=round(v.score, 4),
            )
            for v in result.violations
        ],
        processing_time_ms=round(elapsed_ms, 2),
    )


@app.post("/evaluate", response_model=EvaluateResponse)
def evaluate(req: AnalyzeRequest):
    """Detect, then score entity combinations for re-identification risk.
    Detection-only — does not anonymize."""
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not ready")
    t0 = time.perf_counter()
    result = _engine.evaluate(req.text)
    return _build_evaluate_response(result, (time.perf_counter() - t0) * 1000, req.document_id)
