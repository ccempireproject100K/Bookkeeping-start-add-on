"""Nudge + coverage-statement tests (owner additions, 2026-11-11 evening).

The nudge: a one-time pop-up per unchecked scope, shown when the user
tries to continue. It informs — it never manufactures consent.
The coverage statement: counts and rates only, floor-suppressed, with
the consent method stated on its face.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from consent.clause import SCOPE_LICENSING, SCOPE_MARKETING_IMPROVEMENT  # noqa: E402
from consent.coverage import (  # noqa: E402
    BELOW_FLOOR_LABEL,
    CONSENT_METHOD_STATEMENT,
    coverage_report,
)
from consent.gate import evaluate_onboarding  # noqa: E402
from consent.nudge import (  # noqa: E402
    ACCEPT_LABEL,
    DISMISS_LABEL,
    NUDGE_COPY,
    OWNER_RESPECT_LINE,
    NudgeSession,
    nudge_conversion,
)
from consent.store import ConsentStore  # noqa: E402
from consent.suppression import MIN_CELL_BUSINESSES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ONBOARDING_UI = ROOT / "ui" / "onboarding_step.html"
COVERAGE_UI = ROOT / "ui" / "coverage_report.html"

BOTH_OFF = {SCOPE_MARKETING_IMPROVEMENT: False, SCOPE_LICENSING: False}


def session(store, tid="nudge-tenant"):
    return NudgeSession(store=store, tenant_id=tid, session_id="sess-1")


# -- (a) once per scope per session -------------------------------------------

def test_nudge_fires_once_per_scope_then_never_again():
    store = ConsentStore()
    s = session(store)
    first = s.next_prompt(dict(BOTH_OFF))
    assert first is not None and first.scope == SCOPE_MARKETING_IMPROVEMENT
    s.dismiss(first.scope)
    second = s.next_prompt(dict(BOTH_OFF))
    assert second is not None and second.scope == SCOPE_LICENSING
    s.dismiss(second.scope)
    # Both scopes shown and dismissed: the flow proceeds, no third pop-up.
    assert s.next_prompt(dict(BOTH_OFF)) is None
    assert s.next_prompt(dict(BOTH_OFF)) is None
    shown = [e for e in store.all_events() if e["event"] == "nudge_shown"]
    assert len(shown) == 2  # exactly once per scope


def test_nudge_is_per_session_a_fresh_session_may_show_again():
    store = ConsentStore()
    s1 = session(store)
    s1.dismiss(s1.next_prompt(dict(BOTH_OFF)).scope)
    s2 = NudgeSession(store=store, tenant_id="nudge-tenant", session_id="sess-2")
    assert s2.next_prompt(dict(BOTH_OFF)) is not None


# -- (d) respects the split -----------------------------------------------------

def test_checked_scope_never_nudges_and_both_checked_means_no_nudge():
    store = ConsentStore()
    s = session(store)
    prompt = s.next_prompt({SCOPE_MARKETING_IMPROVEMENT: True, SCOPE_LICENSING: False})
    assert prompt is not None and prompt.scope == SCOPE_LICENSING

    store2 = ConsentStore()
    s2 = session(store2, "both-on")
    assert s2.next_prompt({SCOPE_MARKETING_IMPROVEMENT: True, SCOPE_LICENSING: True}) is None
    assert store2.all_events() == []  # no nudge, no log noise


# -- (b) never checks a box by itself -------------------------------------------

def test_showing_a_nudge_changes_no_checkbox_and_no_consent_state():
    store = ConsentStore()
    s = session(store)
    checked = dict(BOTH_OFF)
    s.next_prompt(checked)
    assert checked == BOTH_OFF  # the caller's checkbox state is untouched
    assert store.get("nudge-tenant") is None  # no consent record created
    assert store.all_scope_records() == []


def test_only_the_explicit_accept_tap_checks_the_box():
    store = ConsentStore()
    s = session(store)
    prompt = s.next_prompt(dict(BOTH_OFF))
    # Dismissal path: no acceptance is recorded, consent stays off.
    s.dismiss(prompt.scope)
    events = [e["event"] for e in store.all_events(scope=prompt.scope)]
    assert events == ["nudge_shown", "nudge_dismissed"]

    store2 = ConsentStore()
    s2 = session(store2, "acceptor")
    p2 = s2.next_prompt(dict(BOTH_OFF))
    assert s2.accept(p2.scope) == p2.scope  # returns the scope for the tap to check
    events2 = [e["event"] for e in store2.all_events(scope=p2.scope)]
    assert events2 == ["nudge_shown", "nudge_accepted"]


def test_accept_or_dismiss_without_a_shown_prompt_is_refused():
    store = ConsentStore()
    s = session(store)
    for fn in (s.accept, s.dismiss):
        try:
            fn(SCOPE_LICENSING)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


# -- (c) never blocks account creation -------------------------------------------

def test_after_dismissal_the_flow_proceeds_exactly_as_before():
    store = ConsentStore()
    s = session(store)
    while (p := s.next_prompt(dict(BOTH_OFF))) is not None:
        s.dismiss(p.scope)
    decision = evaluate_onboarding(True, False, False)
    assert decision.can_create_account is True  # terms alone still gate


# -- (e) logged, and conversion is measurable -------------------------------------

def test_nudge_events_land_in_the_append_only_log_with_session_reason():
    store = ConsentStore()
    s = session(store)
    p = s.next_prompt(dict(BOTH_OFF))
    s.accept(p.scope)
    events = store.all_events(scope=p.scope)
    assert [e["event"] for e in events] == ["nudge_shown", "nudge_accepted"]
    assert all(e["surface"] == "onboarding" for e in events)
    assert all(e["reason"] == "session:sess-1" for e in events)


def test_nudge_conversion_counts_distinct_tenants_per_scope():
    store = ConsentStore()
    for tid, act in (("conv-a", "accept"), ("conv-b", "dismiss"), ("conv-c", "accept")):
        s = NudgeSession(store=store, tenant_id=tid, session_id=f"s-{tid}")
        p = s.next_prompt(dict(BOTH_OFF))
        (s.accept if act == "accept" else s.dismiss)(p.scope)
    report = nudge_conversion(store)
    a = report[SCOPE_MARKETING_IMPROVEMENT]
    assert a["shown_tenants"] == 3 and a["accepted_tenants"] == 2
    assert a["dismissed_tenants"] == 1 and a["accept_rate"] == round(2 / 3, 4)
    # Nobody reached the licensing prompt in this flow.
    assert report[SCOPE_LICENSING]["shown_tenants"] == 0
    assert report[SCOPE_LICENSING]["accept_rate"] is None


# -- (f) + the owner's line, in the copy and on the page ----------------------------

def test_owner_respect_line_is_verbatim_in_copy_and_ui():
    assert OWNER_RESPECT_LINE == (
        "It's 100% your choice and we respect your decision either way."
    )
    for copy in NUDGE_COPY.values():
        assert copy.owner_line == OWNER_RESPECT_LINE
        assert copy.accept_label == ACCEPT_LABEL == "Yes, count me in"
        assert copy.dismiss_label == DISMISS_LABEL == "No thanks, continue"
        assert "benchmark report" in copy.body
    html = ONBOARDING_UI.read_text()
    assert OWNER_RESPECT_LINE in html
    assert ACCEPT_LABEL in html and DISMISS_LABEL in html
    # Both buttons share the one nudge-btn class (equal prominence), and the
    # respect line sits in the pop-up body, before the buttons.
    assert 'id="nudge-accept" class="nudge-btn accept"' in html
    assert 'id="nudge-dismiss" class="nudge-btn dismiss"' in html
    assert html.index(OWNER_RESPECT_LINE) < html.index(ACCEPT_LABEL)
    # No countdown / pressure machinery in the pop-up.
    assert "countdown" not in html.lower() and "hurry" not in html.lower()


# -- coverage statement -------------------------------------------------------------

def seed_coverage(store):
    """hvac x Fort Worth: 12 shown, 7 in (A) / 5 in (B).
    roofing x Smalltown: 4 shown — below the reporting floor."""
    n = 0
    for trade, metro, presented, a_in, b_in in (
        ("hvac", "Fort Worth", 12, 7, 5),
        ("roofing", "Smalltown", 4, 3, 2),
    ):
        for i in range(presented):
            n += 1
            store.record_onboarding_decisions(
                tenant_id=f"cov-{n:02d}",
                business_name=f"Cov Shop {n}",
                signup_month="2026-11", trade=trade, plan_id="starter",
                marketing_improvement_opted_in=i < a_in,
                licensing_opted_in=i < b_in,
                at="2026-11-15T12:00:00+00:00",
                metro=metro,
            )


def test_coverage_math_on_a_seeded_cohort():
    store = ConsentStore()
    seed_coverage(store)
    payload = coverage_report(store, scope=SCOPE_MARKETING_IMPROVEMENT)
    assert payload["consent_method"] == CONSENT_METHOD_STATEMENT == (
        "active opt-in, unchecked by default"
    )
    hvac = next(r for r in payload["rows"] if r["trade"] == "hvac")
    assert hvac["presented"] == 12 and hvac["opted_in"] == 7
    assert hvac["opt_in_rate_pct"] == 58.3 and hvac["suppressed"] is False
    # Rollups cover reportable cells only: the small cell is excluded.
    assert payload["excluded_below_floor_cells"] == 1
    overall = payload["rollups"]["overall"]
    assert overall["presented"] == 12 and overall["opted_in"] == 7
    assert overall["opt_in_rate_pct"] == 58.3
    assert {r["trade"] for r in payload["rollups"]["by_trade"]} == {"hvac"}
    b = coverage_report(store, scope=SCOPE_LICENSING)
    hvac_b = next(r for r in b["rows"] if r["trade"] == "hvac")
    assert hvac_b["opted_in"] == 5 and hvac_b["opt_in_rate_pct"] == 41.7


def test_coverage_floor_suppression_hides_the_small_cell_counts():
    assert MIN_CELL_BUSINESSES == 10  # the existing floor, unchanged
    store = ConsentStore()
    seed_coverage(store)
    payload = coverage_report(store, scope=SCOPE_MARKETING_IMPROVEMENT)
    small = next(r for r in payload["rows"] if r["metro"] == "Smalltown")
    assert small["suppressed"] is True
    assert small["presented"] == BELOW_FLOOR_LABEL
    assert small["opted_in"] == BELOW_FLOOR_LABEL
    assert small["opt_in_rate_pct"] == BELOW_FLOOR_LABEL
    blob = json.dumps(payload)
    assert '"presented": 4' not in blob and '"opted_in": 3' not in blob


def test_coverage_payload_carries_no_tenant_identifiers():
    store = ConsentStore()
    seed_coverage(store)
    for scope in (SCOPE_MARKETING_IMPROVEMENT, SCOPE_LICENSING):
        blob = json.dumps(coverage_report(store, scope=scope))
        for n in range(1, 17):
            assert f"cov-{n:02d}" not in blob  # no tenant ids
            assert f"Cov Shop {n}" not in blob  # no business names
        assert "tenant" not in blob.lower().replace("tenants", "")


def test_coverage_page_states_the_method_and_the_floor_label():
    html = COVERAGE_UI.read_text()
    assert CONSENT_METHOD_STATEMENT in html
    assert BELOW_FLOOR_LABEL in html
    assert "Cov Shop" not in html and "coverage-demo-" not in html
