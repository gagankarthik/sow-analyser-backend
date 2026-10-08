"""Trend aggregates — maintained at write time, rolled up at read time.

Every activity entry (via the activity-table stream) adds to ONE day item of
its tenant in govern-metrics: ``T#<tenant> / D#<yyyy-mm-dd>``. The day item holds flat
numeric attributes updated with DynamoDB ``ADD`` (atomic, no read first), so
two entries landing at once never lose a count:

    received, signed, rejected, sentBack, escalated, overdueEvents, revisions
    rv|<CUR>  sv|<CUR>                received / signed value per currency
    cyc|sum   cyc|n                   intake → signed days (signed in the day)
    rounds|sum rounds|n               rounds of contracts signed
    sd|<stage>|sum  sd|<stage>|n      days spent in a stage, on stage exit
    exits|n   ontime|n                stage exits with a target / within it
    cd|<clauseType>                   deviates + unacceptable found at intake / rescore
    t|<type>|received  t|<type>|signed  t|<type>|cyc|sum  t|<type>|cyc|n
    o|<office>|esc  o|<office>|apsum  o|<office>|apn

``GET /reports/trends`` reads at most 731 day items with BatchGetItem and
rolls them into weeks or months — never a scan of contracts or activity.

Stream retries re-deliver records, so each entry is counted once: a
conditional marker ``T#<tenant> / SEEN#<eventId>`` (TTL) is claimed before
the ADD. ``scripts/backfill_govern_aggregates.py`` rebuilds the day items
from the activity table with ``deltas()`` — the same function.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from ..logger import get_logger
from . import store

log = get_logger("blue-iq.govern.aggregates")

_COUNTERS = {"intake": "received", "signed": "signed", "rejected": "rejected", "sent_back": "sentBack",
             "escalated": "escalated", "revision_received": "revisions"}
STAGES = ("draft", "review", "negotiation", "approval", "signed", "active", "renewal", "expired")
_MAX_WEEKS, _MAX_MONTHS = 26, 24


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def deltas(entry: dict[str, Any]) -> dict[str, float]:
    """The counter increments one activity entry contributes."""
    action = entry.get("action")
    d = entry.get("detail") or {}
    out: dict[str, float] = defaultdict(float)
    if action in _COUNTERS:
        out[_COUNTERS[action]] += 1
    if action == "overdue" and not d.get("obligationId"):
        out["overdueEvents"] += 1
    at = d.get("agreementType")
    if action == "intake":
        value, cur = _num(d.get("value")), (d.get("currency") or "USD")
        if value is not None:
            out[f"rv|{cur}"] += value
        if at:
            out[f"t|{at}|received"] += 1
    if action == "signed":
        value, cur = _num(d.get("value")), (d.get("currency") or "USD")
        if value is not None:
            out[f"sv|{cur}"] += value
        cyc = _num(d.get("cycleDays"))
        if cyc is not None:
            out["cyc|sum"] += cyc
            out["cyc|n"] += 1
        rounds = _num(d.get("rounds"))
        if rounds is not None:
            out["rounds|sum"] += rounds
            out["rounds|n"] += 1
        if at:
            out[f"t|{at}|signed"] += 1
            if cyc is not None:
                out[f"t|{at}|cyc|sum"] += cyc
                out[f"t|{at}|cyc|n"] += 1
    if action == "escalated" and d.get("office"):
        out[f"o|{d['office']}|esc"] += 1
    if action == "office_approved" and d.get("office"):
        days = _num(d.get("daysToApprove"))
        if days is not None:
            out[f"o|{d['office']}|apsum"] += days
            out[f"o|{d['office']}|apn"] += 1
    if action == "rescored":
        for ct in d.get("deviatingClauseTypes") or []:
            if isinstance(ct, str) and ct:
                out[f"cd|{ct}"] += 1
    exit_ = d.get("stageExit") or {}
    if exit_.get("stage") in STAGES:
        days = _num(exit_.get("days"))
        if days is not None:
            out[f"sd|{exit_['stage']}|sum"] += days
            out[f"sd|{exit_['stage']}|n"] += 1
        if exit_.get("onTime") is not None:
            out["exits|n"] += 1
            if exit_.get("onTime"):
                out["ontime|n"] += 1
    return {k: (round(v, 3) if v != int(v) else int(v)) for k, v in out.items() if v}


def day_of(entry: dict[str, Any]) -> str:
    return str(entry.get("at") or store.iso())[:10]


def record(entry: dict[str, Any]) -> bool:
    """Count one activity entry exactly once. True when it was counted."""
    tenant = entry.get("tenantId")
    event_id = entry.get("id")
    if not tenant or not event_id:
        return False
    increments = deltas(entry)
    if not increments:
        return False
    scope = f"T#{tenant}"
    if not store.metrics.claim(scope, str(event_id), days=3):
        return False
    try:
        store.metrics.add(tenant, day_of(entry), increments)
    except Exception:
        store.metrics.release(scope, str(event_id))   # let the stream's retry count it
        raise
    return True


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def _iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def periods_for(granularity: str, count: int, today: date) -> list[dict[str, Any]]:
    """Calendar periods, oldest first, ending with the one containing today."""
    out: list[dict[str, Any]] = []
    if granularity == "week":
        start = today - timedelta(days=today.weekday())
        for i in range(count - 1, -1, -1):
            s = start - timedelta(weeks=i)
            out.append({"period": _iso_week(s), "start": s, "end": s + timedelta(days=6)})
    else:
        y, m = today.year, today.month
        months = []
        for _ in range(count):
            months.append((y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        for y, m in reversed(months):
            s = date(y, m, 1)
            nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
            out.append({"period": f"{y}-{m:02d}", "start": s, "end": nxt - timedelta(days=1)})
    return out


def load_days(tenant_id: str, start: date, end: date) -> dict[str, dict[str, Any]]:
    days, d = [], start
    while d <= end:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return store.metrics.days(tenant_id, days)


def _avg(s: float, n: float) -> float | None:
    return round(s / n, 1) if n else None


def trends(tenant_id: str, granularity: str = "month", periods: int = 12,
           now: datetime | None = None, label: Any = None) -> dict[str, Any]:
    """The ``Trends`` object of GOVERN_API.md."""
    granularity = "week" if granularity == "week" else "month"
    periods = max(1, min(int(periods or 12), _MAX_WEEKS if granularity == "week" else _MAX_MONTHS))
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    spans = periods_for(granularity, periods, now.date())
    days = load_days(tenant_id, spans[0]["start"], min(spans[-1]["end"], now.date()))

    out_periods = []
    by_type: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    clause_total: dict[str, float] = defaultdict(float)
    clause_by_period: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    office: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for span in spans:
        tot: dict[str, float] = defaultdict(float)
        d = span["start"]
        while d <= span["end"]:
            for k, v in (days.get(d.isoformat()) or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    tot[k] += v
            d += timedelta(days=1)
        for k, v in tot.items():
            parts = k.split("|")
            if parts[0] == "t" and len(parts) >= 3:
                by_type[parts[1]]["|".join(parts[2:])] += v
            elif parts[0] == "cd" and len(parts) == 2:
                clause_total[parts[1]] += v
                clause_by_period[parts[1]][span["period"]] += v
            elif parts[0] == "o" and len(parts) == 3:
                office[parts[1]][parts[2]] += v
        out_periods.append({
            "period": span["period"], "start": span["start"].isoformat(), "end": span["end"].isoformat(),
            **{k: int(tot.get(k, 0)) for k in ("received", "signed", "rejected", "sentBack", "escalated",
                                               "overdueEvents", "revisions")},
            "signedValue": {k[3:]: round(v, 2) for k, v in tot.items() if k.startswith("sv|")},
            "receivedValue": {k[3:]: round(v, 2) for k, v in tot.items() if k.startswith("rv|")},
            "avgCycleDays": _avg(tot.get("cyc|sum", 0), tot.get("cyc|n", 0)),
            "avgDaysByStage": {s: _avg(tot.get(f"sd|{s}|sum", 0), tot.get(f"sd|{s}|n", 0)) for s in STAGES},
            "avgRounds": _avg(tot.get("rounds|sum", 0), tot.get("rounds|n", 0)),
            "onTimePct": (round(100.0 * tot.get("ontime|n", 0) / tot["exits|n"], 1) if tot.get("exits|n") else None),
        })
    labeller = label or (lambda ct: ct)
    top = sorted(clause_total.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
    return {
        "granularity": granularity,
        "generatedAt": store.iso(now),
        "periods": out_periods,
        "byAgreementType": {t: {"received": int(v.get("received", 0)), "signed": int(v.get("signed", 0)),
                                "avgCycleDays": _avg(v.get("cyc|sum", 0), v.get("cyc|n", 0))}
                            for t, v in sorted(by_type.items())},
        "clauseDeviations": [{"clauseType": ct, "label": labeller(ct), "total": int(n),
                              "byPeriod": {p["period"]: int(clause_by_period[ct].get(p["period"], 0)) for p in out_periods}}
                             for ct, n in top],
        "officeLoad": [{"office": o, "escalations": int(v.get("esc", 0)),
                        "avgDaysToApprove": _avg(v.get("apsum", 0), v.get("apn", 0))}
                       for o, v in sorted(office.items())],
    }


def rebuild(tenant_id: str, entries: Iterable[dict[str, Any]]) -> int:
    """Replace a tenant's day items with totals recomputed from ``entries``
    (backfill). Returns the number of day items written."""
    per_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in entries:
        for k, v in deltas(e).items():
            per_day[day_of(e)][k] += v
    store.metrics.replace_days(tenant_id, {day: {k: (round(v, 3) if v != int(v) else int(v)) for k, v in inc.items()}
                                           for day, inc in per_day.items()})
    return len(per_day)
