"""Improvement-pass tests: split consent, versioning, explainer, receipt."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from consent.clause import (  # noqa: E402
    LICENSING_CLAUSE_IS_ROUTER_DRAFT,
    LICENSING_CLAUSE_TEXT,
    LICENSING_CLAUSE_VERSION,
    SCOPE_LICENSING,
    SCOPE_MARKETING_IMPROVEMENT,
    WHAT_NEVER_LEAVES,
    WHAT_THIS_MEANS_ACCOUNT_DELETION,
    WHAT_THIS_MEANS_EXAMPLES,
    WHAT_THIS_MEANS_GIVE_BACK,
)
from consent.cohorts import cohort_report, overall_totals  # noqa: E402
from consent.gate import evaluate_onboarding  # noqa: E402
from consent.store import (  # noqa: E402
    ACCOUNT_DELETED_REASON,
    DEMO_RELEASE_LABEL,
    ConsentStore,
    STATE_ACCOUNT_DELETED,
    STATE_DECLINED,
    STATE_OPTED_IN,
    STATE_OPTED_OUT,
    STATE_RECONFIRMATION_REQUIRED,
    STATE_RECONSENT_REQUIRED,
)

ROOT = Path(__file__).resolve().parents[1]
ONBOARDING_UI = ROOT / "ui" / "onboarding_step.html"
SETTINGS_UI = ROOT / "ui" / "settings_privacy.html"


def onboard_both(store, tid, marketing=True, licensing=True):
    return store.record_onboarding_decisions(
        tenant_id=tid,
        business_name=f"Shop {tid}",
        signup_month="2026-11",
        trade="hvac",
        plan_id="starter",
        marketing_improvement_opted_in=marketing,
        licensing_opted_in=licensing,
        at="2026-11-10T00:00:00+00:00",
    )


# -- split clauses and gate independence -------------------------------------

def test_licensing_clause_is_independent_and_marked_router_draft():
    assert LICENSING_CLAUSE_VERSION
    assert LICENSING_CLAUSE_IS_ROUTER_DRAFT is True
    assert LICENSING_CLAUSE_TEXT == (
        "I agree that Mind At Ease may license aggregated, anonymized insights "
        "based on my business data to third parties, with all identifying "
        "details removed."
    )


def test_gate_carries_both_consents_independently_and_terms_alone_gates():
    for marketing in (False, True):
        for licensing in (False, True):
            allowed = evaluate_onboarding(True, marketing, licensing)
            assert allowed.can_create_account is True
            assert allowed.data_release_opted_in is marketing
            assert allowed.licensing_opted_in is licensing

            blocked = evaluate_onboarding(False, marketing, licensing)
            assert blocked.can_create_account is False
            assert blocked.data_release_opted_in is marketing
            assert blocked.licensing_opted_in is licensing


def test_dual_records_and_eligibility_are_independent():
    store = ConsentStore()
    records = onboard_both(store, "split-a", marketing=True, licensing=False)
    assert records[SCOPE_MARKETING_IMPROVEMENT].state == STATE_OPTED_IN
    assert records[SCOPE_LICENSING].state == STATE_DECLINED
    assert store.is_eligible_for_marketing_improvement("split-a") is True
    assert store.is_eligible_for_licensing("split-a") is False

    onboard_both(store, "split-b", marketing=False, licensing=True)
    assert store.is_eligible_for_marketing_improvement("split-b") is False
    assert store.is_eligible_for_licensing("split-b") is True


def test_changing_one_scope_does_not_change_the_other():
    store = ConsentStore()
    onboard_both(store, "split-change", marketing=True, licensing=True)
    changed = store.change_scope_consent(
        "split-change", SCOPE_LICENSING, False, at="2026-12-01T00:00:00+00:00"
    )
    assert changed.state == STATE_OPTED_OUT
    assert store.is_eligible_for_licensing("split-change") is False
    assert store.is_eligible_for_marketing_improvement("split-change") is True
    assert [e["event"] for e in store.events("split-change", SCOPE_LICENSING)] == [
        "presented_opt_in", "opt_out"
    ]
    assert [e["event"] for e in store.events("split-change", SCOPE_MARKETING_IMPROVEMENT)] == [
        "presented_opt_in"
    ]


def test_cohort_reports_are_independent_by_scope():
    store = ConsentStore()
    onboard_both(store, "cohort-a", marketing=True, licensing=False)
    onboard_both(store, "cohort-b", marketing=True, licensing=False)

    marketing = overall_totals(cohort_report(store, scope=SCOPE_MARKETING_IMPROVEMENT))
    licensing = overall_totals(cohort_report(store, scope=SCOPE_LICENSING))
    assert marketing["presented"] == 2 and marketing["opted_in_now"] == 2
    assert licensing["presented"] == 2 and licensing["opted_in_now"] == 0
    assert licensing["opt_in_rate"] == 0.0


# -- no silent carry-over ------------------------------------------------------

def test_version_change_requires_reconsent_for_that_scope_only():
    store = ConsentStore()
    onboard_both(store, "versioned", marketing=True, licensing=True)
    old_licensing_version = store.current_clause_version(SCOPE_LICENSING)

    store.publish_clause_version(
        SCOPE_LICENSING, "v2-2027-01-15", at="2027-01-15T00:00:00+00:00"
    )

    licensing = store.get_scope("versioned", SCOPE_LICENSING)
    assert licensing.state == STATE_RECONSENT_REQUIRED
    assert licensing.clause_version == old_licensing_version  # version actually accepted
    assert store.is_eligible_for_licensing("versioned") is False
    cohort = overall_totals(cohort_report(store, scope=SCOPE_LICENSING))
    assert cohort["presented"] == 1
    assert cohort["opted_in_now"] == 0
    assert cohort["opt_in_rate"] == 0.0
    rows = cohort_report(store, scope=SCOPE_LICENSING)
    assert rows[0]["reconsent_required"] == 1
    # A report filtered to the new version has no v2 decisions yet.
    assert overall_totals(
        cohort_report(store, scope=SCOPE_LICENSING, clause_version="v2-2027-01-15")
    )["presented"] == 0

    # Scope A is untouched by Scope B's version publication.
    assert store.is_eligible_for_marketing_improvement("versioned") is True

    renewed = store.change_scope_consent(
        "versioned", SCOPE_LICENSING, True, at="2027-01-16T00:00:00+00:00"
    )
    assert renewed.state == STATE_OPTED_IN
    assert renewed.clause_version == "v2-2027-01-15"
    assert store.is_eligible_for_licensing("versioned") is True
    assert overall_totals(cohort_report(store, scope=SCOPE_LICENSING))["opted_in_now"] == 1
    assert [e["event"] for e in store.events("versioned", SCOPE_LICENSING)] == [
        "presented_opt_in",
        "clause_version_changed_reconsent_required",
        "reconsent_opt_in",
    ]


def test_version_change_does_not_create_eligibility_from_a_decline():
    store = ConsentStore()
    onboard_both(store, "version-declined", marketing=False, licensing=False)
    store.publish_clause_version(
        SCOPE_MARKETING_IMPROVEMENT, "v2-2027-02-01", at="2027-02-01T00:00:00+00:00"
    )
    assert store.get_scope("version-declined", SCOPE_MARKETING_IMPROVEMENT).state == STATE_DECLINED
    assert store.is_eligible_for_marketing_improvement("version-declined") is False


# -- contributor receipt --------------------------------------------------------

def test_demo_registry_only_attaches_scopes_the_tenant_opted_into():
    store = ConsentStore()
    onboard_both(store, "receipt-split", marketing=True, licensing=False)
    store.seed_demo_release_registry(["receipt-split"])
    receipt = store.contributor_receipt("receipt-split")
    assert [r.scope for r in receipt["releases"]] == [SCOPE_MARKETING_IMPROVEMENT]


def test_contributor_receipt_uses_labeled_demo_registry_and_leave_is_scoped():
    store = ConsentStore()
    onboard_both(store, "receipt", marketing=True, licensing=True)
    store.seed_demo_release_registry(["receipt"])

    receipt = store.contributor_receipt("receipt")
    assert len(receipt["releases"]) == 2
    assert all(r.is_demo for r in receipt["releases"])
    assert all(r.demo_label == DEMO_RELEASE_LABEL for r in receipt["releases"])
    assert all(r.benchmark_report_entitlement == "Your benchmark report" for r in receipt["releases"])
    assert all("trade × metro" in r.benchmark_report_entitlement_detail for r in receipt["releases"])
    assert all("report delivery wired later" in r.benchmark_report_delivery_status for r in receipt["releases"])
    assert receipt["consents"][SCOPE_MARKETING_IMPROVEMENT]["eligible_now"] is True
    assert receipt["consents"][SCOPE_LICENSING]["eligible_now"] is True

    store.change_scope_consent(
        "receipt", SCOPE_LICENSING, False, surface="settings",
        at="2026-12-10T00:00:00+00:00",
    )
    after = store.contributor_receipt("receipt")
    assert after["consents"][SCOPE_LICENSING]["eligible_now"] is False
    assert after["consents"][SCOPE_MARKETING_IMPROVEMENT]["eligible_now"] is True
    # Past seeded receipt entries remain visible as history; leaving changes
    # future eligibility only and is appended to the log.
    assert len(after["releases"]) == 2
    assert store.events("receipt", SCOPE_LICENSING)[-1]["event"] == "opt_out"


# -- account-deletion propagation -------------------------------------------------

def test_account_deletion_excludes_both_scopes_and_recomputes_cohorts():
    store = ConsentStore()
    onboard_both(store, "delete-me", marketing=True, licensing=True)
    onboard_both(store, "keep-me", marketing=True, licensing=True)

    before_a = overall_totals(cohort_report(store, scope=SCOPE_MARKETING_IMPROVEMENT))
    before_b = overall_totals(cohort_report(store, scope=SCOPE_LICENSING))
    assert before_a["presented"] == 2 and before_a["opted_in_now"] == 2
    assert before_b["presented"] == 2 and before_b["opted_in_now"] == 2

    deleted = store.delete_account("delete-me", at="2027-01-05T00:00:00+00:00")
    assert deleted.state == STATE_ACCOUNT_DELETED
    for scope in (SCOPE_MARKETING_IMPROVEMENT, SCOPE_LICENSING):
        assert store.get_scope("delete-me", scope).state == STATE_ACCOUNT_DELETED
        assert store.is_eligible_for_aggregation("delete-me", scope) is False
        event = store.events("delete-me", scope)[-1]
        assert event["event"] == "account_deleted"
        assert event["reason"] == ACCOUNT_DELETED_REASON == "account deleted"

        totals = overall_totals(cohort_report(store, scope=scope))
        assert totals["presented"] == 1  # deleted tenant is out of the rate base
        assert totals["opted_in_now"] == 1
        assert totals["opt_in_rate"] == 1.0

    assert store.contributor_receipt("delete-me")["account_deleted"] is True


# -- yearly licensing re-confirmation ----------------------------------------------

def test_annual_reconfirmation_answered_before_anniversary_continues_both_scopes():
    store = ConsentStore()
    onboard_both(store, "annual-answered", marketing=True, licensing=True)
    assert store.annual_reconfirmation_due_at("annual-answered") == "2027-11-10T00:00:00+00:00"

    answered = store.record_annual_reconfirmation(
        "annual-answered", True, at="2027-10-01T00:00:00+00:00"
    )
    assert answered.state == STATE_OPTED_IN
    # The injectable clock crosses the ORIGINAL anniversary after the answer;
    # the new cycle runs from the answer date, so nothing lapses.
    assert store.apply_annual_reconfirmation_check(
        "annual-answered", now="2027-11-10T00:00:00+00:00"
    ) == []
    assert store.is_eligible_for_marketing_improvement("annual-answered") is True
    assert store.is_eligible_for_licensing("annual-answered") is True
    assert store.events("annual-answered", SCOPE_LICENSING)[-1]["event"] == (
        "annual_reconfirmation_confirmed"
    )


def test_annual_reconfirmation_unanswered_past_anniversary_pauses_licensing_only():
    store = ConsentStore()
    onboard_both(store, "annual-lapsed", marketing=True, licensing=True)

    assert store.apply_annual_reconfirmation_check(
        "annual-lapsed", now="2027-11-09T23:59:59+00:00"
    ) == []
    assert store.apply_annual_reconfirmation_check(
        "annual-lapsed", now="2027-11-10T00:00:00+00:00"
    ) == ["annual-lapsed"]

    licensing = store.get_scope("annual-lapsed", SCOPE_LICENSING)
    assert licensing.state == STATE_RECONFIRMATION_REQUIRED
    assert store.is_eligible_for_licensing("annual-lapsed") is False
    # Scope A is exactly as the tenant left it.
    assert store.get_scope("annual-lapsed", SCOPE_MARKETING_IMPROVEMENT).state == STATE_OPTED_IN
    assert store.is_eligible_for_marketing_improvement("annual-lapsed") is True

    rows = cohort_report(store, scope=SCOPE_LICENSING)
    assert rows[0]["reconfirmation_required"] == 1
    assert rows[0]["opted_in_now"] == 0
    event = store.events("annual-lapsed", SCOPE_LICENSING)[-1]
    assert event["event"] == "annual_reconfirmation_lapsed"
    assert event["reason"] == "annual re-confirmation unanswered"
    receipt = store.contributor_receipt("annual-lapsed")
    assert receipt["annual_reconfirmation"]["pending"] is True

    # Re-confirming after the lapse restores licensing for a new cycle.
    restored = store.record_annual_reconfirmation(
        "annual-lapsed", True, at="2027-11-12T00:00:00+00:00"
    )
    assert restored.state == STATE_OPTED_IN
    assert store.is_eligible_for_licensing("annual-lapsed") is True


# -- UI surfaces -----------------------------------------------------------------

def test_onboarding_ui_has_two_unchecked_checkboxes_and_explainer():
    html = ONBOARDING_UI.read_text()
    for checkbox_id in ("datarelease", "licensingrelease"):
        match = re.search(rf'<input id="{checkbox_id}"[^>]*>', html)
        assert match, f"{checkbox_id} checkbox missing"
        assert "checked" not in match.group(0)
    assert LICENSING_CLAUSE_TEXT in html
    assert "ROUTER-DRAFTED" in html and "awaiting the owner's stamp" in html
    assert '<details id="what-this-means">' in html
    for phrase in WHAT_THIS_MEANS_EXAMPLES + WHAT_NEVER_LEAVES:
        assert phrase.rstrip(".").lower() in html.lower()
    assert WHAT_THIS_MEANS_GIVE_BACK in html
    assert WHAT_THIS_MEANS_ACCOUNT_DELETION in html


def test_settings_ui_shows_demo_receipt_and_per_scope_leave_controls():
    html = SETTINGS_UI.read_text()
    assert DEMO_RELEASE_LABEL in html
    assert "Demo Metro Services Overview" in html
    assert "Demo Licensed Financing Insights" in html
    assert html.count("Your benchmark report") >= 2
    assert html.count("report delivery wired later") >= 2
    assert 'id="leave-marketing"' in html
    assert 'id="leave-licensing"' in html
    assert LICENSING_CLAUSE_TEXT in html
    assert 'id="annual-reconfirmation-status"' in html
    assert "RE-CONFIRMATION REQUIRED" in html
    assert WHAT_THIS_MEANS_ACCOUNT_DELETION in html
