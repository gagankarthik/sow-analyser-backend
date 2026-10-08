"""Govern API Lambda — every route of docs/GOVERN_API.md.

Routes                                                      needs
------                                                      -----
GET    /govern/me                                           signed in
GET    /contracts[?includeClosed=true]                      (lists what the caller may see)
GET    /obligations                                         (open, dated obligations of visible contracts)
POST   /contracts                                           edit on the document
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
GET    /matrix · GET /matrix/versions/{n}                   signed in
PUT    /matrix · POST /matrix/import                        Govern admin
GET    /workflow/settings                                   signed in
PUT    /workflow/settings                                   Govern admin
GET    /integrations · /integrations/sync-log · /integrations/unmatched
PUT    /integrations/{id} · POST /integrations/{id}/sync    Govern admin
GET    /reports/trends                                      Govern admin or leader
GET    /reports/capture                                     signed in (what the caller may see)

ACCESS. A contract is visible exactly when its document is: the caller's role
on the contract is their strongest role (shared/access.py) on its first
document (``contractId``) or its current one (``currentDocId``). No role →
404, so ids cannot be probed; a role without the capability → 403.

GOVERN ROLES come from the Cognito ``cognito:groups`` claim: ``govern-admin``
(matrix, routing, settings, integrations), ``govern-reviewer`` (actions),
``govern-leader`` (view, comment, approve when routed). Which actions a
caller may take is ``Contract.allowedActions`` (workflow.allowed_actions):
editors get every action valid in the state; viewers and leaders may comment,
and office-approve when they approve for an office the contract waits on.
When ``GOVERN_OPEN_ADMIN`` is true (sandbox default) a user in no Govern
group is an admin.

Every state change goes through shared/govern/workflow.py; this module only
authorises, validates the request shape and renders responses.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from aws_lambda_powertools import Tracer

from shared import dynamodb as ddb
from shared.access import Caller, can
from shared.auth import AuthError, jwt_claims
from shared.config import settings
from shared.govern import aggregates, capture, connectors, secrets, store, workflow
from shared.govern.store import ContractConflict, iso
from shared.logger import get_logger
from shared.uploads import clean_upload_filename, pending_document_meta, upload_key

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
_ROLE_GROUPS = (("govern-admin", "admin"), ("govern-reviewer", "reviewer"), ("govern-leader", "leader"))
_TEAMS_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+\.(?:webhook\.office\.com|logic\.azure\.com|office\.com)(?::\d+)?/\S+$")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class GovernUser:
    """The verified caller plus their display name and Govern role."""

    def __init__(self, caller: Caller, claims: dict[str, Any]) -> None:
        self.caller = caller
        self.claims = claims
        self.groups = _groups(claims.get("cognito:groups"))
        self.explicit_role = next((role for group, role in _ROLE_GROUPS if group in self.groups), None)
        self.role = self.explicit_role or ("admin" if settings.govern_open_admin else "reviewer")
        name = claims.get("name") or " ".join(p for p in (claims.get("given_name"), claims.get("family_name")) if p)
        self.person = workflow.person(caller.email or claims.get("email") or caller.sub, name or None)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_leader(self) -> bool:
        return self.explicit_role == "leader"


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


@tracer.capture_lambda_handler
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
    except workflow.GovernError as exc:
        return _err(exc.status, str(exc), exc.code)
    except ContractConflict:
        return _err(409, "Someone else changed this at the same time. Please try again.", "conflict")
    except Exception as exc:  # noqa: BLE001
        log.exception("govern_api.unhandled_error", error_type=type(exc).__name__)
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


def _contract_role(c: dict[str, Any], user: GovernUser, metas: dict[str, dict[str, Any]] | None = None) -> str | None:
    """Strongest document role on the contract's first or current document."""
    from shared.access import stronger

    ids = list(dict.fromkeys([c["contractId"], c.get("currentDocId") or c["contractId"]]))
    if metas is None:
        metas = {m["docId"]: m for m in ddb.get_docs(ids) if m.get("docId")}
    role = None
    for doc_id in ids:
        role = stronger(role, user.caller.document_role(metas.get(doc_id)))
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
        role = stronger((docs.get(c["contractId"]) or {}).get("_role"),
                        (docs.get(c.get("currentDocId") or "") or {}).get("_role"))
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
    return workflow.detail(
        c, now=now, cfg=workflow.get_settings(c["tenantId"]), doc_meta=_analysis_meta(c, metas), viewer=viewer,
        version_metas=metas, blockers=store.contracts.blockers(contract_id),
        obligations=store.contracts.obligations(contract_id), income=store.contracts.income(contract_id),
        review=review, activity=store.activity.recent(contract_id, 200))


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
    base = ddb.get_doc_meta(c.get("currentDocId") or contract_id) or ddb.get_doc_meta(contract_id) or {}
    project_ids = [p for p in base.get("projectIds") or [] if can(user.caller.project_role(p), "upload")]
    doc_id = str(uuid.uuid4())
    tenant_id = user.caller.tenant_id
    from shared.aws import s3_client

    upload_url = s3_client().generate_presigned_url(
        "put_object", Params={"Bucket": raw_bucket, "Key": upload_key(tenant_id, doc_id, filename)}, ExpiresIn=300)
    ddb.put_doc_meta(pending_document_meta(
        doc_id=doc_id, tenant_id=tenant_id, owner_sub=user.caller.sub, owner_email=user.caller.email,
        filename=filename, doc_type=base.get("docType") or "OTHER", project_ids=project_ids,
        extra={"parentDocId": contract_id, "revisionOf": contract_id}))
    for project_id in project_ids:
        ddb.mutate_project(project_id, lambda p: p.__setitem__("docIds", list(p.get("docIds") or []) + [doc_id]))

    def link(stored: dict[str, Any]) -> None:
        stored["versionDocIds"] = list(stored.get("versionDocIds") or [contract_id]) + [doc_id]
        stored.setdefault("versionRounds", {})[doc_id] = int(stored.get("rounds") or 0) + 1
        stored["pendingDocId"] = doc_id
        stored["updatedAt"] = iso()

    store.contracts.mutate(contract_id, link)
    log.info("govern_api.revision_upload", contractId=contract_id, docId=doc_id, projects=len(project_ids))
    return _ok({"uploadUrl": upload_url, "docId": doc_id})


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


def _me(event: dict[str, Any], user: GovernUser) -> dict[str, Any]:
    p = user.person or {}
    return _ok({"email": p.get("email"), "name": p.get("name"), "role": user.role,
                "tenantId": user.caller.tenant_id,
                # Which "Later" features this deployment has on (GOVERN_FEATURES).
                "features": settings.govern_feature_flags()})


# ---------------------------------------------------------------------------
# Route table (order matters: literal segments before wildcards)
# ---------------------------------------------------------------------------

_SEG = r"([^/]+)"
_RAW_ROUTES: list[tuple[tuple[str, str], Callable[..., dict[str, Any]]]] = [
    (("GET", r"/govern/me"), _me),
    (("GET", r"/contracts"), _list_contracts),
    (("GET", r"/obligations"), _list_obligations),
    (("POST", r"/contracts"), _create_contract),
    (("GET", rf"/contracts/{_SEG}"), _get_contract),
    (("PATCH", rf"/contracts/{_SEG}"), _patch_contract),
    (("POST", rf"/contracts/{_SEG}/actions"), _post_action),
    (("POST", rf"/contracts/{_SEG}/rescore"), _rescore),
    (("POST", rf"/contracts/{_SEG}/blockers"), _add_blocker),
    (("PATCH", rf"/contracts/{_SEG}/blockers/{_SEG}"), _edit_blocker),
    (("POST", rf"/contracts/{_SEG}/obligations"), _add_obligation),
    (("PATCH", rf"/contracts/{_SEG}/obligations/{_SEG}"), _edit_obligation),
    (("PUT", rf"/contracts/{_SEG}/income"), _put_income),
    (("GET", rf"/contracts/{_SEG}/revision-upload-url"), _revision_upload_url),
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
