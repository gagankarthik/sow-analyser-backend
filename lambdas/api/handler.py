"""Document management API Lambda — backs API Gateway HTTP API routes.

Access is per project, by membership, enforced HERE on every route (see
shared/access.py for the rule and the permission matrix). A caller reaches a
document only if they uploaded it or it is filed in a project they belong to;
anything else answers 404. A caller who can see something but may not change it
gets 403 with ``code: "forbidden"``.

Routes                                               needs
------                                               -----
GET    /documents                                    (lists what the caller may see)
GET    /documents/upload-url[?projectId=]            upload on that project, if given
GET    /documents/{docId}                            view
PATCH  /documents/{docId}                            edit
DELETE /documents/{docId}                            delete_document
DELETE /documents/{docId}/versions/{n}               delete_document
GET    /documents/{docId}/classification             view
GET    /documents/{docId}/diff                       view (and view of the parent)
GET    /documents/{docId}/timeline                   view (chain limited to what is visible)
GET    /documents/{docId}/file                       view
GET    /documents/{docId}/similar                    view (searches visible documents only)
POST   /documents/{docId}/reprocess                  reprocess
GET    /projects                                     (lists the caller's projects)
POST   /projects                                     legacy whole-list save — merged, see _save_projects
GET    /projects/{id}                                view
PUT    /projects/{id}                                create (caller becomes owner) / rename_project
DELETE /projects/{id}                                delete_project
PUT    /projects/{id}/documents/{docId}              manage_documents (+ share_document on the document)
DELETE /projects/{id}/documents/{docId}              manage_documents
POST   /projects/{id}/invite                         invite
PATCH  /projects/{id}/members/{email}                set_role
DELETE /projects/{id}/members/{email}                remove_member (or the member themself)
GET    /playbook                                     the effective playbook for the caller's workspace
PUT    /playbook/rules/{ruleId}                      create / replace one of the caller's own rules
DELETE /playbook/rules/{ruleId}                      remove it (falls back to the built-in default)
GET    /tenant/compliance                            get enabled compliance packs (frameworks)
POST   /tenant/compliance                            set enabled compliance packs

Identity comes ONLY from the claims of the Cognito JWT that the API Gateway
authorizer has verified (shared/auth.py, shared/access.py). Headers, query
strings and body fields are never identity: the x-tenant-id header and any
``ownerEmail`` / ``members`` a client sends are ignored.

CORS is owned entirely by the API Gateway cors_configuration — this Lambda emits
no CORS headers. Emitting them would create a duplicate/conflicting
Access-Control-Allow-Origin header and break browser requests.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

from shared.config import settings

from aws_lambda_powertools import Tracer
from shared import dynamodb as ddb
from shared import playbook
from shared.access import Caller, can, normalise_email, normalise_role
from shared.auth import AuthError
from shared.aws import s3_client
from shared.dynamodb import (
    delete_doc_entirely,
    delete_doc_version,
    get_compliance_packs,
    get_doc_meta,
    put_compliance_packs,
    put_doc_meta,
    query_doc_versions,
    update_doc_fields,
)
from shared.compliance import resolve_enabled_packs, KNOWN_PACK_IDS
from shared.logger import get_logger
from shared.opensearch import get_clause_vector, knn_search
from shared.s3 import presign_get
from shared.schema import now_iso
from shared.uploads import clean_upload_filename, pending_document_meta, upload_key

log = get_logger("blue-iq.api")
tracer = Tracer(service="blue-iq.api")

_VALID_DOC_TYPES  = frozenset({
    "SOW", "MSA", "AMENDMENT", "NDA",
    "LICENSE", "DPA", "BAA", "COMPLIANCE",
    "OTHER",
})
_VALID_LIFECYCLES = frozenset({
    "draft", "review", "negotiation", "approval",
    "signed", "active", "renewal", "expired",
})

# Document / project ids arrive in the URL path. Ids we mint are UUIDs; anything
# outside this set is rejected before it reaches a DynamoDB key or search query.
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Statuses that mean a pipeline run is in flight, and how long we trust that
# before treating the run as dead (so a stuck document can still be retried).
_IN_FLIGHT_STATUSES = frozenset({
    "PENDING", "PARSING", "CLASSIFYING", "EMBEDDING",
    "GRAPHING", "DIFFING", "TIMELINING", "PERSISTING",
})
_IN_FLIGHT_GRACE = timedelta(minutes=15)

_NOT_FOUND = "Document not found"
_FORBIDDEN = "You do not have permission to do that in this project"
_MAX_PROJECTS = 500
_MAX_PROJECT_DOCS = 2000
_MAX_MEMBERS = 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@tracer.capture_lambda_handler
@log.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    ).upper()
    path = event.get("path") or event.get("rawPath") or "/"

    if method == "OPTIONS":
        return _ok({})

    try:
        caller = Caller.from_event(event)
    except AuthError:
        log.warning("api.no_verified_identity", method=method)
        return _err(403, "Forbidden", "unauthenticated")
    log.append_keys(tenantId=caller.tenant_id, method=method)

    try:
        return _route(method, path, event, caller)
    except ddb.ProjectConflict:
        return _err(409, "This project was changed by someone else at the same time. Please try again.", "conflict")
    except Exception as exc:
        log.exception("api.unhandled_error", error_type=type(exc).__name__)
        return _err(500, "Internal server error", "internal")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _route(method: str, path: str, event: dict[str, Any], caller: Caller) -> dict[str, Any]:
    # Every /documents/{id}/... and /projects/{id}/... route carries an id in
    # the second path segment — validate its shape once, up front.
    seg = path.split("/")
    if len(seg) > 2 and seg[1] in ("documents", "projects") and seg[2] and not _ID_RE.fullmatch(seg[2]):
        return _err(404, "Not found", "not_found")

    if re.fullmatch(r"/projects/?", path):
        if method == "GET":  return _get_projects(caller)
        if method == "POST": return _save_projects(event, caller)
        return _err(405, "Method not allowed", "method_not_allowed")

    # Per-tenant compliance-pack selection (which frameworks Sonar grades against).
    if re.fullmatch(r"/tenant/compliance/?", path):
        if method == "GET":  return _get_compliance(caller.tenant_id)
        if method == "POST": return _save_compliance(event, caller.tenant_id)
        return _err(405, "Method not allowed", "method_not_allowed")

    # The caller's playbook: effective rules, and their own custom rules.
    if re.fullmatch(r"/playbook/?", path):
        if method == "GET":
            return _get_playbook(caller)
        return _err(405, "Method not allowed", "method_not_allowed")
    m = re.fullmatch(r"/playbook/rules/([^/]+)/?", path)
    if m:
        if method == "PUT":    return _put_playbook_rule(unquote(m.group(1)), event, caller)
        if method == "DELETE": return _delete_playbook_rule(unquote(m.group(1)), caller)
        return _err(405, "Method not allowed", "method_not_allowed")

    # POST /projects/{id}/invite — add a member to a project
    m = re.fullmatch(r"/projects/([^/]+)/invite/?", path)
    if method == "POST" and m:
        return _invite_member(m.group(1), event, caller)

    # PATCH / DELETE /projects/{id}/members/{email}
    m = re.fullmatch(r"/projects/([^/]+)/members/([^/]+)/?", path)
    if m:
        if method == "DELETE": return _remove_member(m.group(1), unquote(m.group(2)), caller)
        if method == "PATCH":  return _set_member_role(m.group(1), unquote(m.group(2)), event, caller)

    # PUT / DELETE /projects/{id}/documents/{docId}
    m = re.fullmatch(r"/projects/([^/]+)/documents/([^/]+)/?", path)
    if m:
        if not _ID_RE.fullmatch(m.group(2)):
            return _err(404, "Not found", "not_found")
        if method == "PUT":    return _add_project_document(m.group(1), m.group(2), caller)
        if method == "DELETE": return _remove_project_document(m.group(1), m.group(2), caller)

    m = re.fullmatch(r"/projects/([^/]+)/?", path)
    if m:
        if method == "GET":    return _get_project(m.group(1), caller)
        if method == "PUT":    return _put_project(m.group(1), event, caller)
        if method == "DELETE": return _delete_project(m.group(1), caller)

    if method == "GET" and re.fullmatch(r"/documents/?", path):
        return _list_documents(caller)

    # upload-url must be matched before the /{docId} wildcard
    if method == "GET" and re.fullmatch(r"/documents/upload-url/?", path):
        return _get_upload_url(event, caller)

    m = re.fullmatch(r"/documents/([^/]+)/versions/(\d+)/?", path)
    if method == "DELETE" and m:
        return _delete_version(m.group(1), int(m.group(2)), caller)

    # GET /documents/{docId}/classification
    m = re.fullmatch(r"/documents/([^/]+)/classification/?", path)
    if method == "GET" and m:
        return _get_doc_classification(m.group(1), caller)

    # GET /documents/{docId}/file → presigned URL to the original upload
    m = re.fullmatch(r"/documents/([^/]+)/file/?", path)
    if method == "GET" and m:
        return _get_doc_file(m.group(1), caller)

    # POST /documents/{docId}/reprocess → re-run the pipeline on the stored upload
    m = re.fullmatch(r"/documents/([^/]+)/reprocess/?", path)
    if method == "POST" and m:
        return _reprocess_document(m.group(1), caller)

    # GET /documents/{docId}/diff
    m = re.fullmatch(r"/documents/([^/]+)/diff/?", path)
    if method == "GET" and m:
        return _get_doc_diff(m.group(1), caller)

    # GET /documents/{docId}/timeline
    m = re.fullmatch(r"/documents/([^/]+)/timeline/?", path)
    if method == "GET" and m:
        return _get_doc_timeline(m.group(1), caller)

    # GET /documents/{docId}/similar?clause=<n>&k=<k> — top-KNN similar clauses
    m = re.fullmatch(r"/documents/([^/]+)/similar/?", path)
    if method == "GET" and m:
        return _get_similar_clauses(m.group(1), event, caller)

    m = re.fullmatch(r"/documents/([^/]+)/?", path)
    if m:
        doc_id = m.group(1)
        if method == "GET":    return _get_document(doc_id, caller)
        if method == "PATCH":  return _update_document(doc_id, caller, event)
        if method == "DELETE": return _delete_document(doc_id, caller)

    return _err(404, "Not found", "not_found")


# ---------------------------------------------------------------------------
# Authorisation helpers
# ---------------------------------------------------------------------------


def _doc_for(doc_id: str, caller: Caller, capability: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(meta, None) when the caller may do ``capability`` on the document, else
    (None, error response): 404 if they cannot see it at all, 403 if they can
    see it but their role does not allow the action."""
    meta = get_doc_meta(doc_id)
    role = caller.document_role(meta)
    if role is None:
        return None, _err(404, _NOT_FOUND, "not_found")
    if not can(role, capability):
        return None, _err(403, _FORBIDDEN, "forbidden")
    return dict(meta, _role=role), None


def _project_for(project_id: str, caller: Caller, capability: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    role = caller.project_role(project_id)
    project = ddb.get_project(project_id) if role else None
    if role is None or project is None:
        return None, _err(404, "Project not found", "not_found")
    if not can(role, capability):
        return None, _err(403, _FORBIDDEN, "forbidden")
    return dict(project, _role=role), None


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _list_documents(caller: Caller) -> dict[str, Any]:
    """The caller's own uploads plus every document in a project they belong to."""
    docs = caller.visible_documents()
    out = [_list_view(_clean(d, caller)) for d in docs.values()]
    return _ok({"documents": out, "count": len(out)})


# What a key date carries in the LIST response. The full entry (rawText, rule,
# issues, section reference …) is on GET /documents/{id}, /classification and
# /timeline; a list of many documents only needs enough to draw a dates view.
_LIST_KEY_DATE_FIELDS = ("id", "kind", "label", "date", "precision", "isEstimated", "isDerived",
                         "ambiguous", "amount", "currency", "clauseNumber", "confidence")


def _list_view(doc: dict[str, Any]) -> dict[str, Any]:
    """Trim the per-document payload of the list response so that a user with
    many documents never approaches the response size limit."""
    if isinstance(doc.get("keyDates"), list):
        doc["keyDates"] = [{k: kd.get(k) for k in _LIST_KEY_DATE_FIELDS}
                           for kd in doc["keyDates"] if isinstance(kd, dict)]
    return doc


# ---------------------------------------------------------------------------
# Compliance packs — which regulatory frameworks Sonar grades documents against
# ---------------------------------------------------------------------------


def _get_compliance(tenant_id: str) -> dict[str, Any]:
    saved = get_compliance_packs(tenant_id)
    return _ok({
        "packs":    resolve_enabled_packs(tenant_id),  # effective list (defaults if unset)
        "explicit": saved is not None,                 # has the tenant chosen, or are these defaults?
        "known":    KNOWN_PACK_IDS,
    })


def _save_compliance(event: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    raw_body = event.get("body") or ""
    try:
        body = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body", "bad_request")
    packs = body.get("packs") if isinstance(body, dict) else None
    if not isinstance(packs, list):
        return _err(400, "Body must be {\"packs\": [...] }", "bad_request")
    # Keep only known ids, in canonical order — a client can't store junk.
    wanted = {str(p) for p in packs}
    clean = [pid for pid in KNOWN_PACK_IDS if pid in wanted]
    put_compliance_packs(tenant_id, clean)
    return _ok({"packs": clean})


# ---------------------------------------------------------------------------
# Playbook — the standard positions documents are graded against
# ---------------------------------------------------------------------------


def _get_playbook(caller: Caller) -> dict[str, Any]:
    """The effective playbook for the caller's workspace: built-in defaults,
    overlaid with any deployment overrides and the workspace's own rules.
    Documents the caller uploads are graded against exactly this."""
    rules = playbook.describe_positions(caller.tenant_id)
    return _ok({
        "rules": rules,
        "customRuleCount": sum(1 for r in rules if r["source"] == "custom"),
        # "default" until the workspace saves a rule of its own.
        "source": "custom" if any(r["source"] == "custom" for r in rules) else "default",
        "appliesTo": "Documents uploaded into your workspace, from their next analysis or re-analysis.",
    })


def _put_playbook_rule(rule_id: str, event: dict[str, Any], caller: Caller) -> dict[str, Any]:
    """Create or replace one rule in the caller's own workspace playbook."""
    body, err = _body(event)
    if err:
        return err
    rule, problem = playbook.validate_rule(rule_id, body)
    if problem:
        return _err(400, problem, "bad_request")
    playbook.save_tenant_rule(caller.tenant_id, rule_id, rule)
    described = next((r for r in playbook.describe_positions(caller.tenant_id) if r["ruleId"] == rule_id), None)
    log.info("api.playbook_rule_saved", ruleId=rule_id)
    return _ok({"rule": described})


def _delete_playbook_rule(rule_id: str, caller: Caller) -> dict[str, Any]:
    """Remove the workspace's own rule. A clause type with a built-in default
    falls back to it; a custom type simply has no rule again."""
    if not playbook.valid_rule_id(rule_id):
        return _err(404, "Rule not found", "not_found")
    playbook.save_tenant_rule(caller.tenant_id, rule_id, None)
    described = next((r for r in playbook.describe_positions(caller.tenant_id) if r["ruleId"] == rule_id), None)
    log.info("api.playbook_rule_deleted", ruleId=rule_id)
    return _ok({"deleted": True, "ruleId": rule_id, "rule": described})


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_INVITABLE_ROLES = ("editor", "viewer")


def _project_view(project: dict[str, Any], role: str, members: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The project shape the frontend already reads, plus the caller's ``role``."""
    return {
        "id":         project.get("projectId"),
        "name":       project.get("name") or "",
        "client":     project.get("client"),
        "createdAt":  project.get("createdAt") or "",
        "updatedAt":  project.get("updatedAt"),
        "docIds":     list(project.get("docIds") or []),
        "ownerEmail": project.get("ownerEmail"),
        "role":       role,
        "members":    [
            {"email": m.get("email"), "role": normalise_role(m.get("role")) if m.get("role") != "owner" else "owner",
             "status": m.get("status") or "invited", "sub": m.get("sub"), "invitedAt": m.get("invitedAt") or ""}
            for m in (members if members is not None else ddb.list_project_members(project["projectId"]))
        ],
    }


def _get_projects(caller: Caller) -> dict[str, Any]:
    roles = caller.project_roles()
    out = []
    for project in ddb.get_projects(list(roles)):
        members = ddb.list_project_members(project["projectId"])
        _activate_membership(project["projectId"], members, caller)
        out.append(_project_view(project, roles[project["projectId"]], members))
    out.sort(key=lambda p: (p["createdAt"], p["id"]))
    return _ok({"projects": out})


def _activate_membership(project_id: str, members: list[dict[str, Any]], caller: Caller) -> None:
    """First time an invited person opens the app: mark their invitation accepted
    (best-effort — access never depended on this flag)."""
    for m in members:
        if caller.email and m.get("email") == caller.email and (m.get("status") != "active" or not m.get("sub")):
            m.update(status="active", sub=caller.sub)
            try:
                ddb.update_project_member(project_id, caller.email, {"status": "active", "sub": caller.sub})
            except Exception as exc:  # noqa: BLE001
                log.warning("api.membership_activate_failed", error_type=type(exc).__name__)


def _get_project(project_id: str, caller: Caller) -> dict[str, Any]:
    project, err = _project_for(project_id, caller, "view")
    if err:
        return err
    return _ok({"project": _project_view(project, project["_role"])})


def _body(event: dict[str, Any]) -> tuple[Any, dict[str, Any] | None]:
    raw_body = event.get("body") or ""
    try:
        return (json.loads(raw_body) if raw_body else {}), None
    except json.JSONDecodeError:
        return None, _err(400, "Invalid JSON body", "bad_request")


def _clean_name(value: Any) -> str:
    return str(value or "").strip()[:200]


def _put_project(project_id: str, event: dict[str, Any], caller: Caller) -> dict[str, Any]:
    """Create a project (the caller becomes its owner — from the token, never the
    body) or, if it exists and the caller owns it, rename it."""
    body, err = _body(event)
    if err:
        return err
    if not isinstance(body, dict):
        return _err(400, "Body must be a JSON object", "bad_request")
    name = _clean_name(body.get("name"))
    client = _clean_name(body.get("client")) or None

    role = caller.project_role(project_id)
    if role is None:
        if not name:
            return _err(400, "A project name is required", "bad_request")
        if len(caller.project_roles()) >= _MAX_PROJECTS:
            return _err(400, f"Too many projects (max {_MAX_PROJECTS})", "bad_request")
        created = _create_project(caller, project_id, name, client)
        if not created:
            # The id belongs to a project this caller cannot see.
            return _err(404, "Project not found", "not_found")
        caller.forget_projects()
        log.info("api.project_created", projectId=project_id)
        return _ok({"project": _project_view(ddb.get_project(project_id) or {"projectId": project_id}, "owner")}, 201)

    if not can(role, "rename_project"):
        return _err(403, _FORBIDDEN, "forbidden")

    def change(p: dict[str, Any]) -> None:
        if name:
            p["name"] = name
        if "client" in body:
            p["client"] = client

    project = ddb.mutate_project(project_id, change)
    if project is None:
        return _err(404, "Project not found", "not_found")
    return _ok({"project": _project_view(project, role)})


def _create_project(caller: Caller, project_id: str, name: str, client: str | None,
                    created_at: str | None = None) -> bool:
    """Create a project owned by the caller — the owner is the token's user, never
    a value from the request. Returns False if the id is already taken."""
    created = ddb.create_project({
        "projectId": project_id, "name": name, "client": client, "createdAt": created_at,
        "ownerSub": caller.sub, "ownerEmail": caller.email, "tenantId": caller.tenant_id,
    })
    if created and caller.email:
        # The owner also appears in the member list (and is found by email).
        ddb.put_project_member(project_id, {
            "email": caller.email, "role": "owner", "status": "active", "sub": caller.sub,
            "invitedAt": now_iso(),
        })
    return created


def _delete_project(project_id: str, caller: Caller) -> dict[str, Any]:
    """Delete the project and its memberships. Its documents are kept — they
    stay with whoever uploaded them and simply stop being shared."""
    project, err = _project_for(project_id, caller, "delete_project")
    if err:
        return err
    doc_ids = ddb.delete_project(project_id)
    for doc_id in doc_ids:
        _unlink_hint(doc_id, project_id)
    caller.forget_projects()
    log.info("api.project_deleted", projectId=project_id, documents=len(doc_ids))
    return _ok({"deleted": True, "projectId": project_id})


def _link_hint(doc_id: str, project_id: str) -> None:
    meta = get_doc_meta(doc_id) or {}
    ddb.set_doc_projects(doc_id, list(meta.get("projectIds") or []) + [project_id])


def _unlink_hint(doc_id: str, project_id: str) -> None:
    meta = get_doc_meta(doc_id)
    if meta:
        ddb.set_doc_projects(doc_id, [p for p in (meta.get("projectIds") or []) if p != project_id])


def _add_project_document(project_id: str, doc_id: str, caller: Caller) -> dict[str, Any]:
    """File a document in a project. Doing so shares it with every member, so the
    caller must own the DOCUMENT (uploaded it, or owns a project it is already
    in) as well as being able to manage the project's documents."""
    project, err = _project_for(project_id, caller, "manage_documents")
    if err:
        return err
    _, err = _doc_for(doc_id, caller, "share_document")
    if err:
        return err
    if len(project.get("docIds") or []) >= _MAX_PROJECT_DOCS and doc_id not in project["docIds"]:
        return _err(400, f"Too many documents in one project (max {_MAX_PROJECT_DOCS})", "bad_request")

    def change(p: dict[str, Any]) -> bool | None:
        ids = list(p.get("docIds") or [])
        if doc_id in ids:
            return False
        p["docIds"] = ids + [doc_id]
        return None

    stored = ddb.mutate_project(project_id, change)
    _link_hint(doc_id, project_id)
    return _ok({"project": _project_view(stored or project, project["_role"])})


def _remove_project_document(project_id: str, doc_id: str, caller: Caller) -> dict[str, Any]:
    project, err = _project_for(project_id, caller, "manage_documents")
    if err:
        return err

    def change(p: dict[str, Any]) -> bool | None:
        ids = list(p.get("docIds") or [])
        if doc_id not in ids:
            return False
        p["docIds"] = [d for d in ids if d != doc_id]
        return None

    stored = ddb.mutate_project(project_id, change)
    _unlink_hint(doc_id, project_id)
    return _ok({"project": _project_view(stored or project, project["_role"])})


def _save_projects(event: dict[str, Any], caller: Caller) -> dict[str, Any]:
    """LEGACY whole-list save (``{"projects": [...]}``), kept so the current
    frontend keeps working. It is a MERGE, not an overwrite:

    * a project in the list that the caller owns  → name / client / docIds updated
    * … that the caller edits                     → docIds updated only
    * … that the caller only views, or cannot see → ignored
    * a project id nobody has used                → created, owned by the caller
    * an owned project missing from the list      → deleted (how this client deletes)
    * ``ownerEmail`` and ``members`` in the body   → always ignored: the owner is
      the token's user, and membership changes only through invite / remove / set-role
    * a document is added to a project only if the caller may share that document
      (they uploaded it, or own a project it is already in)

    So one user's save can never add, alter or delete another user's project.
    """
    raw_body = event.get("body") or ""
    if len(raw_body.encode("utf-8")) > 350_000:
        return _err(413, "Projects payload too large", "too_large")
    body, err = _body(event)
    if err:
        return err
    projects = body.get("projects") if isinstance(body, dict) else None
    if not isinstance(projects, list):
        return _err(400, "Body must be {\"projects\": [...] }", "bad_request")
    if len(projects) > _MAX_PROJECTS:
        return _err(400, f"Too many projects (max {_MAX_PROJECTS})", "bad_request")
    for p in projects:
        if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not _ID_RE.fullmatch(p["id"]):
            return _err(400, "Each project needs an id of letters, digits, '-' or '_' (max 64 characters)", "bad_request")
        doc_ids = p.get("docIds") or []
        if not isinstance(doc_ids, list) or len(doc_ids) > _MAX_PROJECT_DOCS:
            return _err(400, f"docIds must be a list (max {_MAX_PROJECT_DOCS})", "bad_request")

    roles = dict(caller.project_roles())
    ignored: list[str] = []
    doc_roles: dict[str, str | None] = {}

    def may_file(doc_id: str) -> bool:
        if doc_id not in doc_roles:
            doc_roles[doc_id] = caller.document_role(get_doc_meta(doc_id))
        return can(doc_roles[doc_id], "share_document")

    for p in projects:
        pid = p["id"]
        name = _clean_name(p.get("name"))
        client = _clean_name(p.get("client")) or None
        wanted = [d for d in dict.fromkeys(p.get("docIds") or []) if isinstance(d, str) and _ID_RE.fullmatch(d)]
        role = roles.get(pid)
        if role is None:
            created = _create_project(caller, pid, name, client, str(p.get("createdAt") or "")[:40] or None)
            if not created:
                ignored.append(pid)        # someone else's project (or a stale id)
                continue
            role = roles[pid] = "owner"
        if not can(role, "manage_documents"):
            ignored.append(pid)
            continue

        added: list[str] = []
        removed: list[str] = []

        def change(stored: dict[str, Any], _name=name, _client=client, _wanted=wanted, _role=role,
                   _p=p, _added=added, _removed=removed) -> bool | None:
            current = list(stored.get("docIds") or [])
            keep = [d for d in current if d in _wanted]
            new = [d for d in _wanted if d not in current and may_file(d)]
            next_ids = keep + new
            changed = next_ids != current
            if can(_role, "rename_project"):
                if _name and _name != stored.get("name"):
                    stored["name"], changed = _name, True
                if "client" in _p and _client != stored.get("client"):
                    stored["client"], changed = _client, True
            if not changed:
                return False
            _added[:] = new
            _removed[:] = [d for d in current if d not in _wanted]
            stored["docIds"] = next_ids
            return None

        ddb.mutate_project(pid, change)
        for doc_id in added:
            _link_hint(doc_id, pid)
        for doc_id in removed:
            _unlink_hint(doc_id, pid)

    sent = {p["id"] for p in projects}
    for pid, role in roles.items():
        if pid not in sent and can(role, "delete_project"):
            for doc_id in ddb.delete_project(pid):
                _unlink_hint(doc_id, pid)

    caller.forget_projects()
    resp = json.loads(_get_projects(caller)["body"])
    resp["ignored"] = ignored
    return _ok(resp)


# ---------------------------------------------------------------------------
# Project membership
# ---------------------------------------------------------------------------


def _invite_member(project_id: str, event: dict[str, Any], caller: Caller) -> dict[str, Any]:
    """Add someone to a project by email. Only the project's owner may.

    The membership is keyed on the email address, so it takes effect whenever a
    user with that VERIFIED address signs in — whether or not an account exists
    yet, and whatever workspace they are in. It grants access to this project's
    documents only. A Cognito invitation email is sent when the address has no
    account (best-effort: the membership stands even if the email could not be
    sent, because sign-up is open).
    """
    project, err = _project_for(project_id, caller, "invite")
    if err:
        return err
    body, err = _body(event)
    if err:
        return err
    if not isinstance(body, dict):
        return _err(400, "Body must be a JSON object", "bad_request")
    raw_email = str(body.get("email", "")).strip()
    email = normalise_email(raw_email)
    if not email or not _EMAIL_RE.match(email):
        return _err(400, "A valid email address is required", "bad_request")
    requested = str(body.get("role") or "viewer").strip().lower()
    if requested == "owner":
        return _err(400, "A project has one owner; invite as editor or viewer", "bad_request")
    role = normalise_role(requested)

    if email == caller.email or email == (project.get("ownerEmail") or ""):
        return _err(409, "That user is already a member of this project", "already_member")
    if ddb.get_project_member(project_id, email):
        return _err(409, "That user is already a member of this project", "already_member")
    if len(ddb.list_project_members(project_id)) >= _MAX_MEMBERS:
        return _err(400, f"Too many members (max {_MAX_MEMBERS})", "bad_request")

    sub, existing, email_sent = _ensure_cognito_user(email)
    member = {
        "email": email, "role": role, "status": "active" if existing else "invited",
        "sub": sub if existing else None, "invitedAt": now_iso(), "invitedBy": caller.sub,
    }
    ddb.put_project_member(project_id, member)
    log.info("api.member_invited", projectId=project_id, role=role, existingUser=existing, emailSent=email_sent)
    return _ok({
        "member": {k: member[k] for k in ("email", "role", "status", "sub", "invitedAt")},
        "projectId": project_id, "invitationEmailSent": email_sent,
    }, status=201)


def _ensure_cognito_user(email: str) -> tuple[str | None, bool, bool]:
    """(sub, account_already_existed, invitation_email_sent). Never raises: the
    membership does not depend on Cognito, and no tenant attribute is written —
    project access comes from membership alone."""
    pool_id = settings.cognito_user_pool_id
    if not pool_id:
        return None, False, False
    from shared.aws import cognito_idp_client
    try:
        resp = cognito_idp_client().admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        sub = next((a.get("Value") for a in resp.get("User", {}).get("Attributes", [])
                    if a.get("Name") == "sub"), None)
        return sub, False, True
    except Exception as exc:  # noqa: BLE001
        if "UsernameExists" in type(exc).__name__ or "UsernameExistsException" in str(exc):
            return _existing_user_sub(pool_id, email), True, False
        log.warning("api.invite_email_failed", error_type=type(exc).__name__)
        return None, False, False


def _existing_user_sub(pool_id: str, email: str) -> str | None:
    from shared.aws import cognito_idp_client
    try:
        resp = cognito_idp_client().admin_get_user(UserPoolId=pool_id, Username=email)
        attrs = {a.get("Name"): a.get("Value") for a in resp.get("UserAttributes", [])}
        return attrs.get("sub")
    except Exception as exc:  # noqa: BLE001
        log.warning("api.invite_lookup_failed", error_type=type(exc).__name__)
        return None


def _remove_member(project_id: str, email: str, caller: Caller) -> dict[str, Any]:
    """Remove a member. Their access ends immediately — every request re-reads
    membership. Does NOT delete the Cognito user. The owner can remove anyone; a
    member can remove themself; the owner cannot be removed."""
    email = normalise_email(email) or ""
    role = caller.project_role(project_id)
    project = ddb.get_project(project_id) if role else None
    if role is None or project is None:
        return _err(404, "Project not found", "not_found")
    if not (can(role, "remove_member") or (email and email == caller.email)):
        return _err(403, _FORBIDDEN, "forbidden")
    member = ddb.get_project_member(project_id, email) if email else None
    if not member:
        return _err(404, "Member not found on this project", "not_found")
    if member.get("role") == "owner":
        return _err(400, "The project owner cannot be removed", "bad_request")
    ddb.delete_project_member(project_id, email)
    log.info("api.member_removed", projectId=project_id)
    return _ok({"removed": True, "email": email, "projectId": project_id})


def _set_member_role(project_id: str, email: str, event: dict[str, Any], caller: Caller) -> dict[str, Any]:
    project, err = _project_for(project_id, caller, "set_role")
    if err:
        return err
    body, err = _body(event)
    if err:
        return err
    role = str((body or {}).get("role") or "").strip().lower() if isinstance(body, dict) else ""
    if role not in _INVITABLE_ROLES:
        return _err(400, "role must be 'editor' or 'viewer'", "bad_request")
    email = normalise_email(email) or ""
    member = ddb.get_project_member(project_id, email) if email else None
    if not member:
        return _err(404, "Member not found on this project", "not_found")
    if member.get("role") == "owner":
        return _err(400, "The project owner's role cannot be changed", "bad_request")
    member["role"] = role
    if not ddb.update_project_member(project_id, email, {"role": role}):
        return _err(404, "Member not found on this project", "not_found")
    log.info("api.member_role_set", projectId=project_id, role=role)
    return _ok({"member": {k: member.get(k) for k in ("email", "role", "status", "sub", "invitedAt")},
                "projectId": project_id})


# ---------------------------------------------------------------------------
# Similar clauses
# ---------------------------------------------------------------------------


def _get_similar_clauses(doc_id: str, event: dict[str, Any], caller: Caller) -> dict[str, Any]:
    """Top-KNN: clauses most similar to a given clause, across the documents the
    caller may see — the search is restricted to those ids in the query itself.
    Returns [] when the clause isn't embedded yet — never a heuristic guess."""
    _, err = _doc_for(doc_id, caller, "view")
    if err:
        return err

    qs = event.get("queryStringParameters") or {}
    clause = (qs.get("clause") or "").strip()
    if not clause or len(clause) > 64:
        return _err(400, "clause query parameter is required (max 64 characters)", "bad_request")
    try:
        k = max(1, min(10, int(qs.get("k", "5"))))
    except (TypeError, ValueError):
        k = 5

    vector = get_clause_vector(doc_id, clause)
    if not vector:
        return _ok({"similar": []})

    # Pull extra so we can drop the clause itself and repeats and still return k.
    visible = caller.visible_doc_ids()
    hits = knn_search(vector=vector, k=k * 3 + 6, doc_ids=visible)
    titles = {m.get("docId"): m.get("title") for m in ddb.get_docs(
        list({(h.get("_source") or {}).get("docId") for h in hits} - {None}))}

    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for h in hits:
        src = h.get("_source") or {}
        hid, hclause = src.get("docId"), src.get("clauseNumber")
        if hid == doc_id and hclause == clause:
            continue  # the clause itself
        if hid not in visible or (hid, hclause) in seen:
            continue  # belt and braces; and one row per clause, not per chunk
        seen.add((hid, hclause))
        out.append({
            "docId":        hid,
            # null when the document has no title — the UI supplies its own label
            "docTitle":     titles.get(hid) or None,
            "docType":      src.get("docType"),
            "clauseNumber": hclause,
            "category":     src.get("category"),
            "specificType": src.get("specificType"),
            "score":        round(float(h.get("_score", 0)), 4),
            "text":         (src.get("text") or "")[:280],
        })
        if len(out) >= k:
            break
    return _ok({"similar": out})


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def _get_upload_url(event: dict[str, Any], caller: Caller) -> dict[str, Any]:
    qs = event.get("queryStringParameters") or {}
    # Sanitised name of a format the parse stage can extract (shared/uploads.py).
    filename, problem = clean_upload_filename(qs.get("filename"))
    if problem:
        return _err(400, problem, "bad_request")

    doc_type = qs.get("docType", "OTHER").strip().upper()
    if doc_type not in _VALID_DOC_TYPES:
        return _err(400, f"Invalid docType '{doc_type}'. Must be one of: {', '.join(sorted(_VALID_DOC_TYPES))}", "bad_request")

    # Optional: upload straight into a project (needs the upload permission there).
    project_id = (qs.get("projectId") or "").strip() or None
    if project_id:
        if not _ID_RE.fullmatch(project_id):
            return _err(404, "Project not found", "not_found")
        _, err = _project_for(project_id, caller, "upload")
        if err:
            return err

    raw_bucket = os.environ.get("RAW_BUCKET", "")
    if not raw_bucket:
        log.error("api.upload_url.missing_bucket")
        return _err(500, "Server misconfiguration: RAW_BUCKET not set", "internal")

    doc_id = str(uuid.uuid4())
    key    = upload_key(caller.tenant_id, doc_id, filename)

    # ContentType is intentionally NOT signed — the browser can send the real
    # MIME type without breaking the signature (SignedHeaders = "host" only).
    upload_url = s3_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": raw_bucket, "Key": key},
        ExpiresIn=300,
    )

    # Write a PENDING row immediately so the document appears in the UI during
    # processing. The persist stage later fills in the analysis fields. The owner
    # is the token's user — it is what makes the document visible to them.
    try:
        put_doc_meta(pending_document_meta(
            doc_id=doc_id, tenant_id=caller.tenant_id, owner_sub=caller.sub, owner_email=caller.email,
            filename=filename, doc_type=doc_type, project_ids=[project_id] if project_id else [],
        ))
    except Exception:
        # The pipeline only processes uploads that have a META row owned by the
        # key's tenant, so handing out a URL without one would lose the upload.
        log.exception("api.upload_url.meta_write_failed", docId=doc_id)
        return _err(500, "Could not start the upload. Please try again.", "internal")

    if project_id:
        def change(p: dict[str, Any]) -> None:
            p["docIds"] = list(p.get("docIds") or []) + [doc_id]
        ddb.mutate_project(project_id, change)

    log.info("api.upload_url_generated", docId=doc_id, docType=doc_type, intoProject=bool(project_id))
    return _ok({"uploadUrl": upload_url, "key": key, "docId": doc_id, "projectId": project_id})


def _get_document(doc_id: str, caller: Caller) -> dict[str, Any]:
    meta, err = _doc_for(doc_id, caller, "view")
    if err:
        return err

    versions_raw = query_doc_versions(doc_id)
    versions = sorted(
        [
            {
                "versionNumber":     int(v.get("versionNumber", 0)),
                "extractionMethod":  v.get("extractionMethod"),
                "createdAt":         v.get("createdAt", ""),
                "parsedKey":         v.get("parsedKey", ""),
                "classificationKey": v.get("classificationKey", ""),
                "timelineKey":       v.get("timelineKey"),
                "diffKey":           v.get("diffKey"),
                # Extraction report for the version (absent on older versions).
                "extractionCoverage": _conv(v.get("extractionCoverage")),
                "clauseCount":        _conv(v.get("clauseCount")),
                "unclassifiedCount":  _conv(v.get("unclassifiedCount")),
                "keyDateCount":       _conv(v.get("keyDateCount")),
                "pageCount":          _conv(v.get("pageCount")),
            }
            for v in versions_raw
        ],
        key=lambda v: v["versionNumber"],
    )
    return _ok({"document": _clean(meta, caller), "versions": versions})


def _content_type(name: str) -> str:
    n = name.lower()
    if n.endswith(".pdf"):  return "application/pdf"
    if n.endswith(".docx"): return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if n.endswith(".doc"):  return "application/msword"
    if n.endswith(".txt"):  return "text/plain"
    return "application/octet-stream"


def _get_doc_file(doc_id: str, caller: Caller) -> dict[str, Any]:
    """Return a short-lived presigned URL to the original uploaded file so the
    UI can render it (PDF inline, DOCX via client-side conversion)."""
    meta, err = _doc_for(doc_id, caller, "view")
    if err:
        return err
    raw_key = meta.get("rawKey")
    if not raw_key:
        return _err(404, "Original file is not available for this document", "not_found")
    raw_bucket = os.environ.get("RAW_BUCKET", "")
    if not raw_bucket:
        return _err(500, "Server misconfiguration: RAW_BUCKET not set", "internal")
    filename = raw_key.rsplit("/", 1)[-1]
    url = presign_get(raw_bucket, raw_key, expires_seconds=900)
    return _ok({"url": url, "filename": filename, "contentType": _content_type(filename)})


def _reprocess_document(doc_id: str, caller: Caller) -> dict[str, Any]:
    """Re-run the ingestion pipeline on the stored upload.

    Re-writes the raw object in place (MetadataDirective=REPLACE), which fires the
    same S3 'Object Created' EventBridge rule that starts a Step Functions
    execution for a fresh upload — no separate pipeline-invoke permission needed.
    """
    meta, err = _doc_for(doc_id, caller, "reprocess")
    if err:
        return err
    raw_key    = meta.get("rawKey")
    raw_bucket = os.environ.get("RAW_BUCKET", "")
    if not raw_key or not raw_bucket:
        return _err(409, "Original upload is no longer available to re-analyze", "conflict")
    # Each run costs several LLM calls. Refuse to stack a second run on top of
    # one that is still in flight, so the endpoint can't be used to burn spend.
    if _in_flight(meta):
        return _err(409, "Analysis is already in progress for this document", "in_progress")

    try:
        s3_client().copy_object(
            Bucket=raw_bucket,
            Key=raw_key,
            CopySource={"Bucket": raw_bucket, "Key": raw_key},
            MetadataDirective="REPLACE",
            Metadata={"reprocessedat": now_iso()},
            ContentType=_content_type(raw_key),
        )
    except Exception as exc:
        log.exception("api.reprocess_failed", docId=doc_id, error_type=type(exc).__name__)
        return _err(500, "Failed to re-trigger analysis", "internal")

    update_doc_fields(doc_id, {"status": "PENDING"})
    log.info("api.reprocess_triggered", docId=doc_id)
    return _ok({"reprocessing": True, "docId": doc_id})


def _in_flight(meta: dict[str, Any]) -> bool:
    """True while a pipeline run for this document is plausibly still running."""
    if meta.get("status") not in _IN_FLIGHT_STATUSES:
        return False
    try:
        updated = datetime.fromisoformat(str(meta.get("updatedAt", "")).replace("Z", "+00:00"))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - updated < _IN_FLIGHT_GRACE


def _update_document(doc_id: str, caller: Caller, event: dict[str, Any]) -> dict[str, Any]:
    meta, err = _doc_for(doc_id, caller, "edit")
    if err:
        return err

    raw_body = event.get("body") or ""
    if not raw_body:
        return _err(400, "Request body is required", "bad_request")
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body", "bad_request")

    if not isinstance(body, dict):
        return _err(400, "Body must be a JSON object", "bad_request")
    if not body:
        return _err(400, "No fields to update", "bad_request")

    allowed = {"title", "lifecycle", "docType"}
    unknown = set(body.keys()) - allowed
    if unknown:
        return _err(400, f"Unknown field(s): {', '.join(sorted(unknown))}", "bad_request")

    if "lifecycle" in body and body["lifecycle"] not in _VALID_LIFECYCLES:
        return _err(400, f"Invalid lifecycle. Must be one of: {', '.join(sorted(_VALID_LIFECYCLES))}", "bad_request")

    if "docType" in body and body["docType"] not in _VALID_DOC_TYPES:
        return _err(400, f"Invalid docType. Must be one of: {', '.join(sorted(_VALID_DOC_TYPES))}", "bad_request")

    if "title" in body:
        title = str(body["title"]).strip()
        if not title:
            return _err(400, "title cannot be empty", "bad_request")
        if len(title) > 500:
            return _err(400, "title is too long (max 500 characters)", "bad_request")
        body["title"] = title

    # Remember which fields a person set, so a later re-analysis keeps them.
    edited = sorted(set(meta.get("userEdited") or []) | set(body.keys()))
    update_doc_fields(doc_id, {**body, "userEdited": edited})
    updated = get_doc_meta(doc_id)
    log.info("api.document_updated", docId=doc_id, fields=list(body.keys()))
    return _ok({"document": _clean(dict(updated or {}, _role=meta["_role"]), caller)})


def _delete_version(doc_id: str, version_number: int, caller: Caller) -> dict[str, Any]:
    meta, err = _doc_for(doc_id, caller, "delete_document")
    if err:
        return err

    versions = query_doc_versions(doc_id)
    # The version must actually exist. Previously ANY version number (e.g. 999)
    # on a single-version document deleted the whole document.
    if not any(int(v.get("versionNumber", -1)) == version_number for v in versions):
        return _err(404, "Version not found", "not_found")
    if len(versions) <= 1:
        # Removing the only version removes the document — including its files
        # and search vectors, which this path used to leave behind.
        _remove_everywhere(doc_id, meta)
        log.info("api.delete_last_version", docId=doc_id)
        return _ok({
            "deleted":        True,
            "docId":          doc_id,
            "versionDeleted": version_number,
            "message":        "Last version deleted — document removed.",
        })

    new_meta = delete_doc_version(doc_id, version_number)
    log.info("api.version_deleted", docId=doc_id, deletedVersion=version_number,
             rolledBackTo=(new_meta or {}).get("latestVersion"))
    return _ok({
        "deleted":        True,
        "docId":          doc_id,
        "versionDeleted": version_number,
        "latestVersion":  _conv((new_meta or {}).get("latestVersion")),
        "document":       _clean(dict(new_meta, _role=meta["_role"]), caller) if new_meta else None,
    })


def _delete_document(doc_id: str, caller: Caller) -> dict[str, Any]:
    meta, err = _doc_for(doc_id, caller, "delete_document")
    if err:
        return err
    _remove_everywhere(doc_id, meta)
    log.info("api.document_deleted", docId=doc_id)
    return _ok({"deleted": True, "docId": doc_id})


def _remove_everywhere(doc_id: str, meta: dict[str, Any]) -> None:
    """Tear down ALL storage for a document, not just the DynamoDB rows:
      - the original upload in the raw bucket
      - every processed artefact (parsed/classification/diff/timeline JSON)
      - the clause vectors + text in the OpenSearch index
      - its entry in every project that lists it
    Each is best-effort so a single failure can't strip-mine the others or leave
    the document un-deletable; the DynamoDB rows are removed last."""
    _purge_storage(doc_id, meta)
    for project_id in meta.get("projectIds") or []:
        try:
            ddb.mutate_project(
                project_id,
                lambda p: False if doc_id not in (p.get("docIds") or [])
                else p.__setitem__("docIds", [d for d in p["docIds"] if d != doc_id]),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("api.delete.project_unlink_failed", docId=doc_id, error_type=type(exc).__name__)
    delete_doc_entirely(doc_id)


def _purge_storage(doc_id: str, meta: dict[str, Any]) -> None:
    raw_bucket       = os.environ.get("RAW_BUCKET", "")
    processed_bucket = os.environ.get("PROCESSED_BUCKET", "")
    raw_key          = meta.get("rawKey")
    processed_prefix = meta.get("processedPrefix") or f"{meta.get('tenantId', '')}/{doc_id}/"

    from shared.s3 import delete_object, delete_prefix

    if raw_bucket and raw_key:
        try:
            delete_object(raw_bucket, raw_key)
        except Exception as exc:
            log.warning("api.delete.raw_failed", docId=doc_id, error_type=type(exc).__name__)

    if processed_bucket and processed_prefix:
        try:
            delete_prefix(processed_bucket, processed_prefix)
        except Exception as exc:
            log.warning("api.delete.processed_failed", docId=doc_id, error_type=type(exc).__name__)

    try:
        from shared.opensearch import delete_doc as os_delete_doc
        os_delete_doc(doc_id)
    except Exception as exc:
        log.warning("api.delete.opensearch_failed", docId=doc_id, error_type=type(exc).__name__)


def _latest_artefact(doc_id: str, key_field: str, missing: str) -> tuple[Any, dict[str, Any] | None]:
    versions = query_doc_versions(doc_id)
    if not versions:
        return None, _err(404, "No processed versions found", "not_ready")
    latest = max(versions, key=lambda v: int(v.get("versionNumber", 0)))
    key = latest.get(key_field)
    if not key:
        return None, _err(404, missing, "not_available")
    processed_bucket = os.environ.get("PROCESSED_BUCKET", "")
    if not processed_bucket:
        return None, _err(500, "Server misconfiguration: PROCESSED_BUCKET not set", "internal")
    try:
        from shared.s3 import get_json
        return get_json(processed_bucket, key), None
    except Exception as exc:
        log.warning("api.artefact_read_failed", docId=doc_id, artefact=key_field, error_type=type(exc).__name__)
        return None, _err(404, "The analysis data was not found in storage", "not_available")


def _get_doc_classification(doc_id: str, caller: Caller) -> dict[str, Any]:
    _, err = _doc_for(doc_id, caller, "view")
    if err:
        return err
    data, err = _latest_artefact(doc_id, "classificationKey", "Classification not yet available")
    return err or _ok(data)


def _get_doc_diff(doc_id: str, caller: Caller) -> dict[str, Any]:
    meta, err = _doc_for(doc_id, caller, "view")
    if err:
        return err
    # A diff quotes the PARENT document's clause text ("before"). Only show it to
    # someone who may read the parent too.
    parent_id = meta.get("parentDocId")
    if parent_id and caller.document_role(get_doc_meta(parent_id)) is None:
        return _err(404, "Diff not available — the parent document is not shared with you", "not_available")
    data, err = _latest_artefact(
        doc_id, "diffKey", "Diff not available — this is the first version or no parent was found")
    return err or _ok(data)


def _get_doc_timeline(doc_id: str, caller: Caller) -> dict[str, Any]:
    meta, err = _doc_for(doc_id, caller, "view")
    if err:
        return err
    data, err = _latest_artefact(doc_id, "timelineKey", "Timeline not yet available")
    if err:
        return err
    return _ok(_restrict_timeline(data, doc_id, caller))


def _restrict_timeline(data: dict[str, Any], doc_id: str, caller: Caller) -> dict[str, Any]:
    """A timeline replays a whole amendment chain, so it can carry clause text
    from documents other than the one requested. If any document in the chain is
    not visible to the caller, return only what belongs to documents they can
    see: the chain entries they may read and this document's own key dates —
    with the clause states withheld and ``restricted: true``."""
    if not isinstance(data, dict):
        return data
    chain = data.get("amendmentChain") or []
    other_ids = [c.get("docId") for c in chain if c.get("docId") and c.get("docId") != doc_id]
    parent_id = (get_doc_meta(doc_id) or {}).get("parentDocId")
    if parent_id:
        other_ids.append(parent_id)
    other_ids = list(dict.fromkeys(other_ids))
    if not other_ids:
        return data
    metas = {m.get("docId"): m for m in ddb.get_docs(other_ids)}
    hidden = {d for d in other_ids if caller.document_role(metas.get(d)) is None}
    if not hidden:
        return data
    return {
        "initialState": {}, "currentState": {}, "futureState": None,
        "amendmentChain": [c for c in chain if c.get("docId") not in hidden],
        "keyDates": data.get("keyDates") or [],
        "restricted": True,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant(event: dict[str, Any]) -> str:
    """Tenant (storage workspace) from VERIFIED JWT claims only."""
    return Caller.from_event(event).tenant_id


def _clean(item: dict[str, Any] | None, caller: Caller | None = None) -> dict[str, Any]:
    if not item:
        return {}
    skip = {"PK", "SK", "GSI1PK", "GSI1SK", "entityType", "_role"}
    out = {k: _conv(v) for k, v in item.items() if k not in skip}
    if out.get("errorMessage"):
        out["errorMessage"] = _public_error(out["errorMessage"])
    if item.get("_role"):
        out["role"] = item["_role"]
    if caller is not None and "projectIds" in out:
        # Only the projects THIS caller can see — not every project the document is in.
        visible = caller.project_roles()
        out["projectIds"] = [p for p in out.get("projectIds") or [] if p in visible]
    return out


def _public_error(raw: Any) -> str:
    """Reduce a stored pipeline failure to a short message safe to show a user.

    Step Functions stores "<Error>: <Cause>", where Cause is the Lambda error
    JSON including a full stack trace (file paths, internals). Keep only the
    exception message — which the pipeline writes for the user (shared/errors.py).
    """
    text = str(raw)
    start = text.find("{")
    if start != -1:
        try:
            cause = json.loads(text[start:])
            if isinstance(cause, dict) and cause.get("errorMessage"):
                text = str(cause["errorMessage"])
        except ValueError:
            pass
    return text[:300]


def _conv(val: Any) -> Any:
    if isinstance(val, Decimal):
        return int(val) if val == val.to_integral_value() else float(val)
    if isinstance(val, dict):
        return {k: _conv(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_conv(v) for v in val]
    if isinstance(val, (set, frozenset)):
        return sorted(_conv(v) for v in val)
    return val


def _ok(body: Any, status: int = 200) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps(body, default=str),
    }


def _err(status: int, message: str, code: str | None = None) -> dict[str, Any]:
    """Error body: ``{"error": <message>, "code": <machine-readable reason>}``."""
    body: dict[str, Any] = {"error": message}
    if code:
        body["code"] = code
    return _ok(body, status=status)
