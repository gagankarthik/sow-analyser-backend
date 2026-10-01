"""The playbook made visible: per-clause outcomes, the effective rules, and a
workspace's own rules.

"No rule for this clause type" is its own outcome — never a pass.
"""
from __future__ import annotations

import json

import pytest

from api import handler as api
from shared import playbook
from shared.access import Caller

SUB_A = "11111111-1111-4111-8111-111111111111"
SUB_B = "22222222-2222-4222-8222-222222222222"


def clauses():
    return [
        {"id": "c001", "number": "1", "title": "Scope", "body": "Build the site.", "category": "ScopeOfWork"},
        {"id": "c002", "number": "2", "title": "Payment", "body": "Invoices are payable Net 45.", "category": "Payment"},
        {"id": "c003", "number": "3", "title": "Liability", "body": "Liability is uncapped.", "category": "Liability"},
        {"id": "c004", "number": "4", "title": "Confidentiality", "body": "Confidential for 2 years.", "category": "Confidentiality"},
        {"id": "c005", "number": "5", "title": "Termination", "body": "Either party may terminate.", "category": "Termination"},
        {"id": "c006", "number": "6", "title": "Non-solicitation", "body": "Neither party shall solicit staff for 12 months.",
         "category": "Other", "specificType": "Non-solicitation", "specificTypeKey": "non-solicitation", "typeIsCustom": True},
        {"id": "c007", "number": "7", "title": "", "body": "garbled", "category": None, "classificationStatus": "unclassified"},
        {"id": "c008", "number": "8", "title": "Fees", "body": "Fees may increase by 3% a year.", "category": "Fees"},
    ]


# ── Outcomes ─────────────────────────────────────────────────────────────────


def test_every_clause_gets_exactly_one_outcome_and_no_rule_is_not_a_pass():
    out = playbook.evaluate_clauses(clauses(), tenant_id=None)
    results = {r["clauseId"]: r for r in out["clauseResults"]}
    assert len(results) == 8
    assert {cid: r["outcome"] for cid, r in results.items()} == {
        "c001": "no_rule",        # no position for scope-of-work clauses
        "c002": "deviates",       # Net 45 against Net 30
        "c003": "deviates",       # uncapped
        "c004": "within",
        "c005": "within",
        "c006": "no_rule",        # a custom clause type with no rule
        "c007": "unclassified",
        "c008": "within",
    }
    assert out["checked"] == 5 and out["withinCount"] == 3 and out["deviationCount"] == 2
    assert out["noRuleCount"] == 2 and out["unclassifiedCount"] == 1 and out["reviewCount"] == 0
    assert out["checked"] + out["noRuleCount"] + out["unclassifiedCount"] == len(clauses())
    assert out["source"] == "default"
    # a "no rule" clause carries no rule, no standard and no verdict
    assert results["c001"]["ruleId"] is None and results["c001"]["standard"] is None
    assert "nothing was checked" in results["c001"]["reason"]


def test_result_rows_carry_rule_position_found_value_and_reason():
    out = playbook.evaluate_clauses(clauses(), tenant_id=None)
    pay = next(r for r in out["clauseResults"] if r["clauseId"] == "c002")
    assert pay == {
        "clauseId": "c002", "clauseNumber": "2", "category": "Payment", "specificType": None,
        "specificTypeKey": None, "ruleId": "Payment", "ruleName": "Payment Terms",
        "standard": "Net 30 from invoice date.", "fallback": None, "found": "Net 45",
        "severity": "minor", "reason": "Payment term is Net 45; standard is Net 30.",
        "outcome": "deviates", "status": "minor",
    }
    lia = next(r for r in out["clauseResults"] if r["clauseId"] == "c003")
    assert lia["found"] == "uncapped" and lia["severity"] == "material"
    assert out["overallSeverity"] == "material"
    dev = next(d for d in out["deviations"] if d["clauseId"] == "c002")
    assert dev["found"] == "Net 45" and dev["ruleId"] == "Payment"       # existing list, extra fields


def test_a_clause_the_rule_cannot_decide_is_flagged_not_passed():
    out = playbook.evaluate_clauses(
        [{"id": "c1", "number": "1", "body": "Fees are payable as agreed between the parties.", "category": "Payment"}])
    row = out["clauseResults"][0]
    assert (row["outcome"], row["status"], row["severity"]) == ("flagged", "review", None)
    assert out["reviewCount"] == 1 and out["deviationCount"] == 0 and out["withinCount"] == 0


def test_results_are_deterministic():
    assert playbook.evaluate_clauses(clauses()) == playbook.evaluate_clauses(clauses())


@pytest.mark.parametrize("body,expected", [
    ("Either party may terminate for convenience on thirty (30) days' notice.", "ok"),
    ("Either party may terminate for convenience on fifteen (15) days notice.", "moderate"),
    ("Either party may terminate without cause on two weeks' notice.", "moderate"),
    ("Either party may terminate for convenience upon three months written notice.", "ok"),
])
def test_written_out_and_bracketed_notice_periods_are_read(body, expected):
    assert playbook._check_termination(body)[0] == expected


def test_payment_terms_in_words():
    assert playbook.parse_net_days("Invoices are payable within forty-five (45) days.") == 45
    assert playbook.parse_net_days("due within sixty days of the invoice date") == 60


# ── The effective playbook and workspace rules ──────────────────────────────


@pytest.fixture
def workspace(ddb, monkeypatch):
    monkeypatch.delenv("PLAYBOOK_JSON", raising=False)
    return ddb


def test_thresholds_in_a_workspace_rule_change_what_counts_as_a_deviation(workspace):
    tenant = f"u-{SUB_A}"
    assert playbook._check_payment("Payable Net 45.")[0] == "minor"
    playbook.save_tenant_rule(tenant, "Payment", {"standard": "Net 45 from invoice.", "extra": {"netDays": 45.0},
                                                  "fallback": "Net 60 with CFO approval"})
    out = playbook.evaluate_clauses(clauses(), tenant_id=tenant)
    pay = next(r for r in out["clauseResults"] if r["clauseId"] == "c002")
    assert pay["outcome"] == "within" and pay["standard"] == "Net 45 from invoice."
    assert pay["fallback"] == "Net 60 with CFO approval"
    assert out["source"] == "custom"
    # another workspace is graded against the defaults
    other = playbook.evaluate_clauses(clauses(), tenant_id=f"u-{SUB_B}")
    assert next(r for r in other["clauseResults"] if r["clauseId"] == "c002")["outcome"] == "deviates"


def test_a_rule_for_a_custom_clause_type_uses_its_phrase_lists(workspace):
    tenant = f"u-{SUB_A}"
    playbook.save_tenant_rule(tenant, "type.non-solicitation", {
        "standard": "Mutual, limited to 12 months.", "requiredPhrases": ["12 months"],
        "forbiddenPhrases": ["in perpetuity"], "severity": "material"})
    out = playbook.evaluate_clauses(clauses(), tenant_id=tenant)
    row = next(r for r in out["clauseResults"] if r["clauseId"] == "c006")
    assert (row["outcome"], row["ruleId"], row["ruleName"]) == ("within", "type.non-solicitation", "Non solicitation")

    bad = [dict(clauses()[5], body="Neither party shall solicit the other's staff in perpetuity.")]
    row = playbook.evaluate_clauses(bad, tenant_id=tenant)["clauseResults"][0]
    assert (row["outcome"], row["severity"], row["found"]) == ("deviates", "material", '"in perpetuity"')
    missing = [dict(clauses()[5], body="Neither party shall solicit the other's staff.")]
    row = playbook.evaluate_clauses(missing, tenant_id=tenant)["clauseResults"][0]
    assert row["outcome"] == "deviates" and "12 months" in row["reason"]


def test_a_rule_with_nothing_to_check_is_flagged_never_passed(workspace):
    tenant = f"u-{SUB_A}"
    playbook.save_tenant_rule(tenant, "type.non-solicitation", {"standard": "Avoid if possible."})
    row = playbook.evaluate_clauses([clauses()[5]], tenant_id=tenant)["clauseResults"][0]
    assert (row["outcome"], row["status"]) == ("flagged", "review")
    assert "No automatic check" in row["reason"] and row["standard"] == "Avoid if possible."


@pytest.mark.parametrize("rule_id,body,problem", [
    ("NotACategory", {"standard": "x"}, "ruleId"),
    ("Other", {"standard": "x"}, "ruleId"),
    ("type.Bad Key!", {"standard": "x"}, "ruleId"),
    ("Payment", {"standard": "x", "evil": 1}, "Unknown field"),
    ("Payment", {"thresholds": {"netDays": "sixty"}}, "must be a number"),
    ("Payment", {"thresholds": {"capMultipleOfFees": 2}}, "Unknown threshold"),
    ("Payment", {"thresholds": {"netDays": -5}}, "must be a number"),
    ("type.exclusivity", {}, "standard"),
    ("Payment", {"requiredPhrases": "net 30"}, "lists"),
    ("Payment", {"forbiddenPhrases": ["x"] * 50}, "lists"),
    ("Payment", {"severity": "apocalyptic"}, "severity"),
    ("Payment", [1, 2], "JSON object"),
])
def test_rule_validation_rejects_bad_input(rule_id, body, problem):
    rule, error = playbook.validate_rule(rule_id, body)
    assert rule is None and problem in error


def test_rule_validation_accepts_and_cleans_good_input():
    rule, error = playbook.validate_rule("Payment", {
        "label": "  Payment  ", "standard": "Net 45.", "fallback": "Net 60 with approval",
        "thresholds": {"netDays": 45}, "forbiddenPhrases": [" pay when paid ", "pay when paid"]})
    assert error is None
    assert rule == {"label": "Payment", "standard": "Net 45.", "fallback": "Net 60 with approval",
                    "extra": {"netDays": 45.0}, "forbiddenPhrases": ["pay when paid"], "severity": "moderate"}


# ── API ──────────────────────────────────────────────────────────────────────


def call(method, path, sub, body=None):
    ev = {"rawPath": path, "body": json.dumps(body) if body is not None else None, "queryStringParameters": None}
    resp = api._route(method, path, ev, Caller.from_claims({"sub": sub}))
    return resp["statusCode"], json.loads(resp["body"])


def test_get_playbook_returns_the_effective_rules(workspace):
    status, out = call("GET", "/playbook", SUB_A)
    assert status == 200 and out["source"] == "default" and out["customRuleCount"] == 0
    rules = {r["ruleId"]: r for r in out["rules"]}
    assert len(rules) == 20 and "Payment" in rules and "Other" not in rules
    assert rules["Payment"] == {
        "ruleId": "Payment", "clauseType": "Payment", "isCustomType": False, "label": "Payment Terms",
        "standard": "Net 30 from invoice date.",
        "rationale": "Protects cash flow; longer terms are a financing cost.", "fallback": None,
        "thresholds": {"netDays": 30}, "requiredPhrases": [], "forbiddenPhrases": [],
        "phraseSeverity": "moderate", "source": "default", "hasAutomaticCheck": True, "hasBuiltInDefault": True,
    }


def test_put_and_delete_a_rule_through_the_api(workspace):
    status, out = call("PUT", "/playbook/rules/Payment", SUB_A,
                       {"standard": "Net 45 from invoice.", "thresholds": {"netDays": 45},
                        "fallback": "Net 60 with CFO approval"})
    assert status == 200 and out["rule"]["source"] == "custom" and out["rule"]["thresholds"] == {"netDays": 45.0}
    assert out["rule"]["fallback"] == "Net 60 with CFO approval"
    assert out["rule"]["rationale"] == "Protects cash flow; longer terms are a financing cost."   # default kept
    status, out = call("PUT", "/playbook/rules/type.non-solicitation", SUB_A,
                       {"standard": "Mutual, 12 months.", "requiredPhrases": ["12 months"]})
    assert status == 200 and out["rule"]["isCustomType"] is True and out["rule"]["hasBuiltInDefault"] is False

    listing = call("GET", "/playbook", SUB_A)[1]
    assert listing["source"] == "custom" and listing["customRuleCount"] == 2
    # one user's rules are theirs alone
    assert call("GET", "/playbook", SUB_B)[1]["customRuleCount"] == 0
    assert ("TENANT#u-" + SUB_A, "PLAYBOOK") in workspace.items

    status, out = call("DELETE", "/playbook/rules/Payment", SUB_A)
    assert status == 200 and out["rule"]["source"] == "default" and out["rule"]["thresholds"] == {"netDays": 30}
    status, out = call("DELETE", "/playbook/rules/type.non-solicitation", SUB_A)
    assert status == 200 and out["rule"] is None                      # a custom type has no rule again
    assert call("GET", "/playbook", SUB_A)[1]["customRuleCount"] == 0


def test_api_rejects_invalid_rules_and_wrong_methods(workspace):
    assert call("PUT", "/playbook/rules/Payment", SUB_A, {"thresholds": {"netDays": "x"}})[0] == 400
    assert call("PUT", "/playbook/rules/Nope", SUB_A, {"standard": "x"})[0] == 400
    assert call("DELETE", "/playbook/rules/Nope", SUB_A)[0] == 404
    assert call("POST", "/playbook", SUB_A, {})[0] == 405
    assert workspace.items == {}


def test_deployment_override_is_reported_as_such(workspace, monkeypatch):
    monkeypatch.setenv("PLAYBOOK_JSON", '{"Payment": {"standard": "Net 15 only", "extra": {"netDays": 15}}}')
    rules = {r["ruleId"]: r for r in call("GET", "/playbook", SUB_A)[1]["rules"]}
    assert rules["Payment"]["source"] == "deployment" and rules["Payment"]["thresholds"] == {"netDays": 15}
    assert playbook.resolve_positions(None)["Payment"].check is playbook._check_payment
    assert playbook._grade(playbook.resolve_positions(None)["Payment"], "Payable Net 30.")[0] == "minor"
