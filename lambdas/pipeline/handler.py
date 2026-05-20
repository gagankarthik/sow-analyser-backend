"""Unified pipeline Lambda entry point.

Dispatches to the stage named by PIPELINE_STAGE env var.  All seven stages
(parse → classify → embed → graph → diff → timeline → persist) share this
single Lambda package; Step Functions invokes each with a different
PIPELINE_STAGE value.
"""
from __future__ import annotations

import importlib
import os
from typing import Any

from aws_lambda_powertools import Tracer
from shared.logger import get_logger

log = get_logger("blue-iq.pipeline")
tracer = Tracer(service="blue-iq.pipeline")

_STAGE = os.environ.get("PIPELINE_STAGE", "")

_STAGE_MAP: dict[str, str] = {
    "01_parse":     "stages.parse",
    "02_classify":  "stages.classify",
    "03_embed":     "stages.embed",
    "04_graph":     "stages.graph",
    "05_diff":      "stages.diff",
    "06_timeline":  "stages.timeline",
    "07_persist":   "stages.persist",
}


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    module_path = _STAGE_MAP.get(_STAGE)
    if not module_path:
        raise ValueError(f"Unknown PIPELINE_STAGE: {_STAGE!r}")
    # Lazy import — only the active stage module is loaded on cold start.
    mod = importlib.import_module(module_path)
    try:
        return mod.run(event)
    except Exception as exc:
        log.exception("pipeline.stage_failed", stage=_STAGE, error=str(exc))
        raise
