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

    # Cognito user pool — used to invite users to a tenant/project.
    cognito_user_pool_id: str = field(
        default_factory=lambda: _env("COGNITO_USER_POOL_ID") or _env("USER_POOL_ID", "")
    )

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
    # Output budget for structured-output (JSON) chat calls. A full contract
    # extraction with many verbatim clauses is large; too small a cap silently
    # truncates the JSON and drops trailing clauses. gpt-4.1-mini supports up to
    # 32k completion tokens — default high so long docs aren't cut off.
    chat_max_output_tokens: int = field(
        default_factory=lambda: int(_env("CHAT_MAX_OUTPUT_TOKENS", "16000"))
    )
    # Upper output budget used on a one-shot retry when the first extraction
    # truncated on `length`. gpt-4.1-mini caps completions at 32k tokens.
    chat_max_output_tokens_max: int = field(
        default_factory=lambda: int(_env("CHAT_MAX_OUTPUT_TOKENS_MAX", "32000"))
    )
    diff_impact_call_cap: int = field(
        default_factory=lambda: int(_env("DIFF_IMPACT_CALL_CAP", "10"))
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


# Module-level singleton.  Tests can mutate fields on this in place.
settings = Settings()
