"""Runtime configuration loaded from environment variables.

Design decision: we use plain `os.environ` instead of `pydantic-settings` to
avoid adding another dependency to `shared/requirements.txt` (which the
infra/CDK agent owns).  Validation is done at first access via a small
dataclass-like Settings object.

All values are read once at module import.  Override in tests by mutating the
`settings` singleton.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and (val is None or val == ""):
        # Don't raise at import time — many tests run without these.  Instead
        # store an empty string; the caller will see a clear error when it
        # tries to actually use the value.
        return ""
    return val or ""


def _int(name: str, default: int) -> int:
    """Integer setting; unset, empty or unparsable falls back to the default."""
    try:
        return int(float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class Settings:
    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", "us-east-2"))
    project_name: str = field(default_factory=lambda: _env("PROJECT_NAME", "blue-iq-sow"))
    stage: str = field(default_factory=lambda: _env("STAGE", "dev"))

    table_name: str = field(default_factory=lambda: _env("DDB_TABLE_NAME") or _env("TABLE_NAME", ""))
    raw_bucket: str = field(default_factory=lambda: _env("RAW_BUCKET", ""))
    processed_bucket: str = field(default_factory=lambda: _env("PROCESSED_BUCKET", ""))

    # OpenAI key read directly from env — injected by Lambda or local .env.
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY", ""))
    opensearch_endpoint: str = field(default_factory=lambda: _env("OPENSEARCH_ENDPOINT", ""))

    # Cognito user pool — used to invite users to a tenant/project.
    cognito_user_pool_id: str = field(
        default_factory=lambda: _env("COGNITO_USER_POOL_ID") or _env("USER_POOL_ID", "")
    )

    # ── Models, one per task ──────────────────────────────────────────────────
    # CHAT_MODEL is the default for every text task; each task can be pointed at
    # a different model without touching the others. An unset / empty override
    # inherits (see the *_for properties below). Nothing here guesses a model
    # name beyond the two defaults the deployment already used.
    embedding_model: str = field(
        default_factory=lambda: _env("EMBEDDING_MODEL") or "text-embedding-3-small"
    )
    chat_model: str = field(default_factory=lambda: _env("CHAT_MODEL") or "gpt-4.1-mini")
    extraction_model: str = field(default_factory=lambda: _env("EXTRACTION_MODEL", ""))
    clause_model: str = field(default_factory=lambda: _env("CLAUSE_MODEL", ""))
    validation_model: str = field(default_factory=lambda: _env("VALIDATION_MODEL", ""))
    rag_model: str = field(default_factory=lambda: _env("RAG_MODEL", ""))

    # Vector size of the clause index. It must equal what EMBEDDING_MODEL returns:
    # the embed stage refuses to write a vector of any other size, and refuses to
    # write at all when the live index was created with a different size.
    embedding_dimensions: int = field(default_factory=lambda: _int("EMBEDDING_DIMENSIONS", 1536))
    # Send `dimensions=` to the embeddings API so a model with a larger native
    # size can fill this index. Only for models that accept the parameter.
    embedding_send_dimensions: bool = field(
        default_factory=lambda: _bool("EMBEDDING_SEND_DIMENSIONS", False)
    )
    # "json_schema" (strict structured outputs) or "json_object" (for models that
    # only support plain JSON mode; the schema is then sent in the prompt and the
    # reply is checked in code). The client also falls back automatically when a
    # model rejects json_schema.
    structured_output_mode: str = field(
        default_factory=lambda: (_env("STRUCTURED_OUTPUT_MODE") or "json_schema").lower()
    )

    # Index names
    clause_vector_index: str = field(
        default_factory=lambda: _env("CLAUSE_VECTOR_INDEX", "clause-vectors")
    )
    clause_text_index: str = field(
        default_factory=lambda: _env("CLAUSE_TEXT_INDEX", "clause-text")
    )

    # ── OpenAI call behaviour ─────────────────────────────────────────────────
    # Per-request timeouts: one hung call must not eat the Lambda's budget.
    openai_timeout_s: float = field(default_factory=lambda: _float("OPENAI_TIMEOUT_S", 180.0))
    openai_embed_timeout_s: float = field(default_factory=lambda: _float("OPENAI_EMBED_TIMEOUT_S", 60.0))
    # Total tries per call (first attempt + retries) on rate limits, timeouts,
    # connection errors, 5xx and unusable output.
    openai_max_attempts: int = field(default_factory=lambda: _int("OPENAI_MAX_ATTEMPTS", 4))
    # Simultaneous OpenAI requests inside one stage invocation.
    llm_max_concurrency: int = field(default_factory=lambda: max(1, _int("LLM_MAX_CONCURRENCY", 4)))

    # ── Extraction (classify stage) ───────────────────────────────────────────
    # "segmented": clauses are cut out of the document in code and the model only
    # labels them (complete by construction). "legacy": the model finds and copies
    # the clauses itself (kept as a switch; falls back to segmented when its
    # coverage of the document is low).
    classify_mode: str = field(default_factory=lambda: (_env("CLASSIFY_MODE") or "segmented").lower())
    # Largest document slice sent in ONE extraction request. A longer document is
    # split into overlapping windows whose results are merged — never truncated.
    classify_max_input_tokens: int = field(
        default_factory=lambda: _int("CLASSIFY_MAX_INPUT_TOKENS", 60000)
    )
    classify_batch_chars: int = field(default_factory=lambda: _int("CLASSIFY_BATCH_CHARS", 12000))
    classify_batch_clauses: int = field(default_factory=lambda: _int("CLASSIFY_BATCH_CLAUSES", 16))
    max_clause_chars: int = field(default_factory=lambda: _int("MAX_CLAUSE_CHARS", 8000))
    # Below this share of the document's words inside the clauses, segmentation
    # is redone with the structure-free fallback and the document is flagged.
    min_coverage_ratio: float = field(default_factory=lambda: _float("MIN_COVERAGE_RATIO", 0.98))
    # Re-analysing a byte-identical file with the same engine reuses the previous
    # model output (deterministic steps always re-run).
    classify_reuse_unchanged: bool = field(
        default_factory=lambda: _bool("CLASSIFY_REUSE_UNCHANGED", True)
    )

    # ── Retrieval (embed stage) ───────────────────────────────────────────────
    embedding_batch_size: int = field(
        default_factory=lambda: _int("EMBEDDING_BATCH_SIZE", 100)
    )
    embed_chunk_chars: int = field(default_factory=lambda: _int("EMBED_CHUNK_CHARS", 1800))
    embed_chunk_overlap: int = field(default_factory=lambda: _int("EMBED_CHUNK_OVERLAP", 200))
    # Output budget for structured-output (JSON) chat calls. Must not exceed the
    # chosen model's completion limit. A reply that still hits the limit is never
    # parsed as-is: the caller retries with CHAT_MAX_OUTPUT_TOKENS_MAX (document
    # extraction) or splits the batch in half (clause labelling).
    chat_max_output_tokens: int = field(
        default_factory=lambda: _int("CHAT_MAX_OUTPUT_TOKENS", 16000)
    )
    # Upper output budget used on the one-shot retry after a `length` stop. Set
    # it to the completion limit of the extraction model.
    chat_max_output_tokens_max: int = field(
        default_factory=lambda: _int("CHAT_MAX_OUTPUT_TOKENS_MAX", 32000)
    )
    diff_impact_call_cap: int = field(
        default_factory=lambda: _int("DIFF_IMPACT_CALL_CAP", 10)
    )
    # Combined match score floor for linking an amendment to its parent. The four
    # signal weights (reference 0.25 / hybrid 0.45 / structural 0.18 / title 0.12)
    # rarely all fire together, so a 0.7 floor rejected legitimate parents (e.g. a
    # named, title-matching parent with good hybrid recall but a different clause
    # structure scores ~0.59). 0.5 requires real evidence without demanding
    # near-universal agreement across every signal.
    parent_match_min_confidence: float = field(
        default_factory=lambda: float(_env("PARENT_MATCH_MIN_CONFIDENCE", "0.5"))
    )

    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    # ── Guardrails / data protection ──────────────────────────────────────────
    # Which AI provider receives client data. Must be on the no-train allowlist
    # in shared/guardrails.py or the client wrapper fails closed.
    ai_provider: str = field(default_factory=lambda: _env("AI_PROVIDER", "openai"))
    # Master switch for PII redaction. The no-train allowlist + audit log are
    # always enforced regardless of this flag.
    guardrails_enabled: bool = field(
        default_factory=lambda: _env("GUARDRAILS_ENABLED", "true").lower()
        not in ("0", "false", "no", "off")
    )
    # Comma-separated entity classes to pseudonymise before text leaves AWS.
    # MONEY / DATE / PARTY are intentionally excluded by default: the extraction
    # stage exists to read those very figures, so redacting them there would
    # break the product. Enable per environment as policy requires.
    redact_classes: str = field(
        default_factory=lambda: _env("REDACT_CLASSES", "EMAIL,PHONE,SSN,CREDIT_CARD,IP")
    )

    def redact_class_list(self) -> tuple[str, ...]:
        """REDACT_CLASSES parsed into an upper-cased tuple."""
        return tuple(c.strip().upper() for c in self.redact_classes.split(",") if c.strip())

    # ── Effective model per task (override → broader default) ────────────────
    def model_for(self, task: str) -> str:
        """Model for a task: "extraction" | "clause" | "validation" | "rag" | "chat"."""
        extraction = self.extraction_model or self.chat_model
        if task == "extraction":
            return extraction
        if task == "clause":
            return self.clause_model or extraction
        if task == "validation":
            return self.validation_model or extraction
        if task == "rag":
            return self.rag_model or self.chat_model
        return self.chat_model


# Module-level singleton.  Tests can mutate fields on this in place.
settings = Settings()
