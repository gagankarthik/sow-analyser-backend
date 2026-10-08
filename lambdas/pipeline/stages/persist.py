"""Stage 07 — Persist: write DynamoDB records and mark the pipeline complete.

Writes a VERSION record with S3 key pointers for all generated artefacts, one
CHANGE row per diff entry (so the timeline stage can replay them in future
runs), and finally UPDATES the document META row with the analysis results.

Two things are deliberate about that last step:

* It is an update of the analysis fields, not a replacement of the row. The row
  also holds who owns the document and which projects it belongs to; those are
  changed by the API while a run is in flight (a user files the upload into a
  project), and a whole-row write here would silently undo that.
* It happens LAST. The document only becomes READY once its version and change
  rows exist, so a failure part-way never leaves a READY document with nothing
  behind it.

Nothing is written that the document did not state: a missing title stays
empty, a missing date stays null. Fields a user edited by hand (title,
lifecycle, type) are not overwritten by a re-analysis.

This is the terminal stage — status is set to READY on success. META fields
this stage does not own (``revisionOf`` from a Govern revision upload,
ownership, projects) are left untouched because the row is updated, not
replaced.
"""
from __future__ import annotations

from typing import Any

from shared.dynamodb import (
    get_doc_meta,
    put_change,
    put_version,
    query_doc_versions,
    update_doc_fields,
    update_status,
)
from shared.keydates import compact_for_record
from shared.logger import get_logger
from shared.s3 import processed_key
from shared.schema import ProcessingStatus, now_iso

log = get_logger("blue-iq.persist")

# Cleared when a run succeeds, so a past failure does not linger on a READY document.
_FAILURE_FIELDS = ["errorMessage", "errorCode", "errorStage"]


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def run(event: dict[str, Any]) -> dict[str, Any]:
    doc_id           = event["docId"]
    tenant_id        = event["tenantId"]
    raw_key          = event.get("rawKey", "")

    log.append_keys(docId=doc_id, tenantId=tenant_id)
    update_status(doc_id, "PERSISTING")

    classification = event.get("classification") or {}
    parsed         = event.get("parsed") or {}
    lineage        = event.get("lineage") or {}
    diffs          = event.get("diffs") or {}
    timeline       = event.get("timeline") or {}
    embeddings     = event.get("embeddings") or {}

    existing_versions = query_doc_versions(doc_id)
    version_n         = _next_version(existing_versions)

    existing_meta = get_doc_meta(doc_id) or {}
    if existing_meta.get("tenantId") != tenant_id:
        # The document was deleted (or never belonged to this tenant) while the
        # run was in flight. Carrying on would bring a deleted contract back.
        raise RuntimeError("persist: document no longer exists; discarding this run")

    # A value the user set by hand (PATCH /documents/{id}) wins over re-extraction.
    edited = set(existing_meta.get("userEdited") or [])

    def keep_or(field: str, extracted: Any) -> Any:
        return existing_meta.get(field) if field in edited else extracted

    # The extracted title; else the name the upload was given; else empty — the UI
    # labels an untitled document itself.
    title = classification.get("title") or existing_meta.get("title") or ""

    # Pre-compute portfolio-level aggregates so the dashboard and document list
    # can render risk without fetching each document's classification.json.
    #
    # UNKNOWN IS NOT ZERO. An aggregate is a number only when it was actually
    # measured; otherwise it is null, so a list view can say "not assessed"
    # instead of showing a reassuring 0 / "low" that nobody computed.
    clauses        = classification.get("clauses") or []
    findings       = classification.get("keyFindings")
    clause_count   = len(clauses)
    risk_counts    = _risk_counts(clauses)
    unrated        = risk_counts["unrated"]
    any_rated      = clause_count > unrated
    high_risk      = (risk_counts["high"] + risk_counts["critical"]) if any_rated else None
    overall_risk   = _overall_risk(risk_counts) if any_rated else None

    # Commercials — surface the validated headline figures so the document list
    # and portfolio dashboard can show value/terms without fetching each
    # document's classification.json.
    commercials    = classification.get("commercials") or {}
    amendment      = classification.get("amendment") or {}
    validation     = classification.get("validation") or {}
    identification = classification.get("identification") or {}
    playbook       = classification.get("playbook") or {}
    compliance     = classification.get("compliance") or {}
    extraction     = classification.get("extraction") or {}
    confidence     = classification.get("confidence") or {}
    # The classification's own timeline block carries term/renewal dates (distinct
    # from the timeline STAGE output, which is the clause replay).
    cls_timeline   = classification.get("timeline") or {}
    key_dates, key_dates_truncated = compact_for_record(classification.get("keyDates") or [])
    clause_types   = classification.get("clauseTypes") or []
    auto_renews    = cls_timeline.get("autoRenews")
    stats          = parsed.get("stats") or {}

    # ── 1. Version + change rows first ───────────────────────────────────────
    put_version({
        "docId":             doc_id,
        "versionNumber":     version_n,
        "extractionMethod":  parsed.get("extraction_method"),
        "parsedKey":         processed_key(tenant_id, doc_id, "parsed.json"),
        "classificationKey": processed_key(tenant_id, doc_id, "classification.json"),
        "timelineKey":       processed_key(tenant_id, doc_id, "timeline.json") if timeline else None,
        "diffKey":           processed_key(tenant_id, doc_id, "diff.json") if diffs.get("changes") else None,
        "createdAt":         now_iso(),
        # Extraction report for this version (all optional for readers).
        "checksum":           parsed.get("checksum") or None,
        "engineVersion":      extraction.get("engineVersion"),
        "extractionCoverage": extraction.get("coverageRatio"),
        "segmentation":       extraction.get("segmentation"),
        "pageCount":          stats.get("pages"),
        "clauseCount":        clause_count,
        "unclassifiedCount":  extraction.get("unclassifiedCount", 0),
        "keyDateCount":       len(classification.get("keyDates") or []),
        "indexedChunks":      embeddings.get("embeddedCount"),
    })

    n_changes = 0
    for ch in diffs.get("changes", []):
        put_change({
            "docId":           doc_id,
            "changeId":        ch["changeId"],
            "clauseNumber":    ch.get("clauseNumber", ""),
            "field":           ch.get("field", "body"),
            "before":          ch.get("before", ""),
            "after":           ch.get("after", ""),
            "impactScore":     int(ch.get("impactScore", 0)),
            "impactRationale": ch.get("impactRationale", ""),
            "versionNumber":   version_n,
        })
        n_changes += 1

    # ── 2. Then the document row: analysis fields only, status READY last ────
    update_doc_fields(doc_id, {
        "title":           keep_or("title", title),
        "docType":         keep_or("docType", classification.get("docType") or existing_meta.get("docType") or "OTHER"),
        "lifecycle":       keep_or("lifecycle", classification.get("lifecycle") or existing_meta.get("lifecycle") or "draft"),
        "status":          ProcessingStatus.READY.value,
        "parties":         classification.get("parties") or [],
        "effectiveDate":   classification.get("effectiveDate"),
        # Term / renewal dates — power the obligations & renewals view without
        # fetching each document's classification.json.
        "termEndDate":       cls_timeline.get("endDate"),
        "termEndDateDerived": bool(cls_timeline.get("endDateDerived")),
        "renewalDate":       cls_timeline.get("renewalDate"),
        "autoRenews":        auto_renews if isinstance(auto_renews, bool) else None,
        "renewalNoticeDays": cls_timeline.get("renewalNoticeDays"),
        "startDate":         cls_timeline.get("startDate"),
        "executionDate":     identification.get("executionDate"),
        # Every dated event / obligation, normalised (see shared/keydates.py).
        "keyDates":          key_dates,
        "keyDateCount":      len(classification.get("keyDates") or []),
        "keyDatesTruncated": key_dates_truncated,
        # A Govern revision upload names its contract (revisionOf); keep that
        # link when the graph stage found no parent of its own.
        "parentDocId":     lineage.get("parentDocId") or existing_meta.get("revisionOf"),
        "lineageStatus":   lineage.get("status"),
        "rawKey":          raw_key or existing_meta.get("rawKey"),
        "processedPrefix": f"{tenant_id}/{doc_id}/",
        "structuralHash":  classification.get("structuralHash", ""),
        "checksum":        parsed.get("checksum", ""),
        "latestVersion":   version_n,
        # Analysis aggregates (cheap to read in list views)
        "summary":         classification.get("summary", ""),
        "clauseCount":     clause_count,
        "highRiskCount":   high_risk,
        "findingsCount":   len(findings) if isinstance(findings, list) else None,
        "overallRisk":     overall_risk,
        "riskCounts":      risk_counts,
        "unratedClauseCount": unrated,
        "clauseTypes":     clause_types[:60],
        "customClauseTypeCount": sum(1 for t in clause_types if t.get("custom")),
        # Commercial aggregates (validated; cheap to read in list/dashboard views)
        "contractValue":   commercials.get("totalContractValue"),
        "baseValue":       commercials.get("baseValue"),
        # SIGNED: negative when the amendment reduces the contract value.
        "valueDelta":      amendment.get("valueDelta"),
        "newTotalValue":   amendment.get("newTotalValue"),
        "valueCap":        commercials.get("caps"),
        "impliedTotalValue": commercials.get("impliedTotalValue"),
        "currency":        commercials.get("currency"),
        "pricingModel":    commercials.get("pricingModel"),
        "paymentTerms":    commercials.get("paymentTerms"),
        "reconciled":      validation.get("reconciled"),
        "parentReference": identification.get("parentReference"),
        # Playbook deviation aggregates (cheap to read in list/dashboard views)
        # null when the playbook check did not run (not 0 deviations).
        "playbookDeviations":     playbook.get("deviationCount"),
        "playbookReviewCount":    playbook.get("reviewCount"),
        "playbookWithinCount":    playbook.get("withinCount"),
        "playbookNoRuleCount":    playbook.get("noRuleCount"),
        "playbookChecked":        playbook.get("checked"),
        "playbookSeverity":       playbook.get("overallSeverity") if playbook.get("checked") else None,
        "playbookSource":         playbook.get("source"),
        # Compliance-pack coverage aggregates (cheap to read in list/dashboard views).
        # With no framework enabled there is no coverage and no gap count to report.
        "complianceCoveragePct":  compliance.get("overallCoveragePct"),
        "complianceGaps":         compliance.get("totalGaps") if compliance.get("evaluated") else None,
        "complianceFrameworks":   compliance.get("evaluated", []),
        # Extraction report — how complete and how trustworthy this analysis is.
        "extractionCoverage":   extraction.get("coverageRatio"),
        "unclassifiedCount":    extraction.get("unclassifiedCount", 0),
        "extractionConfidence": confidence.get("overall"),
        "needsReview":          bool(classification.get("needsReview")),
        "reviewReasons":        [str(r)[:300] for r in (classification.get("reviewReasons") or [])[:12]],
        "pageCount":            stats.get("pages"),
        "searchable":           embeddings.get("searchable"),
        "indexedChunks":        embeddings.get("embeddedCount"),
    }, remove=_FAILURE_FIELDS)

    log.info("persist.done", version=version_n, changes=n_changes,
             clauses=clause_count, highRisk=high_risk, overallRisk=overall_risk,
             unrated=unrated, keyDates=len(key_dates), keyDatesTruncated=key_dates_truncated,
             needsReview=bool(classification.get("needsReview")))
    # This result is the detail of the "Document Analysed" event the state
    # machine publishes next (govern-intake consumes it).
    return {
        "status":     ProcessingStatus.READY.value,
        "docId":      doc_id,
        "tenantId":   tenant_id,
        "docType":    keep_or("docType", classification.get("docType") or existing_meta.get("docType") or "OTHER"),
        "revisionOf": existing_meta.get("revisionOf"),
    }


def _next_version(existing_versions: list[dict[str, Any]]) -> int:
    """Highest existing version + 1.

    Counting rows (the old behaviour) reuses a number once any earlier version
    has been deleted: with versions {1, 3} it produced 3 again, the conditional
    put was skipped as a "duplicate", and the new analysis was silently lost.
    """
    return max((int(v.get("versionNumber", 0)) for v in existing_versions), default=0) + 1


# ---------------------------------------------------------------------------
# Risk aggregation helpers
# ---------------------------------------------------------------------------


def _risk_counts(clauses: list[dict[str, Any]]) -> dict[str, int]:
    """Clauses per assessed risk level, plus ``unrated`` — clauses the analysis
    did not rate. An unrated clause is never counted as low."""
    counts = {"low": 0, "medium": 0, "high": 0, "critical": 0, "unrated": 0}
    for c in clauses:
        level = str(c.get("riskLevel") or "").lower()
        counts[level if level in ("low", "medium", "high", "critical") else "unrated"] += 1
    return counts


def _unrated_count(clauses: list[dict[str, Any]]) -> int:
    return _risk_counts(clauses)["unrated"]


def _overall_risk(counts: dict[str, int]) -> str:
    """Highest risk level present in the document (or 'low' if no clauses)."""
    for level in ("critical", "high", "medium", "low"):
        if counts.get(level, 0) > 0:
            return level
    return "low"
