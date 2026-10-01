#!/usr/bin/env python3
"""One-off migration to per-project access control.

BEFORE: documents and projects lived in a shared tenant (normally "default").
Projects were one JSON blob per tenant (``TENANT#<t> / PROJECTS``) carrying a
client-supplied ``ownerEmail`` and ``members``; documents recorded no owner.

AFTER (what the API now reads — see lambdas/shared/access.py):
  PROJ#<id> / META              the project, with its owner and docIds
  PROJ#<id> / OWNER             owner pointer        (GSI1: USER#<sub>)
  PROJ#<id> / MEMBER#<email>    one row per member   (GSI1: MEMBER#<email>)
  DOC#<id>  / META              + ownerSub, ownerEmail, projectIds, and listed
                                under its owner (GSI1PK = TENANT#u-<ownerSub>)

What this script does, per tenant given with --tenant:
  1. Reads the legacy projects blob and every document of the tenant.
  2. Assigns each project to its recorded ownerEmail (resolving the Cognito sub
     for that email when the user exists) and writes the new project records.
     Existing members are carried over; the legacy "member" role becomes viewer.
  3. Assigns each document's owner from the project that contains it.
  4. Lists documents that are in NO project. It never guesses an owner for
     them: pass --orphans-to <email> to give them (and any project with no
     recorded owner) to one person.

Safety
  * DRY-RUN IS THE DEFAULT. Nothing is written without --apply.
  * It never deletes anything. The legacy blob is left in place.
  * It is idempotent: a project or document that is already migrated is left
    alone, so the script can be re-run (e.g. after creating a missing user).
  * A document's storage location is NOT changed: its ``tenantId``, its S3 keys
    and its search-index records stay exactly where they are.

Run order (see docs/ARCHITECTURE.md §"Access control"):
  1. python scripts/migrate_access.py --table <T> --user-pool-id <P>         # dry run, any time
  2. deploy the new backend
  3. python scripts/migrate_access.py --table <T> --user-pool-id <P> --apply  # straight after
Between 2 and 3 existing documents are hidden from everyone (not lost).

Requires AWS credentials with dynamodb:Query/GetItem/PutItem/UpdateItem on the
table and cognito-idp:ListUsers on the pool. Nothing here calls OpenAI.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable

ROLES = ("owner", "editor", "viewer")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _email(value: Any) -> str | None:
    email = str(value or "").strip().lower()
    return email if email and "@" in email and " " not in email and len(email) <= 200 else None


def _role(value: Any) -> str:
    role = str(value or "").strip().lower()
    return role if role in ("editor", "viewer") else "viewer"


# ---------------------------------------------------------------------------
# Reading the legacy data
# ---------------------------------------------------------------------------


def _query_all(table: Any, **kwargs: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def read_legacy_projects(table: Any, tenant: str) -> list[dict[str, Any]]:
    item = table.get_item(Key={"PK": f"TENANT#{tenant}", "SK": "PROJECTS"}).get("Item") or {}
    raw = item.get("projects")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [p for p in raw if isinstance(p, dict) and isinstance(p.get("id"), str)] if isinstance(raw, list) else []


def read_tenant_docs(table: Any, tenant: str) -> list[dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    return _query_all(table, IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(f"TENANT#{tenant}"))


def cognito_resolver(pool_id: str, region: str) -> Callable[[str], str | None]:
    """email → Cognito sub (None if no such user). Results are cached."""
    import boto3

    client = boto3.client("cognito-idp", region_name=region)
    cache: dict[str, str | None] = {}

    def resolve(email: str) -> str | None:
        if email not in cache:
            safe = email.replace('"', "")
            users = client.list_users(UserPoolId=pool_id, Filter=f'email = "{safe}"', Limit=2).get("Users", [])
            sub = None
            for user in users:
                attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
                if attrs.get("email", "").lower() == email:
                    sub = attrs.get("sub")
                    break
            cache[email] = sub
        return cache[email]

    return resolve


# ---------------------------------------------------------------------------
# Planning (pure: reads only)
# ---------------------------------------------------------------------------


def build_plan(
    table: Any,
    tenants: list[str],
    resolve_sub: Callable[[str], str | None],
    orphans_to: str | None = None,
) -> dict[str, Any]:
    """Work out every write the migration would make. Reads only."""
    orphans_email = _email(orphans_to)
    orphans_sub = resolve_sub(orphans_email) if orphans_email else None
    plan: dict[str, Any] = {
        "tenants": tenants, "projects": [], "documents": [], "orphanDocuments": [],
        "ownerlessProjects": [], "unresolvedOwners": [], "alreadyMigrated": {"projects": 0, "documents": 0},
        "conflicts": [],
        "errors": [],
    }
    if orphans_to and not orphans_email:
        plan["errors"].append(f"--orphans-to {orphans_to!r} is not a valid email address")
    if orphans_email and not orphans_sub:
        plan["errors"].append(
            f"--orphans-to {orphans_email}: no Cognito user with that email. Create the account "
            "(or have them sign up) first, so the documents can be listed under their user."
        )

    for tenant in tenants:
        docs = read_tenant_docs(table, tenant)
        doc_ids = {d.get("docId") for d in docs if d.get("docId")}
        legacy = sorted(read_legacy_projects(table, tenant), key=lambda p: (str(p.get("createdAt") or ""), p["id"]))
        doc_projects: dict[str, list[dict[str, Any]]] = {}

        for p in legacy:
            pid = p["id"]
            owner_email = _email(p.get("ownerEmail"))
            source = "recorded ownerEmail"
            if not owner_email:
                if orphans_email:
                    owner_email, source = orphans_email, "--orphans-to (no owner was recorded)"
                else:
                    plan["ownerlessProjects"].append({"projectId": pid, "name": p.get("name"), "tenant": tenant})
                    continue
            owner_sub = resolve_sub(owner_email)
            if not owner_sub:
                plan["unresolvedOwners"].append({"projectId": pid, "ownerEmail": owner_email})
            kept_docs = [d for d in dict.fromkeys(p.get("docIds") or []) if d in doc_ids]
            members = []
            for m in p.get("members") or []:
                email = _email(m.get("email")) if isinstance(m, dict) else None
                if not email or email == owner_email:
                    continue
                members.append({
                    "email": email, "role": _role(m.get("role")),
                    "status": "active" if m.get("status") == "active" else "invited",
                    "sub": m.get("sub") if isinstance(m.get("sub"), str) else resolve_sub(email),
                    "invitedAt": str(m.get("invitedAt") or ""),
                })
            current = table.get_item(Key={"PK": f"PROJ#{pid}", "SK": "META"}).get("Item")
            exists = bool(current)
            if exists and not current.get("migratedFrom"):
                # Created through the NEW app (e.g. a browser re-saving its cached
                # list between deploy and migration) before this script ran. It is
                # left untouched and reported: someone must decide who owns it.
                plan["conflicts"].append({"projectId": pid, "recordedOwner": owner_email,
                                          "currentOwner": current.get("ownerEmail") or current.get("ownerSub")})
            elif exists:
                plan["alreadyMigrated"]["projects"] += 1
            entry = {
                "projectId": pid, "name": str(p.get("name") or ""), "client": p.get("client"),
                "createdAt": str(p.get("createdAt") or ""), "tenant": tenant,
                "ownerEmail": owner_email, "ownerSub": owner_sub, "ownerSource": source,
                "docIds": kept_docs,
                "droppedDocIds": [d for d in (p.get("docIds") or []) if d not in doc_ids],
                "members": members, "exists": exists,
            }
            plan["projects"].append(entry)
            for d in kept_docs:
                doc_projects.setdefault(d, []).append(entry)

        for d in docs:
            doc_id = d.get("docId")
            if not doc_id:
                continue
            containing = doc_projects.get(doc_id, [])
            project_ids = [p["projectId"] for p in containing]
            if d.get("ownerSub"):
                # Already has an owner (migrated earlier, or uploaded by the new API).
                if set(project_ids) - set(d.get("projectIds") or []):
                    plan["documents"].append({
                        "docId": doc_id, "title": d.get("title"), "tenant": tenant, "action": "link",
                        "projectIds": sorted(set(project_ids) | set(d.get("projectIds") or [])),
                    })
                else:
                    plan["alreadyMigrated"]["documents"] += 1
                continue
            if containing:
                owner = containing[0]
                plan["documents"].append({
                    "docId": doc_id, "title": d.get("title"), "tenant": tenant, "action": "assign",
                    "ownerEmail": owner["ownerEmail"], "ownerSub": owner["ownerSub"],
                    "projectIds": project_ids, "ownerSource": f"project {owner['projectId']}",
                })
            elif orphans_email and orphans_sub:
                plan["documents"].append({
                    "docId": doc_id, "title": d.get("title"), "tenant": tenant, "action": "assign",
                    "ownerEmail": orphans_email, "ownerSub": orphans_sub, "projectIds": [],
                    "ownerSource": "--orphans-to",
                })
            else:
                plan["orphanDocuments"].append({"docId": doc_id, "title": d.get("title"), "tenant": tenant})
    return plan


# ---------------------------------------------------------------------------
# Applying (writes — only called with --apply)
# ---------------------------------------------------------------------------


def _conditional_failed(exc: Exception) -> bool:
    return getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def apply_plan(table: Any, plan: dict[str, Any]) -> dict[str, int]:
    """Carry out a plan. Every write is conditional or an idempotent put, so
    running it twice changes nothing the second time. Nothing is deleted."""
    done = {"projectsCreated": 0, "ownerPointers": 0, "members": 0, "documentsUpdated": 0}
    now = _now()
    for p in plan["projects"]:
        pid = p["projectId"]
        created = False
        if not p["exists"]:
            try:
                table.put_item(
                    Item={
                        "PK": f"PROJ#{pid}", "SK": "META", "entityType": "PROJECT",
                        "projectId": pid, "name": p["name"], "client": p.get("client"),
                        "createdAt": p["createdAt"] or now, "updatedAt": now, "rev": 1,
                        "ownerEmail": p["ownerEmail"], "ownerSub": p.get("ownerSub"),
                        "tenantId": p["tenant"], "docIds": p["docIds"],
                        "migratedFrom": f"TENANT#{p['tenant']}/PROJECTS", "migratedAt": now,
                    },
                    ConditionExpression="attribute_not_exists(PK)",
                )
                done["projectsCreated"] += 1
                created = True
            except Exception as exc:
                if not _conditional_failed(exc):
                    raise
        if not created:
            # Already there (an earlier run, or created through the app since).
            # Its owner, members and roles are NOT rewritten: doing so would give
            # back access that was removed after go-live.
            continue
        if p.get("ownerSub"):
            table.put_item(Item={
                "PK": f"PROJ#{pid}", "SK": "OWNER", "entityType": "PROJECT_OWNER",
                "GSI1PK": f"USER#{p['ownerSub']}", "GSI1SK": f"PROJ#{pid}",
                "projectId": pid, "role": "owner", "createdAt": now,
            })
            done["ownerPointers"] += 1
        # The owner is also a member row keyed on their email, so ownership holds
        # for an owner whose account does not exist yet (no sub to point at).
        rows = [{"email": p["ownerEmail"], "role": "owner", "status": "active" if p.get("ownerSub") else "invited",
                 "sub": p.get("ownerSub"), "invitedAt": p["createdAt"] or now}] + p["members"]
        for m in rows:
            table.put_item(Item={
                "PK": f"PROJ#{pid}", "SK": f"MEMBER#{m['email']}", "entityType": "PROJECT_MEMBER",
                "GSI1PK": f"MEMBER#{m['email']}", "GSI1SK": f"PROJ#{pid}", "projectId": pid, **m,
            })
            done["members"] += 1

    for d in plan["documents"]:
        names = {"#p": "projectIds", "#u": "updatedAt"}
        values: dict[str, Any] = {":p": d["projectIds"], ":u": now}
        sets = ["#p = :p", "#u = :u"]
        if d["action"] == "assign":
            names.update({"#e": "ownerEmail", "#m": "accessMigratedAt"})
            values.update({":e": d["ownerEmail"], ":m": now})
            sets += ["#e = :e", "#m = :m"]
            if d.get("ownerSub"):
                # List the document under its owner. tenantId / S3 keys / search
                # records are deliberately left where they are.
                names.update({"#s": "ownerSub", "#g": "GSI1PK"})
                values.update({":s": d["ownerSub"], ":g": f"TENANT#u-{d['ownerSub']}"})
                sets += ["#s = :s", "#g = :g"]
        try:
            table.update_item(
                Key={"PK": f"DOC#{d['docId']}", "SK": "META"},
                UpdateExpression="SET " + ", ".join(sets),
                ExpressionAttributeNames=names, ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(PK)",
            )
            done["documentsUpdated"] += 1
        except Exception as exc:
            if not _conditional_failed(exc):      # deleted since the plan was built
                raise
    return done


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def format_report(plan: dict[str, Any], applied: dict[str, int] | None) -> str:
    out: list[str] = []
    mode = "APPLIED" if applied is not None else "DRY RUN — nothing was written (pass --apply to write)"
    out.append(f"Access migration — {mode}")
    out.append(f"Tenants: {', '.join(plan['tenants'])}")
    out.append("")
    new_projects = [p for p in plan["projects"] if not p["exists"]]
    out.append(f"Projects to create: {len(new_projects)}   (already migrated: {plan['alreadyMigrated']['projects']})")
    for p in plan["projects"]:
        state = "exists " if p["exists"] else "create "
        sub = p["ownerSub"] or "NO COGNITO USER YET (owner by email until they sign up)"
        out.append(f"  {state}{p['projectId']}  \"{p['name']}\"  owner={p['ownerEmail']} [{p['ownerSource']}]  "
                   f"sub={sub}  docs={len(p['docIds'])}  members={len(p['members'])}")
        if p["droppedDocIds"]:
            out.append(f"         {len(p['droppedDocIds'])} listed document id(s) no longer exist and are not carried over")
    out.append("")
    assign = [d for d in plan["documents"] if d["action"] == "assign"]
    link = [d for d in plan["documents"] if d["action"] == "link"]
    out.append(f"Documents to give an owner: {len(assign)}   to link to a project only: {len(link)}   "
               f"(already migrated: {plan['alreadyMigrated']['documents']})")
    for d in assign:
        out.append(f"  {d['docId']}  \"{d.get('title') or ''}\"  → {d['ownerEmail']} [{d['ownerSource']}]"
                   f"{'' if d.get('ownerSub') else '  (no Cognito user yet: reachable through the project only)'}")
    if plan["orphanDocuments"]:
        out.append("")
        out.append(f"DOCUMENTS IN NO PROJECT — NOT ASSIGNED ({len(plan['orphanDocuments'])}). They stay hidden "
                   "until given an owner; re-run with --orphans-to <email>:")
        for d in plan["orphanDocuments"]:
            out.append(f"  {d['docId']}  \"{d.get('title') or ''}\"  (tenant {d['tenant']})")
    if plan["ownerlessProjects"]:
        out.append("")
        out.append(f"PROJECTS WITH NO RECORDED OWNER — NOT MIGRATED ({len(plan['ownerlessProjects'])}). "
                   "Re-run with --orphans-to <email>:")
        for p in plan["ownerlessProjects"]:
            out.append(f"  {p['projectId']}  \"{p.get('name') or ''}\"  (tenant {p['tenant']})")
    if plan["unresolvedOwners"]:
        out.append("")
        out.append("Owners with no Cognito account yet (their project is theirs by email as soon as they sign up "
                   "with that verified address):")
        for u in plan["unresolvedOwners"]:
            out.append(f"  {u['ownerEmail']}  (project {u['projectId']})")
    if plan["conflicts"]:
        out.append("")
        out.append("CONFLICTS — a project with this id was created in the new app before the migration ran. "
                   "NOT changed; fix by hand (delete the new one in the app and re-run, or keep it):")
        for c in plan["conflicts"]:
            out.append(f"  {c['projectId']}  legacy owner={c['recordedOwner']}  current owner={c['currentOwner']}")
    for err in plan["errors"]:
        out.append("")
        out.append(f"ERROR: {err}")
    if applied is not None:
        out.append("")
        out.append("Written: " + ", ".join(f"{k}={v}" for k, v in applied.items()))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--table", required=True, help="DynamoDB table name")
    parser.add_argument("--user-pool-id", required=True, help="Cognito user pool id (to resolve emails to subs)")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--tenant", action="append", help="legacy tenant to migrate (repeatable; default: default)")
    parser.add_argument("--orphans-to", help="email that receives documents in no project and ownerless projects")
    parser.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    parser.add_argument("--json", action="store_true", help="also print the plan as JSON")
    args = parser.parse_args(argv)

    import boto3

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    resolve = cognito_resolver(args.user_pool_id, args.region)
    plan = build_plan(table, args.tenant or ["default"], resolve, args.orphans_to)
    applied = None
    if args.apply:
        if plan["errors"]:
            print(format_report(plan, None))
            print("\nNothing was written: fix the errors above first.", file=sys.stderr)
            return 2
        applied = apply_plan(table, plan)
    print(format_report(plan, applied))
    if args.json:
        print(json.dumps(plan, indent=2, default=str))
    return 1 if plan["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
