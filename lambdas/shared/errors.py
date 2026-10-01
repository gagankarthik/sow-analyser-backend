"""Errors whose message is safe — and useful — to show to the person who uploaded
the document.

A pipeline failure is stored on the document and read back by the UI. Raw
exception text (stack traces, SDK messages, bucket names) must never reach it,
so the pipeline handler converts every failure into one of these:

* ``UserFacingError``  — raised deliberately by a stage, message written for
  the user ("This file is 80 MB. The limit is 50 MB.").
* ``PipelineStageError`` — the wrapper the handler raises for anything else,
  carrying a generic, stage-appropriate message and a reference id for support.
"""
from __future__ import annotations


class UserFacingError(ValueError):
    """A failure the user can understand and usually fix themselves."""


class PipelineStageError(RuntimeError):
    """What Step Functions records as the cause of a failed run. Its message is
    always user-safe; the original exception is chained for the logs."""

    def __init__(self, message: str, *, stage: str = "", code: str = "") -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code


_STAGE_MESSAGES = {
    "01_parse": "We couldn't read this file. It may be corrupted, password-protected or in an unsupported format.",
    "02_classify": "The analysis could not be completed. Please re-analyze the document.",
    "03_embed": "The document was analysed but could not be made searchable. Please re-analyze the document.",
    "04_graph": "The document was analysed but could not be linked to related documents. Please re-analyze it.",
    "05_diff": "The document was analysed but its changes could not be compared. Please re-analyze it.",
    "06_timeline": "The document was analysed but its timeline could not be built. Please re-analyze it.",
    "07_persist": "The analysis finished but could not be saved. Please re-analyze the document.",
}
_AI_UNAVAILABLE = "The AI service was temporarily unavailable or too busy. Please re-analyze the document in a few minutes."
_AI_NAMES = {
    "RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError",
    "ModelOutputError", "OutputTruncatedError", "AuthenticationError", "PermissionDeniedError",
    "BadRequestError", "NotFoundError",
}


def safe_message(stage: str, exc: BaseException) -> tuple[str, str]:
    """(message, code) to store for a failed stage — never the raw exception text."""
    name = type(exc).__name__
    if isinstance(exc, UserFacingError):
        return str(exc)[:300], "user_error"
    if name == "DeadlineExceededError":
        return str(exc)[:300], "timeout"
    if name == "OutputTruncatedError":
        return ("This document is too long for the analysis model's output limit. "
                "Contact support to raise the limit."), "output_limit"
    if name in _AI_NAMES:
        return _AI_UNAVAILABLE, "ai_unavailable"
    if name == "TimeoutError" and stage == "01_parse":
        return ("Text recognition (OCR) took too long for this scanned document. "
                "Please try again, or upload a text-based PDF."), "ocr_timeout"
    return _STAGE_MESSAGES.get(stage, "The document could not be processed. Please try again."), "internal"
