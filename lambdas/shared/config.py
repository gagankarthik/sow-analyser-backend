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

    embedding_model: str = field(
        default_factory=lambda: _env("EMBEDDING_MODEL", "text-embedding-3-small")
    )
    chat_model: str = field(default_factory=lambda: _env("CHAT_MODEL", "gpt-4.1-mini"))

    # Index names
    clause_vector_index: str = field(
        default_factory=lambda: _env("CLAUSE_VECTOR_INDEX", "clause-vectors")
    )
    clause_text_index: str = field(
        default_factory=lambda: _env("CLAUSE_TEXT_INDEX", "clause-text")
    )

    # Tuning knobs
    embedding_batch_size: int = field(
        default_factory=lambda: int(_env("EMBEDDING_BATCH_SIZE", "100"))
    )
    classify_max_input_tokens: int = field(
        default_factory=lambda: int(_env("CLASSIFY_MAX_INPUT_TOKENS", "30000"))
    )
    diff_impact_call_cap: int = field(
        default_factory=lambda: int(_env("DIFF_IMPACT_CALL_CAP", "10"))
    )
    parent_match_min_confidence: float = field(
        default_factory=lambda: float(_env("PARENT_MATCH_MIN_CONFIDENCE", "0.7"))
    )

    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))


# Module-level singleton.  Tests can mutate fields on this in place.
settings = Settings()
