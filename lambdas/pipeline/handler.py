"""Unified pipeline Lambda entry point — single function, all seven stages.

Step Functions injects `_stage` into the event payload via States.JsonMerge.
The handler pops it, dispatches to the correct stage module, and returns the
enriched event so the next state receives a clean pipeline event.
"""
from __future__ import annotations

import importlib
import os
from typing import Any

from aws_lambda_powertools import Tracer
from shared.logger import get_logger

log = get_logger("blue-iq.pipeline")
tracer = Tracer(service="blue-iq.pipeline")

_STAGE_MAP: dict[str, str] = {
    "01_parse":    "stages.parse",
    "02_classify": "stages.classify",
    "03_embed":    "stages.embed",
    "04_graph":    "stages.graph",
    "05_diff":     "stages.diff",
    "06_timeline": "stages.timeline",
    "07_persist":  "stages.persist",
}


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # _stage is injected by Step Functions via States.JsonMerge; fall back to
    # PIPELINE_STAGE env var so local/test invocations still work.
    stage = event.pop("_stage", None) or os.environ.get("PIPELINE_STAGE", "")
    if not stage:
        raise ValueError("_stage must be present in the event payload or PIPELINE_STAGE env var")

    module_path = _STAGE_MAP.get(stage)
    if not module_path:
        raise ValueError(f"Unknown pipeline stage: {stage!r}")

    log.append_keys(stage=stage)
    mod = importlib.import_module(module_path)
    try:
        return mod.run(event)
    except Exception as exc:
        log.exception("pipeline.stage_failed", stage=stage, error=str(exc))
        raise
