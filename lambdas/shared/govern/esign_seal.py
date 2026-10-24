"""Sealing signed documents with a key held in AWS KMS (docs/GOVERN_ESIGN.md §4).

The PDF gets a PAdES signature (``ETSI.CAdES.detached``) over the whole file.
The private key never leaves KMS: pyHanko asks ``DigestSigner`` to sign the
CMS signed attributes, and ``DigestSigner`` hashes them and calls ``kms:Sign``
on the digest.

THE CERTIFICATE. A PAdES signature carries an X.509 certificate for its key.
On first use Govern builds one for the KMS public key (subject "Blue-IQ Govern
Document Signing", digitalSignature + nonRepudiation, 10 years) and has KMS
sign it, so no certificate authority is involved; it is cached in govern-config
under ``SIGNING / CERT#<keyId>``. To have Adobe Reader trust the seal out of
the box, store a certificate issued by an AATL member for the same key under
that item instead (``put_certificate``); nothing else changes.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .. import aws
from ..config import settings
from ..logger import get_logger
from . import store

log = get_logger("blue-iq.govern.esign_seal")

CERT_SUBJECT = "Blue-IQ Govern Document Signing"
CERT_ORGANIZATION = "Blue-IQ"
CERT_YEARS = 10
SEAL_REASON = "Signed with Blue-IQ Govern e-signature"
SEAL_FIELD = "BlueIQGovernSeal"
KMS_ALGORITHM = "RSASSA_PKCS1_V1_5_SHA_256"
# Room for the CMS signature: chain + an RFC 3161 token fit comfortably.
SIGNATURE_SIZE = 16_384


class SigningUnavailable(RuntimeError):
    """No signing key is configured for this deployment."""


# A function that signs a SHA-256 digest and returns the raw RSA signature.
DigestSign = Callable[[bytes], bytes]


def kms_digest_signer(key_id: str) -> DigestSign:
    def sign(digest: bytes) -> bytes:
        resp = aws.kms_client().sign(KeyId=key_id, Message=digest, MessageType="DIGEST",
                                 SigningAlgorithm=KMS_ALGORITHM)
        return resp["Signature"]

    return sign


def kms_public_key(key_id: str) -> tuple[str, bytes]:
    """(the key's id, its SubjectPublicKeyInfo DER)."""
    resp = aws.kms_client().get_public_key(KeyId=key_id)
    if resp.get("KeyUsage") not in (None, "SIGN_VERIFY") or \
            KMS_ALGORITHM not in (resp.get("SigningAlgorithms") or [KMS_ALGORITHM]):
        raise SigningUnavailable("The signing key cannot make RSA PKCS#1 v1.5 SHA-256 signatures.")
    return str(resp.get("KeyId") or key_id).rsplit("/", 1)[-1], resp["PublicKey"]


# ---------------------------------------------------------------------------
# The certificate
# ---------------------------------------------------------------------------


def build_certificate(public_key_der: bytes, sign: DigestSign, *, now: datetime | None = None) -> bytes:
    """A self-issued X.509 v3 certificate (DER) for ``public_key_der``,
    signed by ``sign`` (the same key)."""
    from asn1crypto import keys, x509

    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    name = x509.Name.build({"common_name": CERT_SUBJECT, "organization_name": CERT_ORGANIZATION})
    spki = keys.PublicKeyInfo.load(public_key_der)
    serial = int.from_bytes(hashlib.sha256(public_key_der + now.isoformat().encode()).digest()[:16], "big") >> 1
    key_id = hashlib.sha1(spki["public_key"].dump()).digest()
    tbs = x509.TbsCertificate({
        "version": "v3",
        "serial_number": serial,
        "signature": {"algorithm": "sha256_rsa"},
        "issuer": name,
        "validity": {"not_before": x509.Time({"utc_time": now - timedelta(minutes=5)}),
                     "not_after": x509.Time({"general_time": now + timedelta(days=365 * CERT_YEARS)})},
        "subject": name,
        "subject_public_key_info": spki,
        "extensions": [
            {"extn_id": "basic_constraints", "critical": True, "extn_value": {"ca": False}},
            {"extn_id": "key_usage", "critical": True, "extn_value": {"digital_signature", "non_repudiation"}},
            {"extn_id": "key_identifier", "critical": False, "extn_value": key_id},
            {"extn_id": "authority_key_identifier", "critical": False, "extn_value": {"key_identifier": key_id}},
        ],
    })
    signature = sign(hashlib.sha256(tbs.dump()).digest())
    cert = x509.Certificate({"tbs_certificate": tbs, "signature_algorithm": {"algorithm": "sha256_rsa"},
                             "signature_value": signature})
    return cert.dump()


def _cert_key(key_id: str) -> dict[str, str]:
    return {"PK": "SIGNING", "SK": f"CERT#{key_id}"}


def put_certificate(key_id: str, der: bytes, *, source: str) -> bool:
    """Store the certificate for a key (first writer wins). ``source``:
    "self-issued" or the issuer's name for a purchased certificate."""
    return store.config.put_new({**_cert_key(key_id), "entityType": "SIGNING_CERT", "keyId": key_id,
                                 "der": base64.b64encode(der).decode("ascii"), "source": source,
                                 "createdAt": store.iso()})


def certificate_for(key_id: str, public_key_der: bytes, sign: DigestSign) -> bytes:
    """The stored certificate for the key, creating the self-issued one on first use."""
    item = store.clean_item(store.config.table().get_item(Key=_cert_key(key_id)).get("Item"))
    if item and item.get("der"):
        return base64.b64decode(str(item["der"]))
    der = build_certificate(public_key_der, sign)
    if not put_certificate(key_id, der, source="self-issued"):
        # Another worker made one at the same moment: use theirs.
        item = store.clean_item(store.config.table().get_item(Key=_cert_key(key_id)).get("Item")) or {}
        if item.get("der"):
            return base64.b64decode(str(item["der"]))
    log.info("esign.certificate_created", keyId=key_id)
    return der


# ---------------------------------------------------------------------------
# Sealing
# ---------------------------------------------------------------------------


def _digest_signer(cert_der: bytes, sign: DigestSign) -> Any:
    from asn1crypto import algos, x509
    from pyhanko.sign import signers

    class DigestSigner(signers.Signer):
        """pyHanko signer whose private key lives elsewhere (KMS)."""

        def __init__(self) -> None:
            cert = x509.Certificate.load(cert_der)
            super().__init__(signing_cert=cert, cert_registry=None,
                             signature_mechanism=algos.SignedDigestAlgorithm({"algorithm": "sha256_rsa"}))
            self._size = cert.public_key.bit_size // 8

        async def async_sign_raw(self, data: bytes, digest_algorithm: str, dry_run: bool = False) -> bytes:
            if dry_run:
                return bytes(self._size)
            if digest_algorithm != "sha256":
                raise ValueError(f"unsupported digest {digest_algorithm}")
            return sign(hashlib.sha256(data).digest())

    return DigestSigner()


async def _seal(data: bytes, cert_der: bytes, sign: DigestSign, tsa_url: str | None, location: str | None) -> bytes:
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers

    timestamper = None
    if tsa_url:
        from pyhanko.sign.timestamps import HTTPTimeStamper

        timestamper = HTTPTimeStamper(tsa_url, timeout=15)
    meta = signers.PdfSignatureMetadata(field_name=SEAL_FIELD, reason=SEAL_REASON, location=location,
                                        subfilter=fields.SigSeedSubFilter.PADES, md_algorithm="sha256")
    writer = IncrementalPdfFileWriter(io.BytesIO(data))
    pdf_signer = signers.PdfSigner(meta, signer=_digest_signer(cert_der, sign), timestamper=timestamper)
    out = await pdf_signer.async_sign_pdf(writer, bytes_reserved=SIGNATURE_SIZE)
    return out.getvalue()


def seal_pdf(data: bytes, *, sign: DigestSign | None = None, public_key_der: bytes | None = None,
             key_id: str | None = None, tsa_url: str | None = None) -> tuple[bytes, dict[str, Any]]:
    """The sealed PDF and what was used: ``{keyId, timestamped}``.

    With no arguments, the deployment's KMS key (``SIGNING_KMS_KEY_ID``) and
    timestamp authority (``SIGNING_TSA_URL``) are used. Tests pass ``sign``,
    ``public_key_der`` and ``key_id`` for a local key.
    """
    if sign is None:
        configured = settings.signing_kms_key_id
        if not configured:
            raise SigningUnavailable("Signing is not set up for this deployment.")
        key_id, public_key_der = kms_public_key(configured)
        sign = kms_digest_signer(configured)
    if public_key_der is None or key_id is None:
        raise SigningUnavailable("A signing key and its public key are required.")
    tsa = settings.signing_tsa_url if tsa_url is None else tsa_url
    cert_der = certificate_for(key_id, public_key_der, sign)
    location = settings.app_base_url.rstrip("/") or None
    try:
        sealed = asyncio.run(_seal(data, cert_der, sign, tsa or None, location))
        timestamped = bool(tsa)
    except Exception as exc:  # noqa: BLE001 — a timestamp authority outage must not block signing
        if not tsa:
            raise
        log.warning("esign.timestamp_failed", error_type=type(exc).__name__)
        sealed = asyncio.run(_seal(data, cert_der, sign, None, location))
        timestamped = False
    return sealed, {"keyId": key_id, "timestamped": timestamped}


def verify(data: bytes, trust_der: bytes | None = None) -> dict[str, Any]:
    """Check a sealed PDF's signature (tests and the runbook): intact, valid,
    whole-file coverage, and trusted when ``trust_der`` is given."""
    from asn1crypto import x509
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import async_validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    reader = PdfFileReader(io.BytesIO(data))
    if not reader.embedded_signatures:
        return {"signed": False}
    emb = reader.embedded_signatures[-1]
    vc = ValidationContext(trust_roots=[x509.Certificate.load(trust_der)]) if trust_der else ValidationContext()
    status = asyncio.run(async_validate_pdf_signature(emb, vc))
    return {"signed": True, "intact": status.intact, "valid": status.valid, "trusted": status.trusted,
            "wholeFile": status.coverage.name == "ENTIRE_FILE", "timestamped": status.timestamp_validity is not None}
