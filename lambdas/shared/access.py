"""Access control — who may see and change which project and document.

THE RULE
--------
Nobody shares a workspace by default. A signed-in user can reach

* a PROJECT only if they own it or are a member of it, and
* a DOCUMENT only if they uploaded it, or it is filed in a project they can see.

Everything else does not exist as far as that user is concerned (the API
answers 404, never 403, so an id cannot be probed). Identity is always the
verified JWT: ``sub`` for ownership, and the ``email`` claim — only when
``email_verified`` is true — for membership. Nothing typed by the client (a
header, a body field, an ``ownerEmail``) is ever treated as identity.

THE PERMISSION MATRIX (the single definition — the API and the tests read it)
----------------------------------------------------------------------------
                         owner   editor   viewer
  view                     ✓       ✓        ✓      read project, documents, analysis, chat
  upload                   ✓       ✓        –      upload a document into the project
  edit                     ✓       ✓        –      edit document title / type / lifecycle
  reprocess                ✓       ✓        –      re-run the analysis
  manage_documents         ✓       ✓        –      add / remove documents in the project
  share_document           ✓       –        –      file a document into (another) project
  delete_document          ✓       –        –      delete a document or one of its versions
  rename_project           ✓       –        –
  delete_project           ✓       –        –
  invite                   ✓       –        –      invite a member
  remove_member            ✓       –        –      remove a member
  set_role                 ✓       –        –      change a member's role

For a document, the caller's role is the strongest of: ``owner`` if they
uploaded it, else their role in any project that currently lists it. Filing a
document into a project shares it with that project's members, so it needs
``share_document`` on the DOCUMENT (its uploader, or the owner of a project it
is already in) as well as ``manage_documents`` on the target project — an
editor cannot take a document they were merely given access to and share it on.

HOW IT IS STORED (single DynamoDB table, no scans — see docs/ARCHITECTURE.md)
---------------------------------------------------------------------------
  PROJ#<id> / META                 the project (name, owner, docIds)
  PROJ#<id> / OWNER                owner pointer   GSI1: USER#<sub>      → PROJ#<id>
  PROJ#<id> / MEMBER#<email>       a membership    GSI1: MEMBER#<email>  → PROJ#<id>
  DOC#<id>  / META                 + ownerSub, ownerEmail, projectIds (hint)

"Which projects can I see" is two key lookups on GSI1 (by sub, by verified
email). A project's ``docIds`` is the source of truth for what it contains; a
document's ``projectIds`` only says where to look and is re-checked against the
project on every access, so a stale hint can never grant access.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import dynamodb
from .auth import AuthError, jwt_claims, tenant_for_identity

ROLES = ("owner", "editor", "viewer")
_RANK = {"viewer": 1, "editor": 2, "owner": 3}

CAPABILITIES = (
    "view", "upload", "edit", "reprocess", "manage_documents", "share_document", "delete_document",
    "rename_project", "delete_project", "invite", "remove_member", "set_role",
)

PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset(CAPABILITIES),
    "editor": frozenset({"view", "upload", "edit", "reprocess", "manage_documents"}),
    "viewer": frozenset({"view"}),
}


def normalise_role(role: Any) -> str:
    """Stored / requested role → one of ROLES. The legacy "member" role and
    anything unrecognised get the least privilege (viewer)."""
    value = str(role or "").strip().lower()
    return value if value in ROLES else "viewer"


def can(role: str | None, capability: str) -> bool:
    return bool(role) and capability in PERMISSIONS.get(role, frozenset())


def stronger(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b:
        return a
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def normalise_email(value: Any) -> str | None:
    email = str(value or "").strip().lower()
    return email if email and "@" in email and len(email) <= 200 and " " not in email else None


@dataclass
class Caller:
    """The verified identity behind a request."""

    sub: str
    tenant_id: str
    email: str | None = None          # lower-cased, only when verified
    _projects: dict[str, str] | None = field(default=None, repr=False)

    # -- construction -------------------------------------------------------
    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> "Caller":
        tenant_id = tenant_for_identity(claims.get("custom:tenantId"), claims.get("sub"))
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise AuthError("no verified identity on the request")
        verified = claims.get("email_verified")
        is_verified = verified is True or str(verified).strip().lower() == "true"
        return cls(sub=sub, tenant_id=tenant_id,
                   email=normalise_email(claims.get("email")) if is_verified else None)

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> "Caller":
        """Caller for an API Gateway (HTTP API, JWT authorizer) request."""
        return cls.from_claims(jwt_claims(event))

    # -- projects -----------------------------------------------------------
    def project_roles(self) -> dict[str, str]:
        """{projectId: role} for every project this caller owns or belongs to.
        Two key lookups, cached for the life of the request."""
        if self._projects is None:
            self._projects = dynamodb.project_roles_for(self.sub, self.email)
        return self._projects

    def project_role(self, project_id: str) -> str | None:
        return self.project_roles().get(project_id)

    def forget_projects(self) -> None:
        self._projects = None

    # -- documents ----------------------------------------------------------
    def owns(self, meta: dict[str, Any]) -> bool:
        """Did this caller upload the document? A document uploaded into a
        private ``u-<sub>`` workspace before owners were recorded belongs to
        that user by construction."""
        owner = meta.get("ownerSub")
        if owner:
            return owner == self.sub
        return meta.get("tenantId") == f"u-{self.sub}"

    def document_role(self, meta: dict[str, Any] | None) -> str | None:
        """Strongest role this caller holds on a document, or None (= 404)."""
        if not meta:
            return None
        if self.owns(meta):
            return "owner"
        doc_id = meta.get("docId")
        roles = self.project_roles()
        best: str | None = None
        if not roles or not doc_id:
            return None
        # The document's hint says where to look first; the project record
        # decides. If the hint is missing (it is best-effort bookkeeping), every
        # project the caller belongs to is checked, so a lost hint cannot hide a
        # document that list and search would show.
        hinted = [pid for pid in (meta.get("projectIds") or []) if pid in roles]
        for project in dynamodb.get_projects(hinted or list(roles)):
            if doc_id in (project.get("docIds") or []):
                best = stronger(best, roles.get(project["projectId"]))
        return best

    def visible_documents(self) -> dict[str, dict[str, Any]]:
        """{docId: META row + ``_role``} for every document this caller may
        read: their own uploads (owner) plus the contents of every project they
        belong to (their strongest role there). One tenant listing, one
        projects batch read and one documents batch read — no per-document
        lookups."""
        docs: dict[str, dict[str, Any]] = {}
        for d in dynamodb.list_tenant_docs(self.tenant_id):
            if d.get("docId") and self.owns(d):
                docs[d["docId"]] = dict(d, _role="owner")
        roles = self.project_roles()
        if roles:
            wanted: dict[str, str] = {}
            for project in dynamodb.get_projects(list(roles)):
                role = roles[project["projectId"]]
                for doc_id in project.get("docIds") or []:
                    if doc_id not in docs and stronger(wanted.get(doc_id), role) == role:
                        wanted[doc_id] = role
            for meta in dynamodb.get_docs(list(wanted)):
                doc_id = meta.get("docId")
                if doc_id:
                    docs[doc_id] = dict(meta, _role=wanted[doc_id])
        return docs

    def visible_doc_ids(self) -> list[str]:
        """Every document id this caller may read: their own uploads plus the
        contents of every project they can see. Used to scope search."""
        ids = [d["docId"] for d in dynamodb.list_tenant_docs(self.tenant_id)
               if d.get("docId") and self.owns(d)]
        roles = self.project_roles()
        if roles:
            for project in dynamodb.get_projects(list(roles)):
                ids.extend(project.get("docIds") or [])
        return list(dict.fromkeys(ids))


# ---------------------------------------------------------------------------
# Workspace-wide settings
# ---------------------------------------------------------------------------

WORKSPACE_ADMIN_GROUP = "govern-admin"


def claim_groups(raw: Any) -> set[str]:
    """``cognito:groups`` arrives as a list, or (HTTP API JWT authorizer) as a
    string like ``"[govern-admin reviewers]"`` or ``"a,b"``."""
    import re

    if isinstance(raw, list):
        return {str(g).strip() for g in raw if str(g).strip()}
    if isinstance(raw, str):
        return {g for g in re.split(r"[\s,\[\]\"']+", raw) if g}
    return set()


def workspace_admin(claims: dict[str, Any], tenant_id: str) -> bool:
    """May this caller change settings every document in the tenant is graded
    against (playbook rules, compliance packs)? A personal ``u-<sub>``
    workspace belongs to its one user; a shared tenant needs the admin group
    (or the dev-only open-admin sandbox)."""
    from .config import settings

    if tenant_id.startswith("u-"):
        return True
    if WORKSPACE_ADMIN_GROUP in claim_groups(claims.get("cognito:groups")):
        return True
    return bool(settings.govern_open_admin)
