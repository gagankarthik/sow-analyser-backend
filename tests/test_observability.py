"""Logs and stored errors: useful to an operator, safe to show a user, and free
of contract text.
"""
from __future__ import annotations

import json

import pytest

import handler as pipeline
from shared import errors
from shared.errors import PipelineStageError, UserFacingError
from shared.openai_client import DeadlineExceededError, ModelOutputError, OutputTruncatedError
from stages import classify

DOC = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECRET = "Zanzibar Quartz Holdings"                 # a party name that must never be logged
SECRET_TEXT = "The supplier shall pay a penalty of USD 9,876,543 to " + SECRET + "."


class _Ctx:
    function_name = "pipeline"
    memory_limit_in_mb = 1024
    invoked_function_arn = "arn:aws:lambda:us-east-2:000000000000:function:pipeline"
    aws_request_id = "abcdef12-0000-0000-0000-000000000000"

    def get_remaining_time_in_millis(self):
        return 600_000


@pytest.fixture
def bucket(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(pipeline, "put_json", lambda b, k, d: store.__setitem__(k, json.loads(json.dumps(d))))
    monkeypatch.setattr(pipeline, "get_json", lambda b, k: json.loads(json.dumps(store[k])))
    monkeypatch.setattr(pipeline, "delete_object", lambda b, k: store.pop(k, None))
    monkeypatch.setattr(pipeline, "get_doc_meta", lambda _id: {"docId": DOC})
    notes: list = []
    monkeypatch.setattr(pipeline, "update_doc_fields", lambda doc_id, fields: notes.append(fields))
    store["_notes"] = notes
    return store


def _event(**extra):
    return {"rawBucket": "raw", "rawKey": f"tenants/acme/uploads/{DOC}/sow.pdf", "processedBucket": "processed",
            "docId": DOC, "tenantId": "acme", **extra}


def _stage(monkeypatch, run):
    monkeypatch.setattr(pipeline.importlib, "import_module", lambda name: type("M", (), {"run": staticmethod(run)}))


# ── Errors a user can read ───────────────────────────────────────────────────


@pytest.mark.parametrize("stage,exc,code,fragment", [
    ("01_parse", UserFacingError("File is too large (80 MB). The limit is 50 MB."), "user_error", "80 MB"),
    ("01_parse", TimeoutError("Textract job 1a2b3c timed out after 240s"), "ocr_timeout", "OCR"),
    ("01_parse", KeyError("pages"), "internal", "couldn't read this file"),
    ("02_classify", ModelOutputError("ContractFacts: the model returned invalid JSON"), "ai_unavailable", "AI service"),
    ("02_classify", OutputTruncatedError("hit the 32000-token limit"), "output_limit", "too long"),
    ("02_classify", DeadlineExceededError("The analysis ran out of time before it could finish."), "timeout", "ran out of time"),
    ("03_embed", ConnectionError("https://search-internal.us-east-2.es.amazonaws.com refused"), "internal", "searchable"),
    ("07_persist", RuntimeError("An error occurred (ProvisionedThroughputExceededException)"), "internal", "could not be saved"),
])
def test_stored_failure_message_is_written_for_the_user(stage, exc, code, fragment):
    message, got_code = errors.safe_message(stage, exc)
    assert got_code == code and fragment in message
    assert len(message) <= 300
    for leak in ("amazonaws.com", "Textract job", "ProvisionedThroughput", "Traceback", "/var/task", "32000"):
        assert leak not in message


def _dump(caplog) -> str:
    """Everything every log record carries: message, extra fields, traceback."""
    out = []
    for record in caplog.records:
        fields = {k: v for k, v in record.__dict__.items() if k not in ("exc_info",)}
        out.append(json.dumps(fields, default=str))
        if record.exc_info:
            import traceback
            out.append("".join(traceback.format_exception(*record.exc_info)))
    return "\n".join(out)


def test_a_failing_stage_raises_a_safe_error_and_records_where(bucket, monkeypatch, caplog):
    def run(event):
        raise ValueError(f"could not parse: {SECRET_TEXT}")
    _stage(monkeypatch, run)
    with pytest.raises(PipelineStageError) as info:
        pipeline.handler(_event(_stage="02_classify"), _Ctx())
    message = str(info.value)
    assert SECRET not in message and "9,876,543" not in message          # no document text
    assert "re-analyze" in message.lower() and "(ref abcdef12)" in message
    assert info.value.stage == "02_classify" and info.value.code == "internal"
    # The original exception is NOT chained (the runtime would print its text);
    # where it came from is in the log line instead, without what it said.
    assert info.value.__cause__ is None and info.value.__suppress_context__ is True
    failed = next(r for r in caplog.records if r.getMessage() == "pipeline.stage_failed")
    assert failed.error_type == "ValueError" and failed.errorCode == "internal"
    assert failed.trace[0].startswith("ValueError @ test_observability.py:") and "in run" in failed.trace[0]
    assert SECRET not in _dump(caplog)
    assert bucket["_notes"] == [{"errorStage": "02_classify", "errorCode": "internal"}]


def test_a_user_error_reaches_the_user_unchanged(bucket, monkeypatch):
    def run(event):
        raise UserFacingError("No readable text was found in this file.")
    _stage(monkeypatch, run)
    with pytest.raises(PipelineStageError, match="No readable text was found in this file."):
        pipeline.handler(_event(_stage="01_parse"), _Ctx())


def test_what_the_api_shows_is_exactly_that_message():
    from api import handler as api
    # Step Functions stores "<Error>: <Cause JSON>" with the stack trace inside.
    cause = json.dumps({"errorMessage": "The AI service was temporarily unavailable or too busy. "
                                        "Please re-analyze the document in a few minutes.",
                        "errorType": "PipelineStageError",
                        "stackTrace": ['  File "/var/task/handler.py", line 151, in handler\n']})
    shown = api._clean({"docId": DOC, "errorMessage": f"PipelineStageError: {cause}",
                        "errorStage": "02_classify", "errorCode": "ai_unavailable"})
    assert shown["errorMessage"].startswith("The AI service was temporarily unavailable")
    assert "/var/task" not in json.dumps(shown)
    assert shown["errorStage"] == "02_classify" and shown["errorCode"] == "ai_unavailable"


def test_parked_document_text_is_removed_when_a_run_fails(bucket, monkeypatch):
    parked = pipeline._park({**_event(), "parsed": {"text": SECRET_TEXT, "pages": []}}, None)
    key = parked["_stateKey"]
    assert key in bucket

    def run(event):
        raise RuntimeError("boom")
    _stage(monkeypatch, run)
    with pytest.raises(PipelineStageError):
        pipeline.handler({**parked, "_stage": "03_embed"}, _Ctx())
    assert key not in bucket


# ── Logs ─────────────────────────────────────────────────────────────────────


def test_stage_log_line_has_ids_duration_and_counts(bucket, monkeypatch, caplog):
    def run(event):
        event["classification"] = {"clauses": [{}, {}, {}], "keyDates": [{}],
                                   "extraction": {"coverageRatio": 0.997, "unclassifiedCount": 1}}
        event["embeddings"] = {"chunkCount": 7, "embeddedCount": 7}
        return event
    _stage(monkeypatch, run)
    parked = pipeline._park({**_event(), "parsed": {"text": "x" * 500, "pages": [{}, {}]}}, None)
    pipeline.handler({**parked, "_stage": "02_classify"}, _Ctx())
    done = next(r for r in caplog.records if r.getMessage() == "pipeline.stage_done")
    assert done.stage == "02_classify" and done.docId == DOC
    assert isinstance(done.durationMs, int)
    for key, value in {"pages": 2, "chars": 500, "clauses": 3, "coverageRatio": 0.997, "unclassified": 1,
                       "keyDates": 1, "chunks": 7, "indexedChunks": 7, "llmCalls": 0, "promptTokens": 0,
                       "completionTokens": 0, "retries": 0}.items():
        assert getattr(done, key) == value


def test_the_whole_classify_stage_logs_no_contract_text(monkeypatch, caplog):
    """Run the real stage on a document full of sensitive wording, with model
    replies that echo it, and with one failing call — then read every log line."""
    from test_classify_pipeline import Model, facts, validation

    model = Model()
    model.facts = facts(title=f"MSA with {SECRET}", parties=[SECRET, "Acme Ltd"],
                        summary=SECRET_TEXT, commercials={"totalContractValue": 9876543.0, "currency": "USD",
                                                          "valueSource": SECRET_TEXT})
    model.validation = validation(totalContractValue=9876543.0,
                                  lineItems=[{"label": "Penalty", "amount": 9876543.0, "source": SECRET_TEXT}])

    def flaky(ids, n_call):
        if n_call == 1:
            raise RuntimeError(f"upstream echoed the prompt: {SECRET_TEXT}")
    model.on_labels = flaky
    monkeypatch.setattr(classify, "chat_json", model)
    monkeypatch.setattr(classify, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(classify, "put_json", lambda *a, **k: None)
    monkeypatch.setattr(classify, "get_doc_meta", lambda _id: None)
    monkeypatch.setattr(classify.settings, "classify_batch_clauses", 2)
    text = f"AGREEMENT between {SECRET} and Acme Ltd.\n1. PENALTY\n{SECRET_TEXT}\n2. TERM\nOne year from 1 March 2026.\n" \
           f"3. NOTICES\nNotices go to legal@{SECRET.split()[0].lower()}.example.\n"
    out = classify.run({"docId": DOC, "tenantId": "acme", "processedBucket": "p",
                        "parsed": {"text": text, "pages": [{"page": 1, "text": text}], "checksum": "c"}})
    assert SECRET in json.dumps(out["classification"])                    # the RESULT has the content...
    everything = _dump(caplog)
    assert "classify.done" in everything and "classify.label_batches_failed" in everything
    for needle in (SECRET, "Zanzibar", "9,876,543", "9876543", "penalty of USD", "legal@"):
        assert needle not in everything, needle                           # ...the LOGS do not


def test_no_stage_logs_exception_text():
    """`error=str(exc)` puts whatever an SDK echoed back (prompt text included)
    into CloudWatch. The pipeline and chat log the exception TYPE instead."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "lambdas"
    offenders = []
    for path in list((root / "pipeline").rglob("*.py")) + list((root / "rag").rglob("*.py")) + [
            root / "shared" / "openai_client.py", root / "shared" / "opensearch.py", root / "shared" / "playbook.py"]:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "error=str(exc)" in line or "error=str(e)" in line:
                offenders.append(f"{path.name}:{n}")
    assert offenders == []
