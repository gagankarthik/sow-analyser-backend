"""govern-seal Lambda — finishes a Govern e-signature envelope once every
signer has signed (shared/govern/esign.py ``seal``; docs/GOVERN_ESIGN.md).

SQS (govern-seal queue) → one message per envelope: ``{contractId, envelopeId}``.
For each: burn the signed values into the pages, append the certificate of
completion, seal the whole file with the KMS key, store it, mark the contract
signed and email every recipient the sealed copy. A message whose envelope is
not waiting to be sealed (already done, voided) is acknowledged and skipped;
a failure is retried by the queue (three tries, then the dead-letter queue),
and after the last try the envelope is marked ``failed``.
"""
from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Tracer

from shared.govern import esign, sqs_batch
from shared.logger import get_logger

log = get_logger("blue-iq.govern-seal")
tracer = Tracer(service="blue-iq.govern-seal")


@tracer.capture_lambda_handler(capture_response=False)
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return sqs_batch.process(event, handle_message, name="govern_seal")


def handle_message(message: dict[str, Any]) -> str:
    contract_id = str(message.get("contractId") or "")
    envelope_id = str(message.get("envelopeId") or "")
    if not contract_id or not envelope_id:
        log.warning("govern_seal.bad_message")
        return "skipped"
    outcome = esign.seal(contract_id, envelope_id)
    log.info("govern_seal.done", envelopeId=envelope_id, outcome=outcome)
    return outcome
