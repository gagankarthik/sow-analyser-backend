"""Pydantic v2 schema for pipeline data.

These models double as:
  1. Validation at stage boundaries (defensive parsing of upstream output)
  2. The source-of-truth for what gets persisted to DynamoDB / S3
  3. Inputs to the OpenAI structured-output JSON schemas (see `02_classify/prompts.py`)
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DocType(str, Enum):
    SOW = "SOW"
    MSA = "MSA"
    AMENDMENT = "AMENDMENT"
    NDA = "NDA"
    OTHER = "OTHER"


class Lifecycle(str, Enum):
    DRAFT = "draft"
    REVIEW = "review"
    NEGOTIATION = "negotiation"
    APPROVAL = "approval"
    SIGNED = "signed"
    ACTIVE = "active"
    RENEWAL = "renewal"
    EXPIRED = "expired"


class ExtractionMethod(str, Enum):
    PDFPLUMBER = "pdfplumber"
    TEXTRACT = "textract"
    DOCX = "docx"


class ProcessingStatus(str, Enum):
    PENDING = "PENDING"
    PARSING = "PARSING"
    CLASSIFYING = "CLASSIFYING"
    EMBEDDING = "EMBEDDING"
    GRAPHING = "GRAPHING"
    DIFFING = "DIFFING"
    TIMELINING = "TIMELINING"
    PERSISTING = "PERSISTING"
    READY = "READY"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Pipeline payloads (per-stage)
# ---------------------------------------------------------------------------


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ParsedPage(_Base):
    page: int
    text: str
    char_count: int = 0


class ParsedDocument(_Base):
    text: str
    pages: list[ParsedPage] = Field(default_factory=list)
    extracted_at: str
    extraction_method: ExtractionMethod
    checksum: str


class Clause(_Base):
    number: str
    title: str
    body: str
    category: str = "Other"


class Classification(_Base):
    docType: DocType
    title: str
    parties: list[str] = Field(default_factory=list)
    effectiveDate: str | None = None
    lifecycle: Lifecycle = Lifecycle.DRAFT
    structuralHash: str
    clauses: list[Clause] = Field(default_factory=list)


class Embeddings(_Base):
    clauseVectorIds: list[str] = Field(default_factory=list)
    embeddedCount: int = 0


class Lineage(_Base):
    parentDocId: str | None = None
    matchConfidence: float = 0.0
    matchReason: str = ""


class Change(_Base):
    changeId: str
    clauseNumber: str
    field: str  # "title" | "body" | "category"
    before: str
    after: str
    impactScore: int = 0
    impactRationale: str = ""


class Diffs(_Base):
    changes: list[Change] = Field(default_factory=list)
    impactSummary: str = ""


class TimelineSnapshot(_Base):
    initialState: dict[str, Any] = Field(default_factory=dict)
    currentState: dict[str, Any] = Field(default_factory=dict)
    amendmentChain: list[dict[str, Any]] = Field(default_factory=list)
    futureState: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Persistence records (DynamoDB items)
# ---------------------------------------------------------------------------


class Document(_Base):
    """`PK = DOC#<docId>`, `SK = META`."""

    docId: str
    tenantId: str
    title: str
    docType: DocType
    lifecycle: Lifecycle
    status: ProcessingStatus
    parties: list[str] = Field(default_factory=list)
    effectiveDate: str | None = None
    parentDocId: str | None = None
    rawKey: str
    processedPrefix: str
    structuralHash: str
    checksum: str
    latestVersion: int = 1
    createdAt: str
    updatedAt: str


class Version(_Base):
    """`PK = DOC#<docId>`, `SK = V#<n>`."""

    docId: str
    versionNumber: int
    extractionMethod: ExtractionMethod
    classificationKey: str  # S3 key
    parsedKey: str
    timelineKey: str | None = None
    createdAt: str


# ---------------------------------------------------------------------------
# Pipeline event (the JSON passed by Step Functions)
# ---------------------------------------------------------------------------


class PipelineEvent(_Base):
    docId: str
    tenantId: str
    rawBucket: str
    rawKey: str
    processedBucket: str

    parsed: ParsedDocument | None = None
    classification: Classification | None = None
    embeddings: Embeddings | None = None
    lineage: Lineage | None = None
    diffs: Diffs | None = None
    timeline: TimelineSnapshot | None = None


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"
