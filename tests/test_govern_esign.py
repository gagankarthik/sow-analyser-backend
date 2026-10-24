"""Govern e-signature (docs/GOVERN_ESIGN.md): drafts, sending, the signer's
link, sequential and parallel order, sealing with a key that only signs
digests (the shape of KMS), decline / void / expiry / reminders through the
contract workflow, the certificate of completion and the seal's validity.
"""
from __future__ import annotations

import base64
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from govern_intake import handler as intake
from govern_seal import handler as seal_api
from govern_signing import handler as signing_api
from govern_sweeper import handler as sweeper
from govern_support import (EDITOR, OWNER, VIEWER, analysed_event, call, install_analysis, lambda_context, seed_doc,
                            seed_project)
from shared.config import settings
from shared.govern import esign, esign_pdf, esign_seal, store

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_DER = KEY.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def sign_digest(digest: bytes) -> bytes:
    return KEY.sign(digest, padding.PKCS1v15(), Prehashed(hashes.SHA256()))


_SEAL_PDF = esign_seal.seal_pdf


def local_seal(data: bytes):
    return _SEAL_PDF(data, sign=sign_digest, public_key_der=PUBLIC_DER, key_id="test-key", tsa_url="")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _S3:
    """An in-memory S3 for the e-signature code (objects, presigned urls, prefixes)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.signed: list[dict] = []

    def put_object(self, Bucket, Key, Body, ContentType=None, Metadata=None, **_):  # noqa: N803
        self.objects[(Bucket, Key)] = bytes(Body)
        return {}

    def get_object(self, Bucket, Key, **_):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "no"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key, **_):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "no"}}, "HeadObject")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        self.signed.append({"op": op, **Params, "ExpiresIn": ExpiresIn})
        return f"https://s3.invalid/{Params['Bucket']}/{Params['Key']}"

    def get_paginator(self, _name):
        s3 = self

        class _P:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                return [{"Contents": [{"Key": k} for (b, k) in s3.objects if b == Bucket and k.startswith(Prefix)]}]

        return _P()

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        for o in Delete["Objects"]:
            self.objects.pop((Bucket, o["Key"]), None)
        return {}


def make_pdf(pages: int = 2, rotate_last: bool = False) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for i in range(pages):
        c.drawString(72, 720, f"Exclusive license agreement - page {i + 1}")
        c.showPage()
    c.save()
    data = buf.getvalue()
    if not rotate_last:
        return data
    from pypdf import PdfReader, PdfWriter

    reader, writer = PdfReader(io.BytesIO(data)), PdfWriter()
    for i, page in enumerate(reader.pages):
        if i == len(reader.pages) - 1:
            page.rotate(90)
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def png_data_url(width: int = 300, height: int = 90) -> str:
    img = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    for x in range(20, width - 20):
        img.putpixel((x, height // 2 + (x % 7) - 3), (11, 26, 58, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def world(gov, ddb, monkeypatch):
    install_analysis(monkeypatch, ddb)
    s3 = _S3()
    import shared.aws
    import shared.s3

    monkeypatch.setattr(shared.aws, "s3_client", lambda: s3)
    monkeypatch.setattr(shared.s3, "s3_client", lambda: s3)
    monkeypatch.setattr(settings, "raw_bucket", "raw")
    monkeypatch.setattr(settings, "processed_bucket", "processed")
    monkeypatch.setattr(settings, "app_base_url", "https://app.example")
    monkeypatch.setattr(settings, "notify_from_email", "govern@northfield.edu")
    monkeypatch.setattr(settings, "signing_kms_key_id", "alias/test-signing")
    monkeypatch.setattr(settings, "seal_queue_url", "https://sqs.invalid/seal")
    monkeypatch.setattr(esign_seal, "seal_pdf", lambda data, **_: local_seal(data))
    seed_doc(ddb, "lic-1", project_ids=["proj_osu"], rawKey="u-owner/lic-1/license.pdf", filename="license.pdf")
    seed_project(ddb, {EDITOR: "editor", VIEWER: "viewer"}, ["lic-1"])
    s3.objects[("raw", "u-owner/lic-1/license.pdf")] = make_pdf(2)
    assert intake.handle_event(analysed_event("lic-1")) == "created"
    store.contracts.mutate("lic-1", lambda c: c.update({"state": "ready_to_sign", "stage": "approval"}))
    gov.s3 = s3
    return gov


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIGNERS = [{"name": "Jordan Avery", "email": "jordan@lakeshore.com", "role": "signer", "order": 1},
           {"name": "Dana Ruiz", "email": "dana@northfield.edu", "role": "signer", "order": 2},
           {"name": "Records Office", "email": "records@northfield.edu", "role": "cc", "order": 3}]


def draft(sub=EDITOR, body=None):
    status, out = call("POST", "/contracts/lic-1/envelopes", sub, body or {})
    assert status == 201, out
    return out


def prepare(env_id, *, order="sequential", signers=SIGNERS, extra_fields=()):
    fields = []
    for i, r in enumerate([s for s in signers if s["role"] == "signer"]):
        rid = f"r{i + 1}"
        fields += [{"recipientId": rid, "type": "signature", "page": 1, "x": 0.1, "y": 0.6 + i * 0.15, "w": 0.3, "h": 0.06},
                   {"recipientId": rid, "type": "date", "page": 1, "x": 0.6, "y": 0.6 + i * 0.15, "w": 0.2, "h": 0.04},
                   {"recipientId": rid, "type": "name", "page": 1, "x": 0.1, "y": 0.68 + i * 0.15, "w": 0.3, "h": 0.04}]
    fields += list(extra_fields)
    status, out = call("PUT", f"/contracts/lic-1/envelopes/{env_id}", EDITOR,
                       {"recipients": signers, "fields": fields, "signingOrder": order,
                        "message": "Please sign by Friday."})
    assert status == 200, out
    return out


def send(env_id):
    status, out = call("POST", f"/contracts/lic-1/envelopes/{env_id}/send", EDITOR)
    assert status == 200, out
    return out


def token_of(url: str) -> str:
    assert url.startswith("https://app.example/sign/")
    return url.rsplit("/", 1)[1]


def signer(method, token, verb=None, body=None):
    path = f"/sign/{token}" + (f"/{verb}" if verb else "")
    resp = signing_api.dispatch({"rawPath": path, "body": json.dumps(body) if body is not None else None,
                                 "requestContext": {"http": {"method": method, "sourceIp": "203.0.113.7",
                                                             "userAgent": "Mozilla/5.0 (Test)"}}})
    return resp["statusCode"], (json.loads(resp["body"]) if resp["body"] else None), resp


def sign(token, **extra):
    body = {"consent": True, "values": {}, "signature": {"image": png_data_url(), "kind": "drawn"}, **extra}
    return signer("POST", token, "complete", body)


def env_item(gov, env_id):
    return gov.contracts.items[("CON#lic-1", f"ENV#{env_id}")]


def invitations(gov):
    return [e for e in gov.emails if b"Please sign: " in e["Content"]["Raw"]["Data"]]


# ---------------------------------------------------------------------------
# PDF and seal
# ---------------------------------------------------------------------------


def test_pages_render_flatten_and_the_seal_validates(gov):
    source = make_pdf(2, rotate_last=True)
    pages = esign_pdf.render_pages(source)
    assert [(round(p.width), round(p.height)) for p in pages] == [(612, 792), (792, 612)]
    assert all(p.png.startswith(b"\x89PNG") for p in pages)
    fields = [{"id": "f1", "type": "name", "page": 0, "x": 0.1, "y": 0.1, "w": 0.4, "h": 0.05, "value": "Jordan Avery"},
              {"id": "f2", "type": "text", "page": 1, "x": 0.5, "y": 0.5, "w": 0.4, "h": 0.06, "value": "Rotated OK"},
              {"id": "f3", "type": "checkbox", "page": 0, "x": 0.8, "y": 0.8, "w": 0.04, "h": 0.04, "value": "true"}]
    flat = esign_pdf.flatten(source, fields, {})
    import pdfplumber

    with pdfplumber.open(io.BytesIO(flat)) as pdf:
        first = pdf.pages[0]
        word = next(w for w in first.extract_words() if w["text"] == "Jordan")
        # Placed 10 % from the left and the top of the displayed page.
        assert abs(word["x0"] - (0.1 * 612 + 2)) < 2 and 0.1 * 792 < word["top"] < 0.15 * 792
        rotated = pdf.pages[1]
        assert (round(rotated.width), round(rotated.height)) == (792, 612)
        word = next(w for w in rotated.extract_words() if w["text"] == "Rotated")
        assert abs(word["x0"] - (0.5 * 792 + 2)) < 3 and 0.5 * 612 < word["top"] < 0.56 * 612

    env = {"envelopeId": "abc", "name": "License", "source": {"pageCount": 2},
           "recipients": [{"id": "r1", "role": "signer", "name": "Jordan Avery", "email": "j@x.com",
                           "sentAt": "2026-10-20T10:00:00Z", "signedAt": "2026-10-20T11:00:00Z", "ip": "203.0.113.7"}]}
    cert = esign_pdf.certificate(envelope=env, organization="Northfield University", source_sha256="a" * 64,
                                 signed_sha256="b" * 64, signatures={}, completed_at="2026-10-20T11:00:00Z")
    final = esign_pdf.append(flat, cert)
    sealed, used = esign_seal.seal_pdf(final, sign=sign_digest, public_key_der=PUBLIC_DER, key_id="k", tsa_url="")
    assert used == {"keyId": "k", "timestamped": False}


def test_seal_is_whole_file_valid_and_tamper_evident(gov):
    final = make_pdf(1)
    sealed, _ = esign_seal.seal_pdf(final, sign=sign_digest, public_key_der=PUBLIC_DER, key_id="k", tsa_url="")
    cert_der = esign_seal.certificate_for("k", PUBLIC_DER, sign_digest)       # cached, same certificate
    status = esign_seal.verify(sealed, cert_der)
    assert status == {"signed": True, "intact": True, "valid": True, "trusted": True, "wholeFile": True,
                      "timestamped": False}
    # One byte of the signed range changed: the seal no longer holds.
    index = sealed.index(b"ReportLab")
    tampered = sealed[:index] + b"I" + sealed[index + 1:]
    assert esign_seal.verify(tampered, cert_der)["intact"] is False


def test_certificate_is_self_issued_by_the_key_and_cached(gov):
    from asn1crypto import x509

    der = esign_seal.certificate_for("k1", PUBLIC_DER, sign_digest)
    cert = x509.Certificate.load(der)
    assert cert.subject.native["common_name"] == "Blue-IQ Govern Document Signing"
    assert cert.issuer == cert.subject and cert.key_usage_value.native == {"digital_signature", "non_repudiation"}
    assert esign_seal.certificate_for("k1", PUBLIC_DER, lambda _d: b"never called") == der


# ---------------------------------------------------------------------------
# The full flow
# ---------------------------------------------------------------------------


def test_sequential_envelope_from_draft_to_sealed(world):
    env = draft()
    assert env["status"] == "draft" and env["canEdit"] and len(env["pages"]) == 2
    assert env["source"]["filename"] == "license.pdf" and len(env["source"]["sha256"]) == 64
    assert "key" not in json.dumps(env["source"]) and "tokenHash" not in json.dumps(env)
    env = prepare(env["id"])
    assert [r["id"] for r in env["recipients"]] == ["r1", "r2", "r3"] and len(env["fields"]) == 6

    sent = send(env["id"])
    assert sent["status"] == "sent" and world.contract("lic-1")["state"] == "out_for_signature"
    assert world.contract("lic-1")["signature"]["provider"] == "govern"
    (link1,) = sent["links"]                                       # sequential: only the first signer
    assert link1["recipientId"] == "r1" and link1["emailed"] is True and len(invitations(world)) == 1
    token1 = token_of(link1["url"])

    status, view, resp = signer("GET", token1)
    assert status == 200 and view["status"] == "ready" and view["recipient"]["email"] == "jordan@lakeshore.com"
    assert len(view["pages"]) == 2 and {f["type"] for f in view["fields"]} == {"signature", "date", "name"}
    assert resp["headers"]["Cache-Control"] == "no-store" and resp["headers"]["X-Robots-Tag"] == "noindex, nofollow"
    stored = env_item(world, env["id"])
    assert stored["recipients"][0]["status"] == "viewed" and stored["recipients"][0]["ip"] == "203.0.113.7"

    status, out, _ = sign(token1)
    assert status == 200 and out == {"status": "signed", "envelopeStatus": "sent"}
    assert len(invitations(world)) == 2                            # now it is Dana's turn
    assert world.contract("lic-1")["state"] == "out_for_signature"
    dana = token_of(_last_link(world))
    status, out, _ = sign(dana)
    assert out["envelopeStatus"] == "sealing" and len(world.sqs_messages) == 1

    result = seal_api.handler({"Records": [{"messageId": "m1", "body": world.sqs_messages[0]["MessageBody"]}]},
                              lambda_context("govern-seal"))
    assert result == {"batchItemFailures": []}
    stored = env_item(world, env["id"])
    assert stored["status"] == "completed" and stored["sealed"]["sha256"]
    contract = world.contract("lic-1")
    assert contract["state"] == "signed" and contract["signature"]["envelopeId"] == env["id"]
    actions = world.actions("lic-1")
    for a in ("signature_sent", "signature_viewed", "signature_signed", "signed", "signature_completed"):
        assert a in actions, a
    sealed = world.s3.objects[("processed", f"esign/{stored['tenantId']}/lic-1/{env['id']}/sealed.pdf")]
    cert = esign_seal.certificate_for("test-key", PUBLIC_DER, sign_digest)
    assert esign_seal.verify(sealed, cert)["valid"] is True
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(sealed))
    assert len(reader.pages) == 3                                   # two pages + the certificate
    assert "Certificate of completion" in reader.pages[2].extract_text()
    assert "Jordan Avery" in reader.pages[1].extract_text()
    done = [e for e in world.emails if b"Completed:" in e["Content"]["Raw"]["Data"]]
    assert len(done) == 1 and b"application/pdf" in done[0]["Content"]["Raw"]["Data"]

    status, dl = call("GET", f"/contracts/lic-1/envelopes/{env['id']}/download", EDITOR, qs={"file": "sealed"})
    assert status == 200 and dl["filename"].endswith("(signed).pdf")
    status, out, _ = signer("GET", token1, "download")
    assert status == 200 and out["url"]
    status, view, _ = signer("GET", token1)
    assert view["status"] == "signed" and view["canDownload"] is True


def _last_link(world) -> str:
    raw = invitations(world)[-1]["Content"]["Raw"]["Data"].decode("utf-8", "ignore")
    import email

    msg = email.message_from_string(raw)
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            text = part.get_payload(decode=True).decode()
            return next(w for w in text.split() if w.startswith("https://app.example/sign/"))
    raise AssertionError("no link")


def test_parallel_order_invites_every_signer_at_once(world):
    env = prepare(draft()["id"], order="parallel")
    sent = send(env["id"])
    assert {link["recipientId"] for link in sent["links"]} == {"r1", "r2"}


def test_decline_closes_the_envelope_and_reopens_the_contract(world):
    env = prepare(draft()["id"])
    token = token_of(send(env["id"])["links"][0]["url"])
    status, out, _ = signer("POST", token, "decline", {"reason": "The indemnity cap is missing."})
    assert status == 200 and out == {"status": "declined"}
    assert env_item(world, env["id"])["status"] == "declined"
    contract = world.contract("lic-1")
    assert contract["state"] == "ready_to_sign" and contract["signature"] is None
    assert "signature_declined" in world.actions("lic-1")
    status, body, _ = signer("GET", token)                       # the link is gone
    assert status == 404 and body["code"] == "not_found"
    assert any(b"Declined:" in e["Content"]["Raw"]["Data"] for e in world.emails)


def test_void_revokes_the_links(world):
    env = prepare(draft()["id"])
    token = token_of(send(env["id"])["links"][0]["url"])
    status, body = call("POST", f"/contracts/lic-1/envelopes/{env['id']}/void", EDITOR, {})
    assert status == 400                                           # a reason is required
    status, body = call("POST", f"/contracts/lic-1/envelopes/{env['id']}/void", EDITOR, {"reason": "Wrong version"})
    assert status == 200 and body["status"] == "voided" and body["voidReason"] == "Wrong version"
    assert world.contract("lic-1")["state"] == "ready_to_sign"
    assert signer("GET", token)[0] == 404
    assert any(b"Cancelled:" in e["Content"]["Raw"]["Data"] for e in world.emails)


def test_sweeper_expires_and_reminds(world):
    env = prepare(draft()["id"])
    send(env["id"])
    later = datetime.now(timezone.utc) + timedelta(days=4)
    assert esign.sweep_contract(world.contract("lic-1"), later) == "reminded"
    assert len(invitations(world)) == 2
    assert b"Reminder:" in invitations(world)[-1]["Content"]["Raw"]["Data"]
    assert esign.sweep_contract(world.contract("lic-1"), later + timedelta(hours=1)) is None   # not again so soon
    expired_at = datetime.now(timezone.utc) + timedelta(days=15)
    totals = sweeper.sweep(expired_at)
    assert totals["signing"] == 1
    assert env_item(world, env["id"])["status"] == "expired"
    assert world.contract("lic-1")["state"] == "ready_to_sign"
    assert "signature_expired" in world.actions("lic-1")


def test_unknown_malformed_and_closed_tokens_look_the_same(world):
    env = prepare(draft()["id"])
    token = token_of(send(env["id"])["links"][0]["url"])
    bodies = {json.dumps(signer("GET", t)[1]) for t in ("x" * 43, "short", "A" * 43)}
    assert len(bodies) == 1 and signer("GET", "A" * 43)[0] == 404
    assert signer("GET", token)[0] == 200


def test_sending_needs_an_approved_contract_a_key_and_signature_fields(world, monkeypatch):
    env = draft()
    status, body = call("POST", f"/contracts/lic-1/envelopes/{env['id']}/send", EDITOR)
    assert (status, body["code"]) == (400, "bad_request")             # no signers yet
    prepare(env["id"], signers=[SIGNERS[0]])
    status, out = call("PUT", f"/contracts/lic-1/envelopes/{env['id']}", EDITOR,
                       {"fields": [{"recipientId": "r1", "type": "date", "page": 0, "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.04}]})
    status, body = call("POST", f"/contracts/lic-1/envelopes/{env['id']}/send", EDITOR)
    assert status == 400 and "signature field" in body["error"]
    prepare(env["id"], signers=[SIGNERS[0]])
    store.contracts.mutate("lic-1", lambda c: c.update({"state": "in_review", "stage": "review"}))
    status, body = call("POST", f"/contracts/lic-1/envelopes/{env['id']}/send", EDITOR)
    assert (status, body["code"]) == (409, "invalid_transition")
    store.contracts.mutate("lic-1", lambda c: c.update({"state": "ready_to_sign", "stage": "approval"}))
    monkeypatch.setattr(settings, "signing_kms_key_id", "")
    status, body = call("POST", f"/contracts/lic-1/envelopes/{env['id']}/send", EDITOR)
    assert (status, body["code"]) == (503, "signing_unavailable")
    assert env_item(world, env["id"])["status"] == "draft"


def test_field_and_recipient_validation(world):
    env = draft()
    bad = [
        {"recipients": [{"name": "", "email": "x@y.com"}]},
        {"recipients": [{"name": "A", "email": "not-an-email"}]},
        {"recipients": [{"name": "A", "email": "a@y.com", "role": "witness"}]},
        {"recipients": [SIGNERS[0]], "fields": [{"recipientId": "r1", "type": "signature", "page": 9, "x": 0, "y": 0,
                                                 "w": 0.2, "h": 0.05}]},
        {"recipients": [SIGNERS[0]], "fields": [{"recipientId": "r1", "type": "signature", "page": 0, "x": 0.9,
                                                 "y": 0, "w": 0.2, "h": 0.05}]},
        {"recipients": [SIGNERS[0]], "fields": [{"recipientId": "r9", "type": "signature", "page": 0, "x": 0,
                                                 "y": 0, "w": 0.2, "h": 0.05}]},
        {"expiresInDays": 90}, {"signingOrder": "random"},
    ]
    for body in bad:
        status, out = call("PUT", f"/contracts/lic-1/envelopes/{env['id']}", EDITOR, body)
        assert (status, out["code"]) == (400, "bad_request"), body


def test_signer_must_consent_and_send_a_real_png(world):
    env = prepare(draft()["id"], signers=[SIGNERS[0]])
    token = token_of(send(env["id"])["links"][0]["url"])
    status, body, _ = signer("POST", token, "complete", {"consent": False, "signature": {"image": png_data_url()}})
    assert status == 400
    status, body, _ = signer("POST", token, "complete", {"consent": True, "signature": {"image": "data:image/png;base64,AAAA"}})
    assert status == 400
    status, body, _ = signer("POST", token, "complete", {"consent": True, "signature": {"image": "https://evil/x.png"}})
    assert status == 400
    assert env_item(world, env["id"])["recipients"][0]["status"] != "signed"


def test_word_versions_need_a_pdf_and_viewers_cannot_prepare(world, ddb):
    ddb.items[("DOC#lic-1", "META")]["rawKey"] = "u-owner/lic-1/license.docx"
    status, body = call("POST", "/contracts/lic-1/envelopes", EDITOR, {})
    assert (status, body["code"]) == (409, "not_pdf")
    status, body = call("POST", "/contracts/lic-1/envelopes", VIEWER, {})
    assert status == 403


def test_only_drafts_can_be_deleted(world):
    env = draft()
    status, _ = call("DELETE", f"/contracts/lic-1/envelopes/{env['id']}", OWNER)
    assert status == 200 and ("CON#lic-1", f"ENV#{env['id']}") not in world.contracts.items
    assert not [k for (b, k) in world.s3.objects if k.startswith(f"esign/") and env["id"] in k]


def test_reopening_the_contract_voids_the_open_envelope(world):
    env = prepare(draft()["id"])
    token = token_of(send(env["id"])["links"][0]["url"])
    status, body = call("POST", "/contracts/lic-1/actions", EDITOR, {"action": "reopen"})
    assert status == 200 and body["contract"]["state"] == "in_review"
    stored = env_item(world, env["id"])
    assert stored["status"] == "voided" and "in review" in stored["voidReason"]
    assert signer("GET", token)[0] == 404


def test_marking_signed_by_hand_voids_the_open_envelope(world):
    env = prepare(draft()["id"])
    send(env["id"])
    status, body = call("POST", "/contracts/lic-1/actions", EDITOR, {"action": "mark_signed"})
    assert status == 200 and body["contract"]["state"] == "signed"
    assert env_item(world, env["id"])["status"] == "voided"

