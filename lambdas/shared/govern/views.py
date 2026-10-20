"""Saved list views (docs/GOVERN_API.md "Saved views") — a person's filters,
columns, grouping and sort for the contracts or obligations list, kept for
themselves, shared with everyone in the workspace, or with named people.

A view is a display preference, never a change to a contract, so every Govern
user (leaders too) may save them. Only the person who created a view may edit
or delete it; a view the caller may not see does not exist for them (404).

Stored in govern-config ``T#<tenant>#VIEWS / V#<module>#<id>`` (store.config);
the ``config`` object is kept as compact JSON so it reads back exactly as it
was validated.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any

from . import store
from .store import iso
from .workflow import BadRequest, NotFound, _EMAIL_RE

MODULES = ("contracts", "obligations")
VISIBILITIES = ("private", "everyone", "specific")
DENSITIES = ("comfortable", "compact")
MAX_VIEWS_PER_MODULE = 100          # per creator
MAX_NAME = 80
MAX_SHARED_WITH = 50
MAX_RULES = 25
MAX_COLUMNS = 40
MAX_VALUE_LIST = 50
MAX_VALUE_TEXT = 200
MAX_CONFIG_BYTES = 16_384

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_VIEW_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CREATE_KEYS = frozenset({"module", "name", "visibility", "sharedWith", "config"})
_PATCH_KEYS = frozenset({"name", "visibility", "sharedWith", "config"})
_CONFIG_KEYS = frozenset({"base", "filters", "columns", "groupBy", "sort", "density"})
_NOT_FOUND = "View not found"


class LimitReached(BadRequest):
    code = "limit"


class _Forbidden(BadRequest):
    status = 403
    code = "forbidden"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def clean_module(value: Any) -> str:
    if value not in MODULES:
        raise BadRequest(f"module must be one of: {', '.join(MODULES)}")
    return value


def _unknown(obj: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    extra = set(obj) - allowed
    if extra:
        raise BadRequest(f"Unknown field(s) in {where}: {', '.join(sorted(map(str, extra)))}")


def _token(value: Any, where: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _TOKEN_RE.match(value):
        raise BadRequest(f"{where} must be up to 64 letters, digits or _ . : -" + (", or null" if nullable else ""))
    return value


def _scalar(value: Any, where: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise BadRequest(f"{where} must be a finite number")
        return value
    if isinstance(value, str):
        if len(value) > MAX_VALUE_TEXT:
            raise BadRequest(f"{where} must be at most {MAX_VALUE_TEXT} characters")
        return value
    raise BadRequest(f"{where} must be a string, number, boolean, null or a list of those")


def _rule_value(value: Any, where: str) -> Any:
    if isinstance(value, list):
        if len(value) > MAX_VALUE_LIST:
            raise BadRequest(f"{where} may list at most {MAX_VALUE_LIST} values")
        return [_scalar(v, f"{where}[{i}]") for i, v in enumerate(value)]
    return _scalar(value, where)


def _columns(raw: Any) -> dict[str, list[str]]:
    if raw is None:
        return {"order": [], "hidden": []}
    if not isinstance(raw, dict):
        raise BadRequest("config.columns must be an object {order, hidden}")
    _unknown(raw, frozenset({"order", "hidden"}), "config.columns")
    out: dict[str, list[str]] = {}
    for key in ("order", "hidden"):
        cols = raw.get(key, [])
        if not isinstance(cols, list) or len(cols) > MAX_COLUMNS:
            raise BadRequest(f"config.columns.{key} must be a list (max {MAX_COLUMNS})")
        out[key] = [_token(c, f"config.columns.{key}[{i}]") for i, c in enumerate(cols)]
    return out


def _filters(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"match": "all", "rules": []}
    if not isinstance(raw, dict):
        raise BadRequest("config.filters must be an object {match, rules}")
    _unknown(raw, frozenset({"match", "rules"}), "config.filters")
    match = raw.get("match", "all")
    if match not in ("all", "any"):
        raise BadRequest("config.filters.match must be all or any")
    rules = raw.get("rules", [])
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise BadRequest(f"config.filters.rules must be a list (max {MAX_RULES})")
    clean = []
    for i, rule in enumerate(rules):
        where = f"config.filters.rules[{i}]"
        if not isinstance(rule, dict):
            raise BadRequest(f"{where} must be an object {{field, op, value}}")
        _unknown(rule, frozenset({"field", "op", "value"}), where)
        clean.append({"field": _token(rule.get("field"), f"{where}.field"),
                      "op": _token(rule.get("op"), f"{where}.op"),
                      "value": _rule_value(rule.get("value"), f"{where}.value")})
    return {"match": match, "rules": clean}


def _sort(raw: Any) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BadRequest("config.sort must be an object {key, dir} or null")
    _unknown(raw, frozenset({"key", "dir"}), "config.sort")
    if raw.get("dir") not in ("asc", "desc"):
        raise BadRequest("config.sort.dir must be asc or desc")
    return {"key": _token(raw.get("key"), "config.sort.key"), "dir": raw["dir"]}


def clean_config(raw: Any) -> dict[str, Any]:
    """Validate a view's ``config``; every key is optional and comes back
    filled in, so a stored config always has the full shape."""
    if not isinstance(raw, dict):
        raise BadRequest("config must be an object")
    try:
        size = len(json.dumps(raw, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except ValueError:
        raise BadRequest("config may not contain NaN or infinite numbers")
    if size > MAX_CONFIG_BYTES:
        raise BadRequest(f"config is too large (max {MAX_CONFIG_BYTES // 1024} KB)")
    _unknown(raw, _CONFIG_KEYS, "config")
    density = raw.get("density")
    if density is not None and density not in DENSITIES:
        raise BadRequest(f"config.density must be one of: {', '.join(DENSITIES)}, or null")
    return {"base": _token(raw.get("base"), "config.base", nullable=True),
            "filters": _filters(raw.get("filters")),
            "columns": _columns(raw.get("columns")),
            "groupBy": _token(raw.get("groupBy"), "config.groupBy", nullable=True),
            "sort": _sort(raw.get("sort")),
            "density": density}


def _name(raw: Any) -> str:
    name = raw.strip() if isinstance(raw, str) else ""
    if not name or len(name) > MAX_NAME:
        raise BadRequest(f"name must be 1 to {MAX_NAME} characters")
    return name


def _visibility(raw: Any) -> str:
    if raw not in VISIBILITIES:
        raise BadRequest(f"visibility must be one of: {', '.join(VISIBILITIES)}")
    return raw


def _shared_with(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_SHARED_WITH:
        raise BadRequest(f"sharedWith must be a list of email addresses (max {MAX_SHARED_WITH})")
    out = []
    for i, value in enumerate(raw):
        email = value.strip().lower() if isinstance(value, str) else ""
        if not _EMAIL_RE.match(email) or len(email) > 200:
            raise BadRequest(f"sharedWith[{i}] must be a valid email address")
        out.append(email)
    return list(dict.fromkeys(out))


def _audience(visibility: str, shared_with: list[str]) -> list[str]:
    """``sharedWith`` as stored: required for "specific", empty otherwise."""
    if visibility != "specific":
        return []
    if not shared_with:
        raise BadRequest("sharedWith must list at least one email address when visibility is specific")
    return shared_with


# ---------------------------------------------------------------------------
# Access and rendering
# ---------------------------------------------------------------------------


def _own(view: dict[str, Any], sub: str) -> bool:
    return bool(sub) and view.get("createdBySub") == sub


def _visible(view: dict[str, Any], sub: str, email: str | None) -> bool:
    if _own(view, sub):
        return True
    if view.get("visibility") == "everyone":
        return True
    return view.get("visibility") == "specific" and bool(email) and email in (view.get("sharedWith") or [])


def to_api(view: dict[str, Any], sub: str) -> dict[str, Any]:
    created_by = view.get("createdBy") or {}
    return {"id": view["id"], "module": view["module"], "name": view.get("name") or "",
            "visibility": view.get("visibility") or "private", "sharedWith": list(view.get("sharedWith") or []),
            "createdBy": {"email": created_by.get("email"), "name": created_by.get("name")},
            "createdAt": view.get("createdAt"), "updatedAt": view.get("updatedAt"),
            "canEdit": _own(view, sub), "config": json.loads(view.get("configJson") or "{}")}


def _find(tenant_id: str, view_id: str) -> dict[str, Any] | None:
    """A view by id alone (the id is unique; the module is part of the key)."""
    if not isinstance(view_id, str) or not _VIEW_ID_RE.match(view_id):
        return None
    for module in MODULES:
        view = store.config.view(tenant_id, module, view_id)
        if view:
            return view
    return None


def _editable(tenant_id: str, view_id: str, sub: str, email: str | None) -> dict[str, Any]:
    """The view if the caller created it; 404 if they cannot see it, 403 if
    they can see it but did not create it."""
    view = _find(tenant_id, view_id)
    if view is None or not _visible(view, sub, email):
        raise NotFound(_NOT_FOUND)
    if not _own(view, sub):
        raise _Forbidden("Only the person who created this view can change it")
    return view


# ---------------------------------------------------------------------------
# Operations (called by govern_api)
# ---------------------------------------------------------------------------


def list_views(tenant_id: str, module: Any, sub: str, email: str | None) -> list[dict[str, Any]]:
    """Views the caller may see: their own, then the others, each by name."""
    module = clean_module(module)
    out = [to_api(v, sub) for v in store.config.views(tenant_id, module) if v.get("id") and _visible(v, sub, email)]
    out.sort(key=lambda v: (not v["canEdit"], v["name"].lower(), v["id"]))
    return out


def create_view(tenant_id: str, body: dict[str, Any], sub: str, creator: dict[str, Any] | None) -> dict[str, Any]:
    _unknown(body, _CREATE_KEYS, "the view")
    module = clean_module(body.get("module"))
    name = _name(body.get("name"))
    visibility = _visibility(body.get("visibility"))
    shared_with = _audience(visibility, _shared_with(body.get("sharedWith")))
    if "config" not in body:
        raise BadRequest("config is required")
    config = clean_config(body["config"])
    if sum(1 for v in store.config.views(tenant_id, module) if _own(v, sub)) >= MAX_VIEWS_PER_MODULE:
        raise LimitReached(f"You can save at most {MAX_VIEWS_PER_MODULE} {module} views. Delete one first.")
    now = iso()
    view = {"id": uuid.uuid4().hex, "module": module, "name": name, "visibility": visibility,
            "sharedWith": shared_with, "createdBy": creator, "createdBySub": sub,
            "createdAt": now, "updatedAt": now, "configJson": json.dumps(config, separators=(",", ":"))}
    if not store.config.create_view(tenant_id, view):
        raise store.ContractConflict("view id collision")      # 409; a uuid4 never repeats in practice
    return to_api(view, sub)


def update_view(tenant_id: str, view_id: str, body: dict[str, Any], sub: str, email: str | None) -> dict[str, Any]:
    _unknown(body, _PATCH_KEYS, "the view")
    if not body:
        raise BadRequest("No fields to update")
    # Shape first, so a bad body is a 400 whoever sends it.
    name = _name(body["name"]) if "name" in body else None
    visibility = _visibility(body["visibility"]) if "visibility" in body else None
    shared_with = _shared_with(body["sharedWith"]) if "sharedWith" in body else None
    config = clean_config(body["config"]) if "config" in body else None
    view = _editable(tenant_id, view_id, sub, email)

    def change(v: dict[str, Any]) -> None:
        if not _own(v, sub):
            raise _Forbidden("Only the person who created this view can change it")
        if name is not None:
            v["name"] = name
        vis = visibility or v.get("visibility") or "private"
        v["visibility"] = vis
        v["sharedWith"] = _audience(vis, shared_with if shared_with is not None else list(v.get("sharedWith") or []))
        if config is not None:
            v["configJson"] = json.dumps(config, separators=(",", ":"))
        v["updatedAt"] = iso()

    stored = store.config.mutate_view(tenant_id, view["module"], view["id"], change)
    if stored is None:
        raise NotFound(_NOT_FOUND)
    return to_api(stored, sub)


def delete_view(tenant_id: str, view_id: str, sub: str, email: str | None) -> str:
    view = _editable(tenant_id, view_id, sub, email)
    store.config.delete_view(tenant_id, view["module"], view["id"])
    return view["id"]
