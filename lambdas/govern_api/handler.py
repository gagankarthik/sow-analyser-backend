"""Govern API Lambda — every route of docs/GOVERN_API.md.

Routes                                                      needs
------                                                      -----
GET    /govern/me                                           signed in (creates the organization on first sign-in)
GET    /govern/org                                          signed in (members + admin trail for admins)
POST   /govern/org/admins · DELETE /govern/org/admins/{sub} admin of this organization
GET    /contracts[?includeClosed=true]                      (lists what the caller may see)
GET    /obligations                                         (open, dated obligations of visible contracts)
POST   /contracts                                           edit on the document
POST   /contracts/bulk                                      edit, per contract (results per id)
GET    /contracts/{id}                                      view
PATCH  /contracts/{id}                                      edit
POST   /contracts/{id}/actions                              Contract.allowedActions (see workflow.allowed_actions)
POST   /contracts/{id}/rescore                              edit
POST   /contracts/{id}/blockers                             edit
PATCH  /contracts/{id}/blockers/{blockerId}                 edit
POST   /contracts/{id}/obligations                          edit
PATCH  /contracts/{id}/obligations/{oblId}                  edit
PUT    /contracts/{id}/income                               edit
GET    /contracts/{id}/revision-upload-url?filename=        edit
GET    /contracts/{id}/activity?before=&limit=              view (older activity, newest first)
GET    /contracts/{id}/portal-links                         edit (counterparty links; never the token)
POST   /contracts/{id}/portal-links                         edit (max 5 active; the url is returned once)
DELETE /contracts/{id}/portal-links/{linkId}                edit (revoke)
GET    /matrix · GET /matrix/versions/{n}                   signed in
PUT    /matrix · POST /matrix/import                        Govern admin
GET    /workflow/settings                                   signed in
PUT    /workflow/settings                                   Govern admin
GET    /integrations · /integrations/sync-log · /integrations/unmatched
PUT    /integrations/{id} · POST /integrations/{id}/sync    Govern admin
GET    /reports/trends                                      Govern admin or leader
GET    /reports/capture                                     signed in (what the caller may see)
GET    /govern/people                                       signed in (people the caller can see)
GET    /views?module= · POST /views                         signed in (saved list views)
PATCH  /views/{id} · DELETE /views/{id}                     the view's creator
GET    /automations                                         signed in (the organization's rules)
POST   /automations · PATCH/DELETE /automations/{id}        Govern admin
POST   /automations/{id}/test                               signed in + view on the contract (dry run, no writes)

ACCESS. A contract is visible exactly when its document is: the caller's role
on the contract is their strongest role (shared/access.py) on its first
document (``contractId``) or its current one (``currentDocId``). No role →
404, so ids cannot be probed; a role without the capability → 403.

GOVERN ROLES. "Govern admin" (matrix, routing, settings, integrations,
automations, the organization's admins) is per tenant: the organization's
creator and the admins they add (shared/govern/org.py). A personal ``u-<sub>``
workspace's user is its admin. Cognito groups are pool-wide, so they never
grant admin: ``govern-reviewer`` (and the legacy ``govern-admin``) mean
reviewer, ``govern-leader`` means leader (view, comment, approve when routed).
Which actions a caller may take is ``Contract.allowedActions``
(workflow.allowed_actions): editors get every action valid in the state;
viewers and leaders may comment, and office-approve when they approve for an
office the contract waits on.

Every state change goes through shared/govern/workflow.py; this module only
authorises, validates the request shape and renders responses.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from aws_lambda_powertools import Tracer

from shared import dynamodb as ddb
from shared.access import Caller, can
from shared.auth import AuthError, jwt_claims
from shared.config import settings
from shared.govern import aggregates, automations, capture, connectors, portal, revisions, secrets, store, views, workflow
from shared.govern import matrix as govern_matrix
from shared.govern import org
from shared.govern.store import ContractConflict, iso
from shared.logger import get_logger, safe_trace
from shared.uploads import clean_upload_filename

log = get_logger("blue-iq.govern-api")
tracer = Tracer(service="blue-iq.govern-api")

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NOT_FOUND = "Contract not found"
_FORBIDDEN = "You do not have permission to do that on this contract"
_ADMIN_ONLY = "Only a Govern admin can do that"
_LEADER_READ_ONLY = "Leaders can view, comment and approve; ask a reviewer to change this contract"
# Documents turned into contracts by one GET /contracts (the rest follow on
# the next call; intake and the hourly reconciliation also create them).
_MAX_LAZY_CREATES = 100
_MAX_BODY_BYTES = 2_000_000
_MAX_BULK = 200            # contracts per POST /contracts/bulk
_MAX_PEOPLE = 500          # people per GET /govern/people
_PEOPLE_SOURCES = ("project", "reviewer", "owner")
# Cognito groups are pool-wide, so none of them grants admin (that is per
# tenant: shared/govern/org.py). ``govern-admin`` is kept as reviewer-level so
# a user in it is never narrowed to a leader.
_ROLE_GROUPS = (("govern-admin", "reviewer"), ("govern-reviewer", "reviewer"), ("govern-leader", "leader"))
_SUB_RE = re.compile(r"[A-Za-z0-9-]{1,60}")
_TEAMS_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+\.(?:webhook\.office\.com|logic\.azure\.com|office\.com)(?::\d+)?/\S+$")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


_UNREAD = object()


class GovernUser:
    """The verified caller plus their display name and Govern role.

    Admin is per tenant (shared/govern/org.py): the caller is an admin exactly
    when they created this tenant's organization or one of its admins added
    them. Cognito groups only say reviewer or leader."""

    def __init__(self, caller: Caller, claims: dict[str, Any]) -> None:
        self.caller = caller
        self.claims = claims
        self.groups = _groups(claims.get("cognito:groups"))
        self.group_role = next((role for group, role in _ROLE_GROUPS if group in self.groups), None)
        self.display_name = (claims.get("name") or " ".join(
            p for p in (claims.get("given_name"), claims.get("family_name")) if p) or None)
        self.person = workflow.person(caller.email or claims.get("email") or caller.sub, self.display_name)
        self._org: Any = _UNREAD

    @property
    def org(self) -> dict[str, Any] | None:
        """This tenant's ORG record (read once; None if it has none yet)."""
        if self._org is _UNREAD:
            self._org = org.get(self.caller.tenant_id)
        return self._org

    def ensure_org(self) -> dict[str, Any] | None:
        """Create the ORG record on first sign-in (this caller becomes creator
        when there is none) and remember the caller as a member."""
        p = self.person or {}
        self._org = org.ensure(self.caller.tenant_id, self.caller.sub, self.caller.email, p.get("name"))
        org.record_member(self.caller.tenant_id, self.caller.sub, self.caller.email, p.get("name"))
        return self._org

    @property
    def actor(self) -> dict[str, Any]:
        return {"sub": self.caller.sub, "email": self.caller.email, "name": (self.person or {}).get("name")}

    @property
    def is_admin(self) -> bool:
        if org.is_personal(self.caller.tenant_id):       # no read needed
            return org.is_admin(None, self.caller.tenant_id, self.caller.sub)
        return org.is_admin(self.org, self.caller.tenant_id, self.caller.sub)

    @property
    def is_creator(self) -> bool:
        if org.is_personal(self.caller.tenant_id):
            return org.is_creator(None, self.caller.tenant_id, self.caller.sub)
        return org.is_creator(self.org, self.caller.tenant_id, self.caller.sub)

    @property
    def role(self) -> str:
        return "admin" if self.is_admin else (self.group_role or "reviewer")

    @property
    def is_leader(self) -> bool:
        return self.group_role == "leader"


def _groups(raw: Any) -> set[str]:
    """``cognito:groups`` arrives as a list, or (HTTP API JWT authorizer) as a
    string like ``"[govern-admin reviewers]"`` or ``"a,b"``."""
    if isinstance(raw, list):
        return {str(g).strip() for g in raw if str(g).strip()}
    if isinstance(raw, str):
        return {g for g in re.split(r"[\s,\[\]\"']+", raw) if g}
    return set()


# ---------------------------------------------------------------------------
# Entry point and router
# ---------------------------------------------------------------------------


# capture_response=False: responses carry contract details, people and
# presigned upload URLs; Powertools would otherwise copy each one into X-Ray
# trace metadata, readable outside the tenant model.
@tracer.capture_lambda_handler(capture_response=False)
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return dispatch(event)


def dispatch(event: dict[str, Any]) -> dict[str, Any]:
    """Authenticate, route, and map errors to ``{"error", "code"}`` bodies."""
    method =(event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod") or "GET").upper()
    path = event.get("rawPath") or event.get("path") or "/"
    if method == "OPTIONS":
        return _ok({})
    try:
        user = GovernUser(Caller.from_event(event), jwt_claims(event))
    except AuthError:
        return _err(403, "Forbidden", "unauthenticated")
    log.append_keys(tenantId=user.caller.tenant_id, method=method)
    try:
        return route(method, path, event, user)
    except (workflow.GovernError, org.OrgError) as exc:
        return _err(exc.status, str(exc), exc.code)
    except ContractConflict:
        return _err(409, "Someone else changed this at the same time. Please try again.", "conflict")
    except Exception as exc:  # noqa: BLE001
        log.error("govern_api.unhandled_error", error_type=type(exc).__name__, trace=safe_trace(exc))
        return _err(500, "Internal server error", "internal")


def route(method: str, path: str, event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    path = path.rstrip("/") or "/"
    seg = path.strip("/").split("/")
    if len(seg) > 1 and seg[0] == "contracts" and not _ID_RE.fullmatch(seg[1]):
        return _err(404, _NOT_FOUND, "not_found")
    for (want_method, pattern), fn in _ROUTES:
        if method != want_method:
            continue
        m = pattern.fullmatch(path)
        if m:
            return fn(event, user, *m.groups())
    if any(p.fullmatch(path) for (_, p), _ in _ROUTES):
        return _err(405, "Method not allowed", "method_not_allowed")
    return _err(404, "Not found", "not_found")


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def _capped_revision_role(role: str | None) -> str | None:
    """A role held on a REVISION document counts at most as editor on the contract.

    Whoever uploads a revision (an editor, or the link creator when the
    counterparty uploads through a portal link) owns that revision document.
    Without this cap that ownership became "owner" of the whole contract once
    the revision was made current — an editor could then delete the contract
    (``delete_document``) that someone else owns. Contract ownership comes only
    from the contract's first document."""
    return "editor" if role == "owner" else role


def _contract_role(c: dict[str, Any], user: GovernUser, metas: dict[str, dict[str, Any]] | None = None) -> str | None:
    """Strongest document role on the contract's first document, or (capped at
    editor) on its current one."""
    from shared.access import stronger

    first = c["contractId"]
    current = c.get("currentDocId") or first
    ids = list(dict.fromkeys([first, current]))
    if metas is None:
        metas = {m["docId"]: m for m in ddb.get_docs(ids) if m.get("docId")}
    role = user.caller.document_role(metas.get(first))
    if current != first:
        role = stronger(role, _capped_revision_role(user.caller.document_role(metas.get(current))))
    return role


def _contract_for(contract_id: str, user: GovernUser, capability: str) -> tuple[dict[str, Any], str]:
    """(contract, role) if the caller may do ``capability`` on it. Raises
    NotFound (cannot see it) or a 403 (can see, may not act)."""
    c = store.contracts.get(contract_id)
    role = _contract_role(c, user) if c else None
    if c is None or role is None:
        raise workflow.NotFound(_NOT_FOUND)
    if not can(role, capability):
        raise _Forbidden(_FORBIDDEN)
    # Leaders view, comment and approve for their office; changing a
    # contract's details, open items, money or obligations is reviewers' work.
    if capability == "edit" and user.is_leader:
        raise _Forbidden(_LEADER_READ_ONLY)
    return c, role


def _viewer(user: GovernUser, role: str | None) -> workflow.Viewer:
    return workflow.Viewer(can_edit=can(role, "edit"), is_leader=user.is_leader,
                           email=(user.person or {}).get("email"))


class _Forbidden(workflow.GovernError):
    status = 403
    code = "forbidden"


class _Conflict(workflow.GovernError):
    status = 409
    code = "conflict"


def _require_admin(user: GovernUser) -> None:
    if not user.is_admin:
        raise _Forbidden(_ADMIN_ONLY)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class _Settings:
    """Workflow settings per tenant, read once per request."""

    def __init__(self) -> None:
        self._by_tenant: dict[str, dict[str, Any]] = {}

    def __call__(self, tenant_id: str) -> dict[str, Any]:
        if tenant_id not in self._by_tenant:
            self._by_tenant[tenant_id] = workflow.get_settings(tenant_id)
        return self._by_tenant[tenant_id]


def _analysis_meta(c: dict[str, Any], metas: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The document whose status is the contract's analysis status: a revision
    still being read, else the current document."""
    pending = metas.get(c.get("pendingDocId") or "")
    if pending and str(pending.get("status") or "").upper() != "READY":
        return pending
    return metas.get(c.get("currentDocId") or c["contractId"])


def _render_list(contracts: list[dict[str, Any]], docs: dict[str, dict[str, Any]], now: datetime,
                 user: GovernUser) -> list[dict[str, Any]]:
    """Contracts as JSON. ``docs`` are the caller's visible documents (with
    ``_role``), so roles and analysis status need no extra reads except for a
    current / pending document the caller cannot see directly."""
    from shared.access import stronger

    cfg = _Settings()
    metas = dict(docs)
    missing = {d for c in contracts for d in (c.get("currentDocId"), c.get("pendingDocId")) if d and d not in metas}
    if missing:
        metas.update({m["docId"]: m for m in ddb.get_docs(list(missing)) if m.get("docId")})
    out = []
    for c in contracts:
        current = c.get("currentDocId") or ""
        role = (docs.get(c["contractId"]) or {}).get("_role")
        if current and current != c["contractId"]:
            # Same rule as _contract_role: a revision's owner is an editor here.
            role = stronger(role, _capped_revision_role((docs.get(current) or {}).get("_role")))
        out.append(workflow.to_api(c, now, cfg(c["tenantId"]), _analysis_meta(c, metas), _viewer(user, role)))
    return out


def _detail(contract_id: str, viewer: workflow.Viewer, now: datetime | None = None) -> dict[str, Any]:
    c = store.contracts.get(contract_id)
    if c is None:
        raise workflow.NotFound(_NOT_FOUND)
    now = now or datetime.now(timezone.utc)
    doc_ids = list(dict.fromkeys((c.get("versionDocIds") or [c["contractId"]])
                                 + [c.get("currentDocId"), c.get("pendingDocId")]))
    metas = {m["docId"]: m for m in ddb.get_docs([d for d in doc_ids if d]) if m.get("docId")}
    review = store.contracts.get_review(contract_id, c.get("reviewedDocId") or c.get("currentDocId") or contract_id)
    head = store.activity.head(contract_id, show=_DETAIL_ACTIVITY, count_up_to=_ACTIVITY_COUNT_CAP)
    out = workflow.detail(
        c, now=now, cfg=workflow.get_settings(c["tenantId"]), doc_meta=_analysis_meta(c, metas), viewer=viewer,
        version_metas=metas, blockers=store.contracts.blockers(contract_id),
        obligations=store.contracts.obligations(contract_id), income=store.contracts.income(contract_id),
        review=review, activity=_detail_activity(head))
    out.update({"activityCount": head["count"], "activityHasMore": head["hasMore"], "activityCursor": head["cursor"]})
    return out


# The contract page shows the newest entries; older ones page in through
# GET /contracts/{id}/activity. The count is exact up to the cap.
_DETAIL_ACTIVITY = 20
_ACTIVITY_COUNT_CAP = 200
_ACTIVITY_PAGE_MAX = 100
# Entries the page reads for more than the timeline: the latest send-back
# (its clauses and note build the redline). Kept when older than the newest 20.
_KEEP_LATEST = ("sent_back",)


def _detail_activity(head: dict[str, Any]) -> list[dict[str, Any]]:
    """The newest entries, plus the latest of each _KEEP_LATEST action read in
    the same query. Those are older than every shown entry, so the list stays
    newest first; the timeline drops the duplicate when it pages them in."""
    entries = list(head["entries"])
    have = {e.get("action") for e in entries}
    for action in _KEEP_LATEST:
        if action in have:
            continue
        older = next((e for e in head["window"] if e.get("action") == action), None)
        if older is not None:
            entries.append(older)
    return entries


def _contract_activity(event: dict[str, Any], user: GovernUser, contract_id: str) -> dict[str, Any]:
    """One page of the activity log, newest first: ``{entries, nextCursor}``.
    ``before`` is the previous page's ``nextCursor`` (or the detail's
    ``activityCursor``); ``limit`` is 1-100, default 50."""
    _contract_for(contract_id, user, "view")
    qs = event.get("queryStringParameters") or {}
    raw_limit = qs.get("limit")
    try:
        limit = int(raw_limit) if raw_limit not in (None, "") else 50
    except (TypeError, ValueError):
        raise workflow.BadRequest("limit must be a whole number from 1 to 100")
    if not 1 <= limit <= _ACTIVITY_PAGE_MAX:
        raise workflow.BadRequest("limit must be a whole number from 1 to 100")
    before = qs.get("before") or None
    if before is not None and (len(before) > 256 or not before.isprintable()):
        raise workflow.BadRequest("before is not a valid cursor")
    entries, cursor = store.activity.page(contract_id, before=before, limit=limit)
    return _ok({"entries": [workflow.activity_view(e) for e in entries], "nextCursor": cursor})


def _contract_response(contract_id: str, user: GovernUser, role: str | None, status: int = 200) -> dict[str, Any]:
    return _ok({"contract": _detail(contract_id, _viewer(user, role))}, status)


# ---------------------------------------------------------------------------
# Contracts — list and intake
# ---------------------------------------------------------------------------


def _visible_contracts(user: GovernUser, *, create_missing: bool) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """(contracts the caller may see, visible document metas).

    One GSI1 query for the caller's tenant + one pass over the documents the
    caller may see (shared/access.py). Visible documents not covered by those
    contracts are looked up by key (a contract in another workspace shared
    through a project), and — with ``create_missing`` — READY ones that are
    still not a contract become one, so the library's existing documents
    appear on the board.
    """
    docs = user.caller.visible_documents()
    tenant_contracts = store.contracts.for_tenant(user.caller.tenant_id)
    visible = {c["contractId"]: c for c in tenant_contracts
               if c["contractId"] in docs or (c.get("currentDocId") or "") in docs}
    covered = capture.covered_doc_ids(tenant_contracts)
    uncovered = [d for d in docs if d not in covered]
    if uncovered:
        for cid, c in store.contracts.get_many(uncovered).items():
            visible[cid] = c
        covered |= capture.covered_doc_ids(visible.values())
    if create_missing:
        candidates = [docs[d] for d in uncovered if d not in covered
                      and str(docs[d].get("status") or "").upper() == "READY" and not docs[d].get("revisionOf")]
        cfg = _Settings()
        for meta in candidates[:_MAX_LAZY_CREATES]:
            c, _ = workflow.create_from_document(_meta_for_contract(meta), None, source="library",
                                                 cfg=cfg(meta.get("tenantId") or user.caller.tenant_id))
            visible[c["contractId"]] = c
        if candidates or not tenant_contracts:
            store.config.register_tenant(user.caller.tenant_id)
    return list(visible.values()), docs


def _meta_for_contract(meta: dict[str, Any]) -> dict[str, Any]:
    """A documents-table row as plain values, without the caller's role."""
    return {k: v for k, v in (store.clean_item(meta) or {}).items() if k != "_role"}


def _list_contracts(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    qs = event.get("queryStringParameters") or {}
    include_closed = str(qs.get("includeClosed") or "").lower() == "true"
    contracts, docs = _visible_contracts(user, create_missing=True)
    if not include_closed:
        contracts = [c for c in contracts if c.get("state") not in workflow.TERMINAL_STATES]
    now = datetime.now(timezone.utc)
    out = _render_list(contracts, docs, now, user)
    out.sort(key=lambda c: (c["stageEnteredAt"] or "", c["contractId"]))
    return _ok({"contracts": out, "count": len(out), "generatedAt": iso(now)})


def _list_obligations(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    """Every open, dated obligation across the contracts this caller can see,
    soonest due first, each with the contract it belongs to."""
    contracts, _docs = _visible_contracts(user, create_missing=False)
    by_id = {c["contractId"]: c for c in contracts}
    # A contract shared through a project can live in another workspace: read
    # the obligations of every workspace the visible contracts belong to.
    tenants = sorted({str(c.get("tenantId") or user.caller.tenant_id) for c in contracts} or {user.caller.tenant_id})
    rows = [o for t in tenants for o in store.contracts.open_obligations(t)]
    rows.sort(key=lambda o: (str(o.get("dueDate") or ""), str(o.get("id") or "")))
    out = []
    for o in rows:
        c = by_id.get(o.get("contractId"))
        if c is None:
            continue
        owner = c.get("owner") or {}
        out.append({**workflow.obligation_view(o), "contractId": c["contractId"],
                    "contractTitle": c.get("title"), "counterparty": c.get("sponsor") or c.get("counterparty"),
                    "agreementType": c.get("agreementType"), "stage": c.get("stage"),
                    "currency": c.get("currency"),
                    "owner": {"email": owner.get("email"), "name": owner.get("name")} if owner else None})
    return _ok({"obligations": out, "count": len(out),
                "enabled": settings.feature_enabled("obligations"),
                "generatedAt": iso(datetime.now(timezone.utc))})


def _create_contract(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    """Intake right after upload — idempotent: an existing contract is patched."""
    body = _body(event)
    doc_id = body.pop("docId", None)
    if not isinstance(doc_id, str) or not _ID_RE.fullmatch(doc_id):
        raise workflow.BadRequest("docId is required")
    fields = workflow.clean_fields(body)
    meta = ddb.get_doc_meta(doc_id)
    role = user.caller.document_role(meta)
    if role is None:
        raise workflow.NotFound("Document not found")
    if not can(role, "edit"):
        raise _Forbidden(_FORBIDDEN)
    if meta.get("revisionOf"):
        raise _Conflict("This document is a revised version of another contract.")
    existing = store.contracts.get(doc_id)
    if existing is None:
        c, created = workflow.create_from_document(_meta_for_contract(meta), user.person, fields=fields,
                                                   source="upload")
        if created:
            store.config.register_tenant(c["tenantId"])
            return _contract_response(doc_id, user, role, 201)
    if fields:
        workflow.update_fields(doc_id, fields, user.person)
    return _contract_response(doc_id, user, role)


# ---------------------------------------------------------------------------
# Contracts — one contract
# ---------------------------------------------------------------------------


def _get_contract(event: dict[str, Any], user: GovernUser, contract_id: str) -> dict[str, Any]:
    _, role = _contract_for(contract_id, user, "view")
    return _contract_response(contract_id, user, role)


def _patch_contract(event: dict[str, Any], user: GovernUser, contract_id: str) -> dict[str, Any]:
    _, role = _contract_for(contract_id, user, "edit")
    fields = workflow.clean_fields(_body(event))
    if not fields:
        raise workflow.BadRequest("No fields to update")
    workflow.update_fields(contract_id, fields, user.person)
    return _contract_response(contract_id, user, role)


def _bulk_update(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    """The same change on many contracts: PATCH fields and / or an owner (the
    ``assign`` action). The body is validated once, before anything changes;
    then each contract is authorised and changed on its own, and a failure is
    reported in its result without stopping the rest. A contract the caller
    cannot see is ``not_found``, exactly as PATCH /contracts/{id} answers."""
    body = _body(event)
    unknown = set(body) - {"contractIds", "fields", "owner"}
    if unknown:
        raise workflow.BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")
    ids = body.get("contractIds")
    if not isinstance(ids, list) or not 1 <= len(ids) <= _MAX_BULK or not all(isinstance(i, str) for i in ids):
        raise workflow.BadRequest(f"contractIds must be a list of 1 to {_MAX_BULK} contract ids")
    if len(set(ids)) != len(ids):
        raise workflow.BadRequest("contractIds must not repeat an id")
    raw_fields = body.get("fields")
    if raw_fields is not None and not isinstance(raw_fields, dict):
        raise workflow.BadRequest("fields must be an object")
    fields = workflow.clean_fields(raw_fields or {})
    owner = workflow.parse_person(body["owner"], "owner") if body.get("owner") is not None else None
    if not fields and owner is None:
        raise workflow.BadRequest("Send fields and / or owner to update")

    cfg = _Settings()
    results: list[dict[str, Any]] = []
    for contract_id in ids:
        try:
            if not _ID_RE.fullmatch(contract_id):
                raise workflow.NotFound(_NOT_FOUND)
            c, role = _contract_for(contract_id, user, "edit")
            if owner is not None:
                # Checked before the fields are written, so a contract that
                # cannot be assigned (rejected, closed) is left untouched.
                if "assign" not in workflow.allowed_actions(c, _viewer(user, role), cfg(c["tenantId"])):
                    raise workflow.InvalidTransition(
                        f"You cannot assign a contract that is {c.get('state', '').replace('_', ' ')}.")
            if fields:
                workflow.update_fields(contract_id, fields, user.person, cfg=cfg(c["tenantId"]))
            if owner is not None:
                workflow.perform_action(contract_id, "assign", {"owner": owner}, user.person, cfg=cfg(c["tenantId"]))
            results.append({"contractId": contract_id, "ok": True})
        except workflow.GovernError as exc:
            results.append({"contractId": contract_id, "ok": False, "error": {"code": exc.code, "message": str(exc)}})
        except ContractConflict:
            results.append({"contractId": contract_id, "ok": False, "error": {
                "code": "conflict", "message": "Someone else changed this at the same time. Please try again."}})
        except Exception as exc:  # noqa: BLE001 - one contract's failure must not stop the rest
            log.error("govern_api.bulk_item_failed", error_type=type(exc).__name__, trace=safe_trace(exc))
            results.append({"contractId": contract_id, "ok": False,
                            "error": {"code": "internal", "message": "Internal server error"}})
    updated = sum(1 for r in results if r["ok"])
    log.info("govern_api.bulk_update", requested=len(ids), updated=updated)
    return _ok({"results": results, "updated": updated})


def _post_action(event: dict[str, Any], user: GovernUser, contract_id: str) -> dict[str, Any]:
    body = _body(event)
    action = body.pop("action", None)
    if action not in workflow.ACTIONS:
        raise workflow.BadRequest(f"action must be one of: {', '.join(workflow.ACTIONS)}")
    c, role = _contract_for(contract_id, user, "view")
    cfg = workflow.get_settings(c["tenantId"])
    viewer = _viewer(user, role)
    if action not in workflow.allowed_actions(c, viewer, cfg):
        if action in workflow.allowed_actions(c, workflow.Viewer(can_edit=True), cfg):
            raise _Forbidden(_FORBIDDEN)
        raise workflow.InvalidTransition(
            f"You cannot {action.replace('_', ' ')} a contract that is {c.get('state', '').replace('_', ' ')}.")
    if action == "office_approve" and not (viewer.can_edit and not viewer.is_leader) \
            and body.get("office") not in workflow.routed_offices_for(c, viewer, cfg):
        raise _Forbidden("You can approve only for an office this contract is waiting on")
    workflow.perform_action(contract_id, action, body, user.person, cfg=cfg)
    return _contract_response(contract_id, user, role)


def _delete_contract(event: dict[str, Any], user: GovernUser, contract_id: str) -> dict[str, Any]:
    """Remove a contract from Govern (the document owner only). The response
    lists its documents; the app then deletes them (DELETE /documents/{id}),
    which removes the files and the analysis."""
    _contract_for(contract_id, user, "delete_document")
    doc_ids = workflow.remove_contract(contract_id, user.person)
    return _ok({"deleted": True, "contractId": contract_id, "docIds": doc_ids})


def _rescore(event: dict[str, Any], user: GovernUser, contract_id: str) -> dict[str, Any]:
    _, role = _contract_for(contract_id, user, "edit")
    workflow.rescore(contract_id, user.person, reason="manual")
    return _contract_response(contract_id, user, role)


def _add_blocker(event: dict[str, Any], user: GovernUser, contract_id: str) -> dict[str, Any]:
    _, role = _contract_for(contract_id, user, "edit")
    workflow.add_blocker(contract_id, _body(event), user.person)
    return _contract_response(contract_id, user, role)


def _edit_blocker(event: dict[str, Any], user: GovernUser, contract_id: str, blocker_id: str) -> dict[str, Any]:
    _, role = _contract_for(contract_id, user, "edit")
    workflow.edit_blocker(contract_id, blocker_id, _body(event), user.person)
    return _contract_response(contract_id, user, role)


def _add_obligation(event: dict[str, Any], user: GovernUser, contract_id: str) -> dict[str, Any]:
    _, role = _contract_for(contract_id, user, "edit")
    workflow.add_obligation(contract_id, _body(event), user.person)
    return _contract_response(contract_id, user, role)


def _edit_obligation(event: dict[str, Any], user: GovernUser, contract_id: str, obligation_id: str) -> dict[str, Any]:
    _, role = _contract_for(contract_id, user, "edit")
    workflow.edit_obligation(contract_id, obligation_id, _body(event), user.person)
    return _contract_response(contract_id, user, role)


def _put_income(event: dict[str, Any], user: GovernUser, contract_id: str) -> dict[str, Any]:
    _, role = _contract_for(contract_id, user, "edit")
    workflow.replace_income(contract_id, _body(event), user.person)
    return _contract_response(contract_id, user, role)


def _revision_upload_url(event: dict[str, Any], user: GovernUser, contract_id: str) -> dict[str, Any]:
    """A new document linked to the contract (``revisionOf``). It is filed in
    the contract's projects the caller may upload to, so everyone who sees the
    contract sees the revision; intake links and rescores it on analysis."""
    c, _ = _contract_for(contract_id, user, "edit")
    if c.get("state") in workflow.TERMINAL_STATES:
        raise workflow.InvalidTransition("Reopen this contract before uploading a revised version.")
    filename, problem = clean_upload_filename((event.get("queryStringParameters") or {}).get("filename"))
    if problem:
        raise workflow.BadRequest(problem)
    raw_bucket = settings.raw_bucket or os.environ.get("RAW_BUCKET", "")
    if not raw_bucket:
        log.error("govern_api.missing_raw_bucket")
        return _err(500, "Server misconfiguration: RAW_BUCKET not set", "internal")
    project_ids = _uploadable_projects(c, user)
    upload_url, doc_id = revisions.presign_revision(
        c, raw_bucket=raw_bucket, tenant_id=user.caller.tenant_id, owner_sub=user.caller.sub,
        owner_email=user.caller.email, filename=filename, project_ids=project_ids)
    log.info("govern_api.revision_upload", contractId=contract_id, docId=doc_id, projects=len(project_ids))
    return _ok({"uploadUrl": upload_url, "docId": doc_id})


def _uploadable_projects(c: dict[str, Any], user: GovernUser) -> list[str]:
    """The contract's projects the caller may upload to (a revision is filed there)."""
    return [p for p in revisions.base_project_ids(c) if can(user.caller.project_role(p), "upload")]


# ---------------------------------------------------------------------------
# Counterparty portal links (shared/govern/portal.py; the public side is the
# govern-portal Lambda)
# ---------------------------------------------------------------------------


def _create_portal_link(event: dict[str, Any], user: GovernUser, contract_id: str) -> dict[str, Any]:
    c, _ = _contract_for(contract_id, user, "edit")
    view, url = portal.create_link(c, _body(event), creator=user.person, creator_sub=user.caller.sub,
                                   creator_email=user.caller.email, upload_tenant_id=user.caller.tenant_id,
                                   project_ids=_uploadable_projects(c, user))
    log.info("govern_api.portal_link_created", contractId=contract_id, linkId=view["id"])
    return _ok({"id": view["id"], "url": url, "expiresAt": view["expiresAt"], "link": view}, 201)


def _list_portal_links(event: dict[str, Any], user: GovernUser, contract_id: str) -> dict[str, Any]:
    _contract_for(contract_id, user, "edit")
    links = portal.list_links(contract_id)
    return _ok({"links": links, "activeCount": sum(1 for v in links if v["status"] == "active"),
                "maxActive": portal.MAX_ACTIVE_LINKS})


def _revoke_portal_link(event: dict[str, Any], user: GovernUser, contract_id: str, link_id: str) -> dict[str, Any]:
    c, _ = _contract_for(contract_id, user, "edit")
    view = portal.revoke_link(c, link_id, user.person)
    log.info("govern_api.portal_link_revoked", contractId=contract_id, linkId=link_id)
    return _ok({"link": view})


# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------


def _get_matrix(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    tenant = user.caller.tenant_id
    return _ok({"current": store.config.current_matrix(tenant), "versions": store.config.matrix_versions(tenant)})


def _get_matrix_version(event: dict[str, Any], user: GovernUser, version: str) -> dict[str, Any]:
    matrix = store.config.matrix_version(user.caller.tenant_id, int(version))
    if matrix is None:
        raise workflow.NotFound("Matrix version not found")
    return _ok({"matrix": matrix})


def _matrix_meta(body: dict[str, Any]) -> tuple[str | None, str | None]:
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise workflow.BadRequest("note must be a string")
    eff = body.get("effectiveDate")
    if eff is not None and (not isinstance(eff, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", eff)
                            or store.parse_iso(eff) is None):
        raise workflow.BadRequest("effectiveDate must be a date (YYYY-MM-DD)")
    return ((note or "").strip()[:500] or None), eff


def _home_state_arg(body: dict[str, Any]) -> Any:
    """``homeState`` from the body: a US state or DC name, null to clear, or
    left out (``...``) to keep the current one."""
    if "homeState" not in body:
        return ...
    raw = body.get("homeState")
    if raw is None or raw == "":
        return None
    state = workflow._m().home_state(raw)
    if state is None:
        raise workflow.BadRequest("homeState must be the name of a US state or the District of Columbia")
    return state


def _put_matrix(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    _require_admin(user)
    body = _body(event)
    note, effective = _matrix_meta(body)
    clean, problem = workflow._m().validate_matrix(body.get("playbooks"))
    if problem:
        raise workflow.BadRequest(problem)
    matrix = store.config.save_matrix(user.caller.tenant_id, clean, created_by=user.person, note=note,
                                      effective_date=effective, home_state=_home_state_arg(body))
    log.info("govern_api.matrix_saved", version=matrix["version"])
    return _ok({"matrix": matrix})


def _import_matrix(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    _require_admin(user)
    body = _body(event)
    m = workflow._m()
    agreement_type = body.get("agreementType")
    if agreement_type not in m.AGREEMENT_TYPES:
        raise workflow.BadRequest(f"agreementType must be one of: {', '.join(m.AGREEMENT_TYPES)}")
    mode = body.get("mode") or "merge"
    if mode not in ("replace", "merge"):
        raise workflow.BadRequest("mode must be replace or merge")
    rows, csv_text = body.get("rows"), body.get("csv")
    if (rows is None) == (csv_text is None):
        raise workflow.BadRequest("Send either rows or csv")
    if rows is not None and (not isinstance(rows, list) or len(rows) > 1000):
        raise workflow.BadRequest("rows must be a list (max 1000)")
    if csv_text is not None and (not isinstance(csv_text, str) or len(csv_text) > 1_000_000):
        raise workflow.BadRequest("csv must be text (max 1 MB)")
    note, effective = _matrix_meta(body)
    clauses, skipped = m.parse_import(agreement_type, rows=rows, csv_text=csv_text)
    if not clauses:
        raise workflow.BadRequest("No usable rows to import" + (f" ({len(skipped)} skipped)" if skipped else ""))
    current = store.config.current_matrix(user.caller.tenant_id)
    merged = m.merge_import(current["playbooks"], agreement_type, clauses, mode)
    clean, problem = m.validate_matrix(merged)
    if problem:
        raise workflow.BadRequest(problem)
    matrix = store.config.save_matrix(user.caller.tenant_id, clean, created_by=user.person,
                                      note=note or f"Imported {len(clauses)} clause(s) for {workflow.agreement_label(agreement_type)}",
                                      effective_date=effective)
    return _ok({"matrix": matrix, "imported": len(clauses), "skipped": skipped})


# ---------------------------------------------------------------------------
# Workflow settings
# ---------------------------------------------------------------------------


def _get_settings(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    return _ok({"settings": workflow.get_settings(user.caller.tenant_id)})


def _put_settings(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    _require_admin(user)
    tenant = user.caller.tenant_id
    body = _body(event)
    clean = workflow.validate_settings(body, workflow.get_settings(tenant))
    if "teamsWebhookUrl" in body:
        url = body["teamsWebhookUrl"]
        if url is not None and (not isinstance(url, str) or not _TEAMS_URL_RE.match(url.strip())):
            raise workflow.BadRequest("teamsWebhookUrl must be a Microsoft Teams incoming-webhook https URL, or null")
        try:
            secrets.put_tenant_value(settings.teams_secret_arn, tenant, url.strip() if url else None)
        except secrets.SecretUnavailable:
            raise workflow.BadRequest("Teams alerts are not configured on this deployment.")
        clean["teamsWebhookConfigured"] = bool(url)
    store.config.put_settings(tenant, clean)
    return _ok({"settings": workflow.get_settings(tenant)})


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------


def _list_integrations(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    _require_admin(user)
    return _ok({"connectors": connectors.list_connectors(user.caller.tenant_id)})


def _put_integration(event: dict[str, Any], user: GovernUser, connector_id: str) -> dict[str, Any]:
    _require_admin(user)
    return _ok({"connector": connectors.update_connector(user.caller.tenant_id, connector_id, _body(event))})


def _sync_integration(event: dict[str, Any], user: GovernUser, connector_id: str) -> dict[str, Any]:
    _require_admin(user)
    workflow.require_feature("integrations", "Syncing with Huron, Workday and other systems")
    run = connectors.run_connector(user.caller.tenant_id, connector_id, "manual")
    return _ok({"run": connectors.sync_run_view(run)})


def _sync_log(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    _require_admin(user)
    return _ok({"runs": [connectors.sync_run_view(r) for r in store.sync.runs(user.caller.tenant_id, 100)]})


def _unmatched(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    _require_admin(user)
    contracts, docs = _visible_contracts(user, create_missing=False)
    wanted = [c for c in contracts if (c.get("workdayMatch") or "unmatched") == "unmatched"
              and c.get("state") not in workflow.TERMINAL_STATES]
    return _ok({"contracts": _render_list(wanted, docs, datetime.now(timezone.utc), user)})


# ---------------------------------------------------------------------------
# Reports and me
# ---------------------------------------------------------------------------


def _trends(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    # Portfolio totals span every contract of the workspace, so they are for
    # the people who run it, not for any signed-in user.
    if not (user.is_admin or user.is_leader):
        raise _Forbidden("Trends are available to Govern admins and leaders")
    qs = event.get("queryStringParameters") or {}
    granularity = qs.get("granularity") or "month"
    if granularity not in ("month", "week"):
        raise workflow.BadRequest("granularity must be month or week")
    try:
        periods = int(qs.get("periods") or 12)
    except ValueError:
        raise workflow.BadRequest("periods must be a whole number")
    if periods < 1:
        raise workflow.BadRequest("periods must be at least 1")
    return _ok(aggregates.trends(user.caller.tenant_id, granularity, periods,
                                 label=workflow._m().matrix_clause_label))


def _capture_report(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    contracts, docs = _visible_contracts(user, create_missing=False)
    open_contracts = [c for c in contracts if c.get("state") not in workflow.TERMINAL_STATES]
    state = capture.get_reconcile_state(user.caller.tenant_id)
    return _ok({"gaps": capture.gap_summary(open_contracts),
                "missedDocuments": capture.missed_documents(docs.values(), contracts),
                "lastReconciledAt": state.get("lastReconciledAt")})


def _edition_for(tenant_id: str) -> str:
    """The organization's edition (Settings → Organization), else the deployment default."""
    try:
        chosen = (workflow.get_settings(tenant_id).get("organization") or {}).get("edition")
    except Exception:  # noqa: BLE001 - the profile must load even if settings cannot
        chosen = None
    return chosen if chosen in govern_matrix.EDITIONS else settings.govern_default_edition


def _features_for(tenant_id: str) -> dict[str, bool]:
    """The deployment's features, narrowed to the organization's modules when it has chosen them."""
    flags = settings.govern_feature_flags()
    try:
        mods = (workflow.get_settings(tenant_id).get("organization") or {}).get("enabledModules")
    except Exception:  # noqa: BLE001 - the profile must load even if settings cannot
        mods = None
    if isinstance(mods, list):
        flags = {k: on and k in mods for k, on in flags.items()}
    return flags


def _me(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    p = user.person or {}
    user.ensure_org()
    edition = _edition_for(user.caller.tenant_id)
    return _ok({"email": p.get("email"), "name": p.get("name"), "role": user.role,
                "tenantId": user.caller.tenant_id,
                # Admin of THIS organization (its creator, or added by an admin).
                "isAdmin": user.is_admin, "isCreator": user.is_creator, "isLeader": user.is_leader,
                # Requirement 7: which edition this organization sees, and its vocabulary.
                "edition": edition,
                "defaultEdition": settings.govern_default_edition,
                "agreementTypes": [{"id": t, "label": govern_matrix.AGREEMENT_TYPE_LABELS[t]}
                                   for t in govern_matrix.edition_agreement_types(edition)],
                # Which "Later" features this deployment has on (GOVERN_FEATURES).
                "features": _features_for(user.caller.tenant_id),
                "deploymentFeatures": settings.govern_feature_flags()})


# ---------------------------------------------------------------------------
# The organization and its admins (shared/govern/org.py)
# ---------------------------------------------------------------------------


def _org_response(user: GovernUser, record: dict[str, Any] | None) -> dict[str, Any]:
    tenant = user.caller.tenant_id
    out = org.view(record, tenant, user.caller.sub)
    out["name"] = ((workflow.get_settings(tenant).get("organization") or {}).get("name"))
    if out["you"]["isAdmin"]:
        # Who can be made an admin (people who signed in here) and the trail.
        out["members"] = org.members(tenant)
        out["activity"] = [org.audit_view(e) for e in store.config.org_audit(tenant)]
    return _ok(out)


def _get_org(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    return _org_response(user, user.ensure_org())


def _rev_arg(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.isdigit() and len(raw) <= 9:
        return int(raw)
    raise workflow.BadRequest("rev must be a whole number")


def _add_org_admin(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    _require_admin(user)
    body = _body(event)
    unknown = set(body) - {"email", "rev"}
    if unknown:
        raise workflow.BadRequest(f"Unknown field(s): {', '.join(sorted(unknown))}")
    record = org.add_admin(user.caller.tenant_id, user.actor, body.get("email"), _rev_arg(body.get("rev")))
    return _org_response(user, record)


def _remove_org_admin(event: dict[str, Any], user: GovernUser, sub: str) -> dict[str, Any]:
    _require_admin(user)
    if not _SUB_RE.fullmatch(sub):
        raise workflow.NotFound("That person is not an admin of this organization")
    rev = _rev_arg((event.get("queryStringParameters") or {}).get("rev"))
    record = org.remove_admin(user.caller.tenant_id, user.actor, sub, rev)
    return _org_response(user, record)


def _people(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    """People the caller can reasonably assign or share with: members (and
    owners) of the projects they belong to, the workspace's reviewers, and
    owners of the contracts they can see — plus the caller. Nothing outside
    what the caller can already see is listed."""
    found: dict[str, dict[str, Any]] = {}

    def add(email: Any, name: Any, source: str | None) -> None:
        from shared.access import normalise_email

        address = normalise_email(email)
        if not address:
            return
        p = found.setdefault(address, {"email": address, "name": None, "sources": set()})
        if not p["name"] and isinstance(name, str) and name.strip():
            p["name"] = name.strip()[:200]
        if source:
            p["sources"].add(source)

    roles = user.caller.project_roles()
    for project in ddb.get_projects(list(roles)):
        add(project.get("ownerEmail"), None, "project")
        for member in ddb.list_project_members(project["projectId"]):
            add(member.get("email"), member.get("name"), "project")
    for reviewer in workflow.get_settings(user.caller.tenant_id).get("reviewers") or []:
        add(reviewer.get("email"), reviewer.get("name"), "reviewer")
    contracts, _docs = _visible_contracts(user, create_missing=False)
    for c in contracts:
        owner = c.get("owner") or {}
        add(owner.get("email"), owner.get("name"), "owner")
    me = user.person or {}
    add(me.get("email"), me.get("name"), None)

    people = sorted(found.values(), key=lambda p: (p["name"] is None, (p["name"] or "").lower(), p["email"]))
    return _ok({"people": [{"email": p["email"], "name": p["name"],
                            "sources": [s for s in _PEOPLE_SOURCES if s in p["sources"]]}
                           for p in people[:_MAX_PEOPLE]]})


# ---------------------------------------------------------------------------
# Saved views (shared/govern/views.py)
# ---------------------------------------------------------------------------


def _list_views(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    module = (event.get("queryStringParameters") or {}).get("module")
    return _ok({"views": views.list_views(user.caller.tenant_id, module, user.caller.sub, user.caller.email)})


def _create_view(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    view = views.create_view(user.caller.tenant_id, _body(event), user.caller.sub, user.person)
    return _ok({"view": view}, 201)


def _patch_view(event: dict[str, Any], user: GovernUser, view_id: str) -> dict[str, Any]:
    view = views.update_view(user.caller.tenant_id, view_id, _body(event), user.caller.sub, user.caller.email)
    return _ok({"view": view})


def _delete_view(event: dict[str, Any], user: GovernUser, view_id: str) -> dict[str, Any]:
    deleted = views.delete_view(user.caller.tenant_id, view_id, user.caller.sub, user.caller.email)
    return _ok({"deleted": True, "id": deleted})


# ---------------------------------------------------------------------------
# Automations (shared/govern/automations.py)
# ---------------------------------------------------------------------------


def _list_automations(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    tenant = user.caller.tenant_id
    rules = automations.list_rules(tenant)
    return _ok({"automations": rules, "count": len(rules), "limit": automations.MAX_RULES,
                "enabled": automations.enabled_for(tenant)})


def _create_automation(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    _require_admin(user)
    workflow.require_feature(automations.FEATURE, "Automations")
    return _ok({"automation": automations.create_rule(user.caller.tenant_id, _body(event), user.person)}, 201)


def _patch_automation(event: dict[str, Any], user: GovernUser, rule_id: str) -> dict[str, Any]:
    _require_admin(user)
    workflow.require_feature(automations.FEATURE, "Automations")
    return _ok({"automation": automations.update_rule(user.caller.tenant_id, rule_id, _body(event), user.person)})


def _delete_automation(event: dict[str, Any], user: GovernUser, rule_id: str) -> dict[str, Any]:
    _require_admin(user)
    return _ok({"deleted": True, "id": automations.delete_rule(user.caller.tenant_id, rule_id)})


def _test_automation(event: dict[str, Any], user: GovernUser, rule_id: str) -> dict[str, Any]:
    tenant = user.caller.tenant_id
    rule, contract_id = automations.resolve_for_test(tenant, rule_id, _body(event))
    contract, _role = _contract_for(contract_id, user, "view")
    return _ok({"result": automations.dry_run(tenant, rule, contract)})


# ---------------------------------------------------------------------------
# Route table (order matters: literal segments before wildcards)
# ---------------------------------------------------------------------------

_SEG = r"([^/]+)"
_RAW_ROUTES: list[tuple[tuple[str, str], Callable[..., dict[str, Any]]]] = [
    (("GET", r"/govern/me"), _me),
    (("GET", r"/govern/org"), _get_org),
    (("POST", r"/govern/org/admins"), _add_org_admin),
    (("DELETE", rf"/govern/org/admins/{_SEG}"), _remove_org_admin),
    (("GET", r"/contracts"), _list_contracts),
    (("GET", r"/obligations"), _list_obligations),
    (("POST", r"/contracts"), _create_contract),
    (("POST", r"/contracts/bulk"), _bulk_update),
    (("GET", rf"/contracts/{_SEG}"), _get_contract),
    (("PATCH", rf"/contracts/{_SEG}"), _patch_contract),
    (("DELETE", rf"/contracts/{_SEG}"), _delete_contract),
    (("POST", rf"/contracts/{_SEG}/actions"), _post_action),
    (("POST", rf"/contracts/{_SEG}/rescore"), _rescore),
    (("POST", rf"/contracts/{_SEG}/blockers"), _add_blocker),
    (("PATCH", rf"/contracts/{_SEG}/blockers/{_SEG}"), _edit_blocker),
    (("POST", rf"/contracts/{_SEG}/obligations"), _add_obligation),
    (("PATCH", rf"/contracts/{_SEG}/obligations/{_SEG}"), _edit_obligation),
    (("PUT", rf"/contracts/{_SEG}/income"), _put_income),
    (("GET", rf"/contracts/{_SEG}/revision-upload-url"), _revision_upload_url),
    (("GET", rf"/contracts/{_SEG}/activity"), _contract_activity),
    (("GET", rf"/contracts/{_SEG}/portal-links"), _list_portal_links),
    (("POST", rf"/contracts/{_SEG}/portal-links"), _create_portal_link),
    (("DELETE", rf"/contracts/{_SEG}/portal-links/{_SEG}"), _revoke_portal_link),
    (("GET", r"/matrix"), _get_matrix),
    (("PUT", r"/matrix"), _put_matrix),
    (("POST", r"/matrix/import"), _import_matrix),
    (("GET", r"/matrix/versions/(\d{1,6})"), _get_matrix_version),
    (("GET", r"/workflow/settings"), _get_settings),
    (("PUT", r"/workflow/settings"), _put_settings),
    (("GET", r"/integrations"), _list_integrations),
    (("GET", r"/integrations/sync-log"), _sync_log),
    (("GET", r"/integrations/unmatched"), _unmatched),
    (("PUT", rf"/integrations/{_SEG}"), _put_integration),
    (("POST", rf"/integrations/{_SEG}/sync"), _sync_integration),
    (("GET", r"/reports/trends"), _trends),
    (("GET", r"/reports/capture"), _capture_report),
    (("GET", r"/govern/people"), _people),
    (("GET", r"/views"), _list_views),
    (("POST", r"/views"), _create_view),
    (("PATCH", rf"/views/{_SEG}"), _patch_view),
    (("DELETE", rf"/views/{_SEG}"), _delete_view),
    (("GET", r"/automations"), _list_automations),
    (("POST", r"/automations"), _create_automation),
    (("PATCH", rf"/automations/{_SEG}"), _patch_automation),
    (("DELETE", rf"/automations/{_SEG}"), _delete_automation),
    (("POST", rf"/automations/{_SEG}/test"), _test_automation),
]
_ROUTES = [((method, re.compile(pattern)), fn) for (method, pattern), fn in _RAW_ROUTES]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded") and raw:
        import base64

        raw = base64.b64decode(raw).decode("utf-8", errors="replace")
    if len(raw.encode("utf-8")) > _MAX_BODY_BYTES:
        raise workflow.BadRequest("Request body is too large")
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise workflow.BadRequest("Invalid JSON body")
    if not isinstance(body, dict):
        raise workflow.BadRequest("Body must be a JSON object")
    return body


def _plain(val: Any) -> Any:
    if isinstance(val, Decimal):
        return int(val) if val == val.to_integral_value() else float(val)
    if isinstance(val, dict):
        return {k: _plain(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_plain(v) for v in val]
    if isinstance(val, (set, frozenset)):
        return sorted(_plain(v) for v in val)
    return val


def _ok(body: Any, status: int = 200) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(_plain(body), default=str)}


def _err(status: int, message: str, code: str) -> dict[str, Any]:
    return _ok({"error": message, "code": code}, status)
