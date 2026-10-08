"""Govern events: the activity stream → EventBridge + trend aggregates
(lambdas/govern_stream, shared/govern/aggregates.py), GET /reports/trends,
the backfill, and the notifier (lambdas/govern_notifier)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from boto3.dynamodb.types import TypeSerializer

from govern_intake import handler as intake
from govern_notifier import handler as notifier
from govern_stream import handler as stream
from govern_support import DANA, OWNER, analysed_event, call, install_analysis, lambda_context, seed_doc
from shared.config import settings
from shared.govern import aggregates, store, workflow

TENANT = f"u-{OWNER}"
_ser = TypeSerializer().serialize


def stream_event(entries: list[dict]) -> dict:
    """DynamoDB stream INSERT records for activity items (as written)."""
    from shared.dynamodb import _to_ddb

    return {"Records": [{"eventName": "INSERT", "dynamodb": {
        "SequenceNumber": str(100 + i), "NewImage": {k: _ser(v) for k, v in _to_ddb(e).items()}}}
        for i, e in enumerate(entries)]}


@pytest.fixture
def lic(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    seed_doc(ddb, "lic-1", value=200000)
    intake.handle_event(analysed_event("lic-1"))
    return gov


def replay(gov) -> dict:
    """Feed every activity entry written so far through the stream Lambda."""
    items = [i for (_, _), i in sorted(gov.activity.items.items(), key=lambda kv: kv[0][1])]
    return stream.handler(stream_event(items), lambda_context())


def test_every_entry_becomes_a_govern_event_and_counts_once(lic):
    assert replay(lic) == {"batchItemFailures": []}
    assert [e["DetailType"] for e in lic.events] == ["Govern.intake", "Govern.rescored"]
    first = lic.events[0]
    assert first["Source"] == "blue-iq.govern" and first["EventBusName"] == "platform-bus"
    assert json.loads(first["Detail"])["contractId"] == "lic-1"
    day = next(i for (pk, sk), i in lic.metrics.items.items() if sk.startswith("D#"))
    assert day["received"] == 1 and day["rv|USD"] == 200000
    assert day["cd|Royalties"] == 1 and day["cd|GoverningLaw"] == 1
    replay(lic)                                                       # stream retry: no double counting
    day = next(i for (pk, sk), i in lic.metrics.items.items() if sk.startswith("D#"))
    assert day["received"] == 1


def test_a_failed_publish_is_retried_from_that_record(lic):
    lic.fail_events = True
    out = replay(lic)
    assert out == {"batchItemFailures": [{"itemIdentifier": "100"}]}
    lic.fail_events = False
    assert replay(lic) == {"batchItemFailures": []}


def test_trends_roll_days_into_periods(lic):
    for action, body in (("assign", {"owner": DANA}), ("send_back", {"clauses": []}), ("reopen", {}),
                         ("approve", {}), ("office_approve", {"office": "legal_affairs"}),
                         ("send_for_signature", {"provider": "manual"}), ("mark_signed", {})):
        workflow.perform_action("lic-1", action, body, DANA)
    replay(lic)
    status, t = call("GET", "/reports/trends", OWNER, qs={"granularity": "month", "periods": "3"})
    assert status == 200 and t["granularity"] == "month" and len(t["periods"]) == 3
    p = t["periods"][-1]
    assert p["period"] == datetime.now(timezone.utc).strftime("%Y-%m")
    assert (p["received"], p["signed"], p["sentBack"], p["escalated"]) == (1, 1, 1, 0)
    assert p["signedValue"] == {"USD": 200000} and p["receivedValue"] == {"USD": 200000}
    assert p["avgRounds"] == 1 and p["avgCycleDays"] == 0 and p["onTimePct"] == 100
    assert p["avgDaysByStage"]["review"] == 0 and p["avgDaysByStage"]["active"] is None
    assert t["byAgreementType"]["license"] == {"received": 1, "signed": 1, "avgCycleDays": 0}
    top = {d["clauseType"]: d for d in t["clauseDeviations"]}
    assert top["GoverningLaw"]["total"] == 1 and top["GoverningLaw"]["label"]
    assert t["officeLoad"] == [{"office": "legal_affairs", "escalations": 0, "avgDaysToApprove": 0}]
    weekly = call("GET", "/reports/trends", OWNER, qs={"granularity": "week", "periods": "99"})[1]
    assert len(weekly["periods"]) == 26 and weekly["periods"][-1]["period"].count("-W") == 1
    assert call("GET", "/reports/trends", OWNER, qs={"granularity": "day"})[0] == 400


def test_backfill_rebuilds_the_same_totals(lic):
    workflow.perform_action("lic-1", "assign", {"owner": DANA}, DANA)
    replay(lic)
    live = {sk: dict(i) for (pk, sk), i in lic.metrics.items.items() if sk.startswith("D#")}
    entries = list(store.activity.all_for_tenant_contracts(["lic-1"]))
    aggregates.rebuild(TENANT, entries)
    rebuilt = {sk: dict(i) for (pk, sk), i in lic.metrics.items.items() if sk.startswith("D#")}
    assert rebuilt == live


def _govern_event(entry: dict) -> dict:
    return {"Records": [{"messageId": "m1", "body": json.dumps({"detail-type": f"Govern.{entry['action']}",
                                                                "detail": entry}, default=str)}]}


def test_notifier_emails_the_new_owner_and_logs_it_once(lic, monkeypatch):
    monkeypatch.setattr(settings, "notify_from_email", "govern@osu.edu")
    workflow.perform_action("lic-1", "assign", {"owner": {"email": "eli@osu.edu", "name": "Eli Park"}}, DANA)
    entry = {k: v for k, v in lic.activity_for("lic-1")[-1].items() if k not in ("PK", "SK")}
    assert notifier.handler(_govern_event(entry), lambda_context()) == {"batchItemFailures": []}
    assert len(lic.emails) == 1
    mail = lic.emails[0]
    assert mail["Destination"]["ToAddresses"] == ["eli@osu.edu"]
    assert mail["Content"]["Simple"]["Subject"]["Data"] == "Govern: Exclusive License Agreement assigned to you"
    assert lic.actions("lic-1")[-1] == "notification_sent"
    assert lic.activity_for("lic-1")[-1]["summary"] == "Sonar emailed eli@osu.edu about this."
    notifier.handler(_govern_event(entry), lambda_context())          # re-delivery
    assert len(lic.emails) == 1 and lic.actions("lic-1").count("notification_sent") == 1


def test_notifier_respects_settings_and_never_emails_the_actor(lic, monkeypatch):
    monkeypatch.setattr(settings, "notify_from_email", "govern@osu.edu")
    workflow.perform_action("lic-1", "assign", {"owner": DANA}, DANA)          # Dana assigned herself
    entry = {k: v for k, v in lic.activity_for("lic-1")[-1].items() if k not in ("PK", "SK")}
    assert notifier.handle_event({"detail": entry}) == []
    store.config.put_settings(TENANT, {"notifications": {"email": True, "teams": False, "events": {"assigned": False}}})
    entry2 = dict(entry, id="other", actor=None)
    assert notifier.handle_event({"detail": entry2}) == []
    assert lic.emails == []


def test_notifier_posts_to_teams_and_retries_when_every_channel_fails(lic, monkeypatch):
    monkeypatch.setattr(settings, "teams_secret_arn", "arn:teams")
    lic.secrets["arn:teams"] = json.dumps({"tenants": {TENANT: "https://osu.webhook.office.com/x"}})
    store.config.put_settings(TENANT, {"notifications": {"email": False, "teams": True}, "teamsWebhookConfigured": True})
    workflow.perform_action("lic-1", "escalate", {"office": "legal_affairs"}, DANA)
    entry = {k: v for k, v in lic.activity_for("lic-1")[-1].items() if k not in ("PK", "SK")}
    posts = []
    monkeypatch.setattr(notifier, "_post_teams", lambda url, title, text, link: posts.append((url, title)) or False)
    out = notifier.handler(_govern_event(entry), lambda_context())
    assert out == {"batchItemFailures": [{"itemIdentifier": "m1"}]} and len(posts) == 1
    monkeypatch.setattr(notifier, "_post_teams", lambda url, title, text, link: posts.append((url, title)) or True)
    assert notifier.handle_event({"detail": entry}) == ["teams"]               # the retry goes through
    assert lic.activity_for("lic-1")[-1]["summary"] == "Sonar posted to Teams about this."
