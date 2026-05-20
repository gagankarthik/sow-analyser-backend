"""Pipeline Lambda entry point — single function, seven stages.

Step Functions injects `_stage` via States.JsonMerge. The handler pops it,
dispatches to the matching stage module, and returns the enriched event so the
next state receives a clean pipeline payload.

Failure contract: any unhandled exception bubbles up; Step Functions catches it
and routes to the MarkFailed state, which writes status=FAILED to DynamoDB.
"""
from __future__ import annotations

import importlib
import os
from typing import Any

from aws_lambda_powertools import Tracer
from shared.logger import get_logger

log = get_logger("blue-iq.pipeline")
tracer = Tracer(service="blue-iq.pipeline")

_STAGES: dict[str, str] = {
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
    stage = event.pop("_stage", None) or os.environ.get("PIPELINE_STAGE", "")
    if not stage:
        raise ValueError("_stage must be present in the event or PIPELINE_STAGE env var")

    module_path = _STAGES.get(stage)
    if not module_path:
        raise ValueError(f"Unknown pipeline stage: {stage!r}. Valid: {list(_STAGES)}")

    log.append_keys(stage=stage, docId=event.get("docId", "?"))

    # Surface remaining Lambda time for timeout-aware stages.
    if context and hasattr(context, "get_remaining_time_in_millis"):
        event["_remainingMs"] = context.get_remaining_time_in_millis()

    mod = importlib.import_module(module_path)
    try:
        return mod.run(event)
    except Exception as exc:
        log.exception("pipeline.stage_failed", stage=stage, error=str(exc))
        raise
