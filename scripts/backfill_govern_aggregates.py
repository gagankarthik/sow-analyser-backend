#!/usr/bin/env python3
"""Rebuild Govern's trend aggregates from the activity log.

The activity-stream Lambda keeps ``govern-metrics`` day items current with
atomic ADDs (shared/govern/aggregates.py). This script recomputes them from
scratch — after a bug fix in the counters, after restoring a table, or to
fill history recorded before the stream existed. It uses the SAME
``aggregates.deltas`` the Lambda uses, so live and rebuilt totals agree.

What it does, per tenant (``--tenant``, repeatable; default: every tenant
registered in govern-config):
  1. lists the tenant's contracts (govern-contracts GSI1);
  2. reads each contract's activity (one Query per contract — no scans);
  3. computes the per-day counters and, with --apply, REPLACES the tenant's
     ``D#<date>`` items in govern-metrics.

Safety
  * DRY-RUN IS THE DEFAULT: it prints the days and totals it would write.
  * Re-running is harmless: the result is a full replacement, not an increment.
  * Stream markers (SEEN#…) are left alone; new entries keep counting live.
    Run it when the stream is quiet, or re-run it afterwards.

Requires AWS credentials with Query on govern-contracts (GSI1) and
govern-activity, GetItem/Query on govern-config, and Query/PutItem/DeleteItem
on govern-metrics. Nothing here calls OpenAI.

    python scripts/backfill_govern_aggregates.py --stage dev               # dry run
    python scripts/backfill_govern_aggregates.py --stage dev --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambdas"))


def plan_tenant(tenant_id: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    """(activity entries, {day: counters}) for one tenant."""
    from shared.govern import aggregates, store

    contract_ids = [c["contractId"] for c in store.contracts.for_tenant(tenant_id)]
    entries = list(store.activity.all_for_tenant_contracts(contract_ids))
    per_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for entry in entries:
        for k, v in aggregates.deltas(entry).items():
            per_day[aggregates.day_of(entry)][k] += v
    return entries, per_day


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stage", default="dev", help="deployment stage (table names are <project>-<stage>-govern-*)")
    parser.add_argument("--project", default="blue-iq-sow")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--tenant", action="append", help="tenant to rebuild (repeatable; default: all)")
    parser.add_argument("--apply", action="store_true", help="write the rebuilt day items (default is a dry run)")
    args = parser.parse_args(argv)

    prefix = f"{args.project}-{args.stage}-govern"
    os.environ.setdefault("AWS_REGION", args.region)
    for name in ("contracts", "activity", "config", "metrics"):
        os.environ.setdefault(f"{name.upper()}_TABLE", f"{prefix}-{name}")
    from shared.govern import aggregates, store       # reads the table names set above

    tenants = args.tenant or store.config.tenants()
    for tenant_id in tenants:
        entries, per_day = plan_tenant(tenant_id)
        received = sum(d.get("received", 0) for d in per_day.values())
        signed = sum(d.get("signed", 0) for d in per_day.values())
        print(f"{tenant_id}: {len(entries)} entries → {len(per_day)} day(s); received {received:g}, signed {signed:g}")
        if args.apply:
            aggregates.rebuild(tenant_id, entries)
    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to replace the day items.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
