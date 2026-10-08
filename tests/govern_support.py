"""Shared fixtures-as-functions for the Govern tests.

Documents are seeded straight into the fake documents table; their analysis
(classification + parsed header text) comes from the Northfield sample agreements in
``samples/research``, cut into labelled clauses exactly as test_govern_matrix.py
does (``_sample_clauses`` — the stand-in for the pipeline's classify stage).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared.govern import workflow
from test_govern_matrix import _sample_clauses

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "research"

OWNER = "aaaaaaaa-0000-4000-8000-0000000000a1"
EDITOR = "aaaaaaaa-0000-4000-8000-0000000000a2"
VIEWER = "aaaaaaaa-0000-4000-8000-0000000000a3"
OUTSIDER = "aaaaaaaa-0000-4000-8000-0000000000a4"
EMAIL = {OWNER: "dana@northfield.edu", EDITOR: "eli@northfield.edu", VIEWER: "avery@northfield.edu", OUTSIDER: "x@else.com"}
NAME = {OWNER: "Dana Ruiz", EDITOR: "Eli Park", VIEWER: "Avery Chen", OUTSIDER: "Out Sider"}
TENANT = f"u-{OWNER}"
PROJECT = "proj_osu"

LICENSE_V1 = "01-exclusive-license-v1.txt"
LICENSE_V2 = "01-exclusive-license-v2-revised.txt"
SRA = "03-sponsored-research-agreement.txt"

DANA = {"email": EMAIL[OWNER], "name": NAME[OWNER]}


def claims(sub: str, groups: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"sub": sub, "email": EMAIL[sub], "email_verified": "true", "name": NAME[sub]}
    if groups is not None:
        out["cognito:groups"] = groups
    return out


def header_of(sample: str) -> str:
    text = (SAMPLES / sample).read_text(encoding="utf-8")
    return "\n".join([ln.strip() for ln in text.splitlines() if ln.strip()][:60])


def classification_of(sample: str, parties: list[str]) -> dict[str, Any]:
    clauses = _sample_clauses(sample)
    title = (SAMPLES / sample).read_text(encoding="utf-8").splitlines()[0].strip().title()
    return {"title": title, "summary": "", "parties": parties, "clauses": clauses}


def seed_doc(ddb, doc_id: str, *, sample: str = LICENSE_V1, owner: str = OWNER, status: str = "READY",
             doc_type: str = "LICENSE", value: float | None = None, project_ids: list[str] | None = None,
             parties: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    first = (SAMPLES / sample).read_text(encoding="utf-8").splitlines()[0].strip().title()
    meta = {
        "PK": f"DOC#{doc_id}", "SK": "META", "GSI1PK": f"TENANT#u-{owner}", "GSI1SK": f"DOC#{doc_id}",
        "entityType": "DOCUMENT", "docId": doc_id, "tenantId": f"u-{owner}", "ownerSub": owner,
        "ownerEmail": EMAIL[owner], "title": first, "docType": doc_type, "status": status,
        "parties": parties or ["Northfield University", "Lakeshore BioSensors, Inc."],
        "projectIds": project_ids or [], "latestVersion": 1, "createdAt": "2026-10-01T09:00:00Z",
        "updatedAt": "2026-10-01T09:00:00Z", "lifecycle": "draft", "sample": sample, **extra,
    }
    if value is not None:
        meta["contractValue"] = value
        meta["currency"] = "USD"
    ddb.items[(f"DOC#{doc_id}", "META")] = meta
    return meta


def seed_project(ddb, members: dict[str, str], doc_ids: list[str], project_id: str = PROJECT) -> None:
    """A project owned by OWNER; ``members`` maps sub → role."""
    ddb.items[(f"PROJ#{project_id}", "META")] = {
        "PK": f"PROJ#{project_id}", "SK": "META", "entityType": "PROJECT", "projectId": project_id,
        "name": "Northfield", "ownerSub": OWNER, "ownerEmail": EMAIL[OWNER], "docIds": list(doc_ids), "rev": 1}
    ddb.items[(f"PROJ#{project_id}", "OWNER")] = {
        "PK": f"PROJ#{project_id}", "SK": "OWNER", "GSI1PK": f"USER#{OWNER}", "GSI1SK": f"PROJ#{project_id}",
        "projectId": project_id, "role": "owner"}
    for sub, role in members.items():
        ddb.items[(f"PROJ#{project_id}", f"MEMBER#{EMAIL[sub]}")] = {
            "PK": f"PROJ#{project_id}", "SK": f"MEMBER#{EMAIL[sub]}", "GSI1PK": f"MEMBER#{EMAIL[sub]}",
            "GSI1SK": f"PROJ#{project_id}", "projectId": project_id, "email": EMAIL[sub], "role": role,
            "status": "active"}


def install_analysis(monkeypatch, ddb) -> None:
    """Serve each seeded document's analysis from its ``sample`` file."""
    def classification(doc_id: str) -> dict[str, Any] | None:
        meta = ddb.doc(doc_id) or {}
        if not meta.get("sample"):
            return None
        return classification_of(meta["sample"], list(meta.get("parties") or []))

    def header(doc_id: str) -> str:
        meta = ddb.doc(doc_id) or {}
        return header_of(meta["sample"]) if meta.get("sample") else ""

    monkeypatch.setattr(workflow, "load_classification", classification)
    monkeypatch.setattr(workflow, "load_header_text", header)


def call(method: str, path: str, sub: str, body: Any = None, qs: dict[str, str] | None = None,
         groups: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    from govern_api import handler as api

    event = {"rawPath": path, "queryStringParameters": qs, "body": json.dumps(body) if body is not None else None,
             "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims(sub, groups)}}}}
    resp = api.dispatch(event)
    return resp["statusCode"], json.loads(resp["body"])


def analysed_event(doc_id: str, tenant: str = TENANT) -> dict[str, Any]:
    return {"source": "blue-iq.pipeline", "detail-type": "Document Analysed",
            "detail": {"docId": doc_id, "tenantId": tenant, "status": "READY"}}


def lambda_context(name: str = "govern") -> Any:
    """The slice of a Lambda context Powertools' logger reads."""
    import types

    return types.SimpleNamespace(function_name=name, memory_limit_in_mb=256, aws_request_id="req-1",
                                 invoked_function_arn=f"arn:aws:lambda:us-east-2:000000000000:function:{name}")
