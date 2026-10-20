"""The organization: who created a tenant and who administers it.

THE RULE
--------
An organization is a tenant (shared/auth.py). Whoever creates it is its admin,
and admin rights are per tenant: a person is an admin of a tenant exactly when
they are its creator or listed in that tenant's ``admins``. Nothing else grants
admin — not the Cognito ``govern-admin`` group (pool-wide, so it would make a
person admin of every tenant they can reach) and not ``GOVERN_OPEN_ADMIN``.

* Personal workspace ``u-<sub>``: only that user can be in it, so they are its
  creator and only admin. Their ORG record is written on their first
  GET /govern/me; their admin right does not depend on it existing.
* Shared tenant (``custom:tenantId``): the first member to open Govern
  (GET /govern/me) creates the ORG record with a conditional put, so exactly one
  person is the creator even when several members sign in at once. Later
  members are not admins until an admin adds them. Provisioning a new shared
  tenant should therefore have its intended owner sign in first — that is the
  creation path; every claim is logged (``govern_org.created``).

STORAGE (govern-config, one partition per tenant — store.ConfigRepository)
--------------------------------------------------------------------------
  T#<tenant>#ORG / ORG              {tenantId, createdBy, createdAt, admins[], rev}
  T#<tenant>#ORG / MEMBER#<sub>     a person who signed in to Govern in this tenant
  T#<tenant>#ORG / AUDIT#<iso>#<id> admin added / removed (append-only)

Only MEMBER rows can become admins: they were written from a verified JWT whose
tenant is this one, so an email from another tenant is never accepted.
"""
from __future__ import annotations

from typing import Any

from ..logger import get_logger
from . import store
from .store import iso

log = get_logger("blue-iq.govern.org")

_MAX_WRITE_RETRIES = 5
_MAX_ADMINS = 50


class OrgError(Exception):
    status = 400
    code = "bad_request"


class OrgNotFound(OrgError):
    status = 404
    code = "not_found"


class OrgForbidden(OrgError):
    status = 403
    code = "forbidden"


class OrgConflict(OrgError):
    status = 409
    code = "conflict"


def is_personal(tenant_id: str) -> bool:
    return tenant_id.startswith("u-")


def _person(sub: str, email: str | None, name: str | None) -> dict[str, Any]:
    return {"sub": sub, "email": email, "name": name}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def get(tenant_id: str) -> dict[str, Any] | None:
    return store.config.org(tenant_id)


def creator_sub(org: dict[str, Any] | None) -> str | None:
    return ((org or {}).get("createdBy") or {}).get("sub")


def admin_subs(org: dict[str, Any] | None) -> set[str]:
    subs = {a.get("sub") for a in (org or {}).get("admins") or [] if a.get("sub")}
    if creator_sub(org):
        subs.add(creator_sub(org))
    return subs


def is_admin(org: dict[str, Any] | None, tenant_id: str, sub: str) -> bool:
    """Is ``sub`` an admin of ``tenant_id``? ``org`` is that tenant's record
    (None if it has none yet)."""
    if is_personal(tenant_id):
        return tenant_id == f"u-{sub}"
    if org is None or org.get("tenantId") not in (None, tenant_id):
        return False
    return sub in admin_subs(org)


def is_creator(org: dict[str, Any] | None, tenant_id: str, sub: str) -> bool:
    if is_personal(tenant_id):
        return tenant_id == f"u-{sub}"
    return bool(org) and creator_sub(org) == sub


def is_tenant_admin(tenant_id: str, sub: str) -> bool:
    """One read: may ``sub`` change tenant-wide settings of ``tenant_id``?
    Fails closed when the record cannot be read."""
    if is_personal(tenant_id):
        return tenant_id == f"u-{sub}"
    try:
        return is_admin(get(tenant_id), tenant_id, sub)
    except Exception as exc:  # noqa: BLE001 - no record, no table: not an admin
        log.warning("govern_org.admin_check_failed", tenantId=tenant_id, error_type=type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# Creating (first sign-in) and members
# ---------------------------------------------------------------------------


def ensure(tenant_id: str, sub: str, email: str | None, name: str | None) -> dict[str, Any] | None:
    """The tenant's ORG record, created with this caller as creator and admin
    when it has none. A personal workspace is created only by its own user.
    Conditional put: under a race exactly one caller becomes the creator; the
    others read the winner's record."""
    org = get(tenant_id)
    if org is not None:
        return org
    if is_personal(tenant_id) and tenant_id != f"u-{sub}":
        return None
    now = iso()
    me = _person(sub, email, name)
    record = {"tenantId": tenant_id, "createdBy": me, "createdAt": now,
              "admins": [{**me, "addedBy": me, "addedAt": now}]}
    if store.config.create_org(tenant_id, record):
        log.info("govern_org.created", tenantId=tenant_id, creatorSub=sub, personal=is_personal(tenant_id),
                 claim="personal" if is_personal(tenant_id) else "first_member")
        store.config.append_org_audit(tenant_id, {"action": "org_created", "at": now, "actor": me, "target": me})
        return {**record, "rev": 1}
    log.info("govern_org.creation_lost_race", tenantId=tenant_id, sub=sub)
    return get(tenant_id)


def record_member(tenant_id: str, sub: str, email: str | None, name: str | None) -> None:
    """Remember that ``sub`` signed in to Govern in this tenant (so an admin
    can find them by email). Written only when new or changed."""
    current = store.config.org_member(tenant_id, sub)
    if current and current.get("email") == email and current.get("name") == name:
        return
    now = iso()
    store.config.put_org_member(tenant_id, {"sub": sub, "email": email, "name": name,
                                      "firstSeenAt": (current or {}).get("firstSeenAt") or now, "lastSeenAt": now})


def members(tenant_id: str) -> list[dict[str, Any]]:
    out = [{"sub": m.get("sub"), "email": m.get("email"), "name": m.get("name")}
           for m in store.config.org_members(tenant_id) if m.get("sub")]
    return sorted(out, key=lambda m: ((m["name"] or m["email"] or "").lower(), m["sub"]))


# ---------------------------------------------------------------------------
# Admin changes (the caller is already checked to be an admin of the tenant)
# ---------------------------------------------------------------------------


def _check_rev(org: dict[str, Any], rev: Any) -> None:
    if rev is None:
        return
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise OrgError("rev must be a whole number")
    if int(org.get("rev") or 0) != rev:
        raise OrgConflict("Someone else changed the admins at the same time. Reload and try again.")


def _mutate(tenant_id: str, rev: Any, change: Any) -> dict[str, Any]:
    """Read-modify-write the ORG record under optimistic locking. ``change``
    validates against the record it is given and returns the new one, or None
    for no change. A client ``rev`` that is stale is a 409."""
    for attempt in range(_MAX_WRITE_RETRIES):
        org = get(tenant_id)
        if org is None:
            raise OrgNotFound("This organization has not been set up yet")
        if attempt == 0:
            _check_rev(org, rev)
        updated = change(org)
        if updated is None:
            return org
        current_rev = int(org.get("rev") or 0)
        if store.config.put_org(tenant_id, updated, current_rev):
            return {**updated, "rev": current_rev + 1}
    raise OrgConflict("Someone else changed the admins at the same time. Reload and try again.")


def add_admin(tenant_id: str, actor: dict[str, Any], email: Any, rev: Any = None) -> dict[str, Any]:
    from ..access import normalise_email

    if is_personal(tenant_id):
        raise OrgError("A personal workspace has one admin: the person it belongs to.")
    address = normalise_email(email)
    if not address:
        raise OrgError("email must be an email address")
    member = next((m for m in store.config.org_members(tenant_id) if (m.get("email") or "").lower() == address), None)
    if member is None or not member.get("sub"):
        raise OrgNotFound("No one with that email has signed in to this organization yet.")
    target = _person(member["sub"], member.get("email"), member.get("name"))

    added = {"now": False}

    def change(org: dict[str, Any]) -> dict[str, Any] | None:
        added["now"] = False
        if target["sub"] in admin_subs(org):
            return None
        admins = list(org.get("admins") or [])
        if len(admins) >= _MAX_ADMINS:
            raise OrgError(f"An organization can have at most {_MAX_ADMINS} admins")
        admins.append({**target, "addedBy": actor, "addedAt": iso()})
        added["now"] = True
        return {**org, "admins": admins}

    org = _mutate(tenant_id, rev, change)
    if added["now"]:
        store.config.append_org_audit(tenant_id, {"action": "admin_added", "at": iso(), "actor": actor, "target": target})
        log.info("govern_org.admin_added", tenantId=tenant_id, actorSub=actor.get("sub"), targetSub=target["sub"])
    return org


def remove_admin(tenant_id: str, actor: dict[str, Any], sub: str, rev: Any = None) -> dict[str, Any]:
    removed: dict[str, Any] = {}

    def change(org: dict[str, Any]) -> dict[str, Any] | None:
        if sub not in admin_subs(org):
            raise OrgNotFound("That person is not an admin of this organization")
        if sub == creator_sub(org):
            raise OrgConflict("The person who created the organization is always an admin.")
        if len(admin_subs(org)) <= 1:
            raise OrgConflict("An organization needs at least one admin.")
        keep = []
        for a in org.get("admins") or []:
            if a.get("sub") == sub:
                removed.update(_person(sub, a.get("email"), a.get("name")))
            else:
                keep.append(a)
        return {**org, "admins": keep}

    if is_personal(tenant_id):
        raise OrgConflict("A personal workspace has one admin: the person it belongs to.")
    org = _mutate(tenant_id, rev, change)
    store.config.append_org_audit(tenant_id, {"action": "admin_removed", "at": iso(), "actor": actor,
                                        "target": removed or _person(sub, None, None)})
    log.info("govern_org.admin_removed", tenantId=tenant_id, actorSub=actor.get("sub"), targetSub=sub)
    return org


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def view(org: dict[str, Any] | None, tenant_id: str, sub: str) -> dict[str, Any]:
    """GET /govern/org without the audit trail and member list."""
    creator = (org or {}).get("createdBy")
    admins = []
    for a in (org or {}).get("admins") or []:
        admins.append({"sub": a.get("sub"), "email": a.get("email"), "name": a.get("name"),
                       "addedAt": a.get("addedAt"), "isCreator": a.get("sub") == creator_sub(org),
                       "addedBy": {k: (a.get("addedBy") or {}).get(k) for k in ("email", "name")}})
    if creator and creator.get("sub") not in {a["sub"] for a in admins}:
        admins.insert(0, {"sub": creator.get("sub"), "email": creator.get("email"), "name": creator.get("name"),
                          "addedAt": (org or {}).get("createdAt"), "isCreator": True, "addedBy": None})
    admins.sort(key=lambda a: (not a["isCreator"], a.get("addedAt") or ""))
    return {"tenantId": tenant_id, "personal": is_personal(tenant_id),
            "createdBy": {k: (creator or {}).get(k) for k in ("sub", "email", "name")} if creator else None,
            "createdAt": (org or {}).get("createdAt"), "admins": admins, "rev": int((org or {}).get("rev") or 0),
            "you": {"isAdmin": is_admin(org, tenant_id, sub), "isCreator": is_creator(org, tenant_id, sub)}}


def audit_view(entry: dict[str, Any]) -> dict[str, Any]:
    pick = lambda p: {k: (p or {}).get(k) for k in ("email", "name")}  # noqa: E731
    return {"action": entry.get("action"), "at": entry.get("at"), "actor": pick(entry.get("actor")),
            "target": pick(entry.get("target"))}
