"""notifier Lambda — SQS ← EventBridge ``Govern.assigned | reassigned |
sent_back | approved | office_approved | escalated | overdue``.

Per the tenant's workflow settings (``notifications``) it sends

* an email through SES (from ``NOTIFY_FROM_EMAIL``) to the person the event
  lands on — the new owner, the contract owner, or the reviewers of the
  office it was escalated to — never to the person who acted;
* a Microsoft Teams MessageCard to the tenant's incoming-webhook URL (kept in
  Secrets Manager).

Then it appends ONE ``notification_sent`` entry. That action is not in the
notifier's EventBridge rule, so a notification never triggers another.

Best-effort but not lossy: each event is claimed once (a SEEN marker keyed
on the activity entry id, so a re-delivered message sends nothing twice); if
every attempted channel fails the claim is released and the record is
retried, then DLQ'd (alarmed).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
import urllib.error
import urllib.request
from typing import Any

from aws_lambda_powertools import Tracer

from shared import aws
from shared.config import settings
from shared.govern import secrets, sqs_batch, store, workflow
from shared.logger import get_logger

log = get_logger("blue-iq.govern-notifier")
tracer = Tracer(service="blue-iq.govern-notifier")

# Activity action → the settings switch that controls it.
EVENT_SWITCH = {"assigned": "assigned", "reassigned": "assigned", "sent_back": "sent_back",
                "approved": "approved", "office_approved": "approved", "escalated": "escalated",
                "overdue": "overdue"}
_TEAMS_TIMEOUT_S = 10


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return sqs_batch.process(event, handle_event, name="govern_notifier")


def handle_event(evt: dict[str, Any]) -> list[str]:
    """Notify for one Govern event. Returns the channels used."""
    if not settings.feature_enabled("notifications"):
        # Notifications are a "Later" feature: nothing is sent or claimed.
        log.info("govern_notifier.feature_disabled", feature="notifications")
        return []
    entry = evt.get("detail") or {}
    action, tenant_id, contract_id = entry.get("action"), entry.get("tenantId"), entry.get("contractId")
    switch = EVENT_SWITCH.get(action or "")
    if not (switch and tenant_id and contract_id and entry.get("id")):
        return []
    cfg = workflow.get_settings(tenant_id)
    prefs = cfg.get("notifications") or {}
    if not (prefs.get("events") or {}).get(switch, False):
        return []
    contract = store.contracts.get(contract_id)
    if contract is None:
        return []
    scope = f"NOTIFY#{tenant_id}"
    if not store.metrics.claim(scope, str(entry["id"]), days=14):
        return []                                    # already sent for this entry

    recipients = recipients_for(entry, contract, cfg)
    subject, text = message_for(entry, contract)
    link = f"{settings.app_base_url.rstrip('/')}/contracts/{contract_id}"
    attempted, sent = 0, []
    if prefs.get("email") and recipients and settings.notify_from_email:
        attempted += 1
        if _send_email(recipients, subject, f"{text}\n\nOpen it in Govern: {link}"):
            sent.append("email")
    if prefs.get("teams") and cfg.get("teamsWebhookConfigured"):
        url = secrets.tenant_value(settings.teams_secret_arn, tenant_id)
        if isinstance(url, str) and url.startswith("https://"):
            attempted += 1
            if _post_teams(url, subject, text, link):
                sent.append("teams")
    if attempted and not sent:
        store.metrics.release(scope, str(entry["id"]))
        raise RuntimeError("every notification channel failed")
    if sent:
        who = ", ".join(r for r in recipients[:3]) if "email" in sent else ""
        parts = ([f"emailed {who}"] if who else []) + (["posted to Teams"] if "teams" in sent else [])
        workflow.record_system_entry(contract, "notification_sent", f"Sonar {' and '.join(parts)} about this.",
                                     detail={"channels": sent, "recipients": len(recipients) if "email" in sent else 0,
                                             "sourceEventId": entry["id"], "sourceAction": action})
    return sent


def recipients_for(entry: dict[str, Any], contract: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    """Who the event lands on, without the person who caused it."""
    detail = entry.get("detail") or {}
    action = entry.get("action")
    emails: list[str] = []
    if action in ("assigned", "reassigned"):
        emails.append(((detail.get("owner") or {}).get("email") or ""))
    elif action == "escalated":
        office = detail.get("office")
        emails.extend(str(r.get("email") or "") for r in cfg.get("reviewers") or [] if office in (r.get("offices") or []))
        if not emails:
            emails.append(((contract.get("owner") or {}).get("email") or ""))
    else:
        emails.append(((contract.get("owner") or {}).get("email") or ""))
    actor = ((entry.get("actor") or {}).get("email") or "").lower()
    return [e for e in dict.fromkeys(x.strip().lower() for x in emails) if e and "@" in e and e != actor]


def message_for(entry: dict[str, Any], contract: dict[str, Any]) -> tuple[str, str]:
    title = contract.get("title") or "An agreement"
    words = {"assigned": "assigned to you", "reassigned": "assigned to you", "sent_back": "sent back",
             "approved": "approved", "office_approved": "approved by an office", "escalated": "needs your office",
             "overdue": "is overdue"}
    subject = f"Govern: {title} {words.get(entry.get('action') or '', 'changed')}"
    next_step = workflow.next_step(contract, contract.get("analysisStatus") or "READY",
                                   datetime.now(timezone.utc))["headline"]
    return subject[:200], f"{entry.get('summary') or ''}\n\n{title}\nNext step: {next_step}"


def _send_email(recipients: list[str], subject: str, text: str) -> bool:
    try:
        aws.ses_client().send_email(
            FromEmailAddress=settings.notify_from_email,
            Destination={"ToAddresses": recipients[:50]},
            Content={"Simple": {"Subject": {"Data": subject, "Charset": "UTF-8"},
                                "Body": {"Text": {"Data": text, "Charset": "UTF-8"}}}},
        )
        return True
    except Exception as exc:  # noqa: BLE001 — the other channel may still work
        log.warning("govern_notifier.email_failed", error_type=type(exc).__name__)
        return False


def _post_teams(url: str, title: str, text: str, link: str) -> bool:
    card = {"@type": "MessageCard", "@context": "https://schema.org/extensions", "summary": title[:150],
            "themeColor": "0B5CAD", "title": title, "text": text.replace("\n", "<br>"),
            "potentialAction": [{"@type": "OpenUri", "name": "Open in Govern",
                                 "targets": [{"os": "default", "uri": link}]}]}
    try:
        req = urllib.request.Request(url, data=json.dumps(card).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_TEAMS_TIMEOUT_S) as resp:  # noqa: S310 — https only
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.warning("govern_notifier.teams_failed", error_type=type(exc).__name__)
        return False
