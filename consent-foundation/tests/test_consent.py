"""Tests for the data-release consent build (staged).

Run from the buildout root:  python -m pytest tests/ -q --capture=sys
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from consent.clause import CLAUSE_TEXT, CLAUSE_VERSION          # noqa: E402
from consent.gate import evaluate_onboarding                    # noqa: E402
from consent.store import ConsentStore, STATE_OPTED_IN, STATE_DECLINED, STATE_OPTED_OUT  # noqa: E402
from consent.cohorts import cohort_report, overall_totals       # noqa: E402
from consent.suppression import Cell, evaluate_cell, evaluate_dataset  # noqa: E402

UI = Path(__file__).resolve().parents[1] / "ui" / "onboarding_step.html"


def make_store():
    return ConsentStore()


def onboard(store, tid, month="2026-11", trade="hvac", plan="starter", opted_in=True):
    return store.record_onboarding_decision(
        tenant_id=tid, business_name=f"Shop {tid}", signup_month=month,
        trade=trade, plan_id=plan, opted_in=opted_in, at=f"{month}-10T00:00:00+00:00")


# -- the clause itself ------------------------------------------------------

def test_clause_text_is_the_owners_exact_words():
    assert CLAUSE_TEXT == (
        "I agree that Mind At Ease may use my business data in aggregated, "
        "anonymized form for marketing, product improvement, and licensed "
        "insights to third parties, with all identifying details removed."
    )
    assert CLAUSE_VERSION  # versioned, and records store it


def test_ui_shows_the_clause_verbatim():
    html = UI.read_text()
    assert CLAUSE_TEXT in html


# -- checkbox independence ---------------------------------------------------

def test_terms_accepted_clause_declined_still_creates_account():
    d = evaluate_onboarding(terms_accepted=True, data_release_checked=False)
    assert d.can_create_account is True
    assert d.data_release_opted_in is False


def test_clause_checked_terms_declined_still_blocks_account():
    d = evaluate_onboarding(terms_accepted=False, data_release_checked=True)
    assert d.can_create_account is False
    assert d.block_reason == "main terms not accepted"
    # the clause choice is carried, never treated as terms acceptance
    assert d.data_release_opted_in is True


def test_both_accepted_creates_account_with_opt_in():
    d = evaluate_onboarding(terms_accepted=True, data_release_checked=True)
    assert d.can_create_account is True and d.data_release_opted_in is True


def test_both_declined_blocks_only_on_terms():
    d = evaluate_onboarding(terms_accepted=False, data_release_checked=False)
    assert d.can_create_account is False and d.data_release_opted_in is False


# -- default unchecked --------------------------------------------------------

def test_checkbox_is_unchecked_by_default_in_the_ui():
    html = UI.read_text()
    m = re.search(r'<input id="datarelease"[^>]*>', html)
    assert m, "data-release checkbox missing from onboarding UI"
    assert "checked" not in m.group(0), "data-release checkbox must NOT be pre-ticked"


def test_terms_and_release_are_separate_inputs():
    html = UI.read_text()
    assert 'id="terms"' in html and 'id="datarelease"' in html
    # the submit button is wired to the terms state only
    assert "btn.disabled = !terms.checked" in html


# -- consent records -----------------------------------------------------------

def test_consent_record_written_on_opt_in():
    store = make_store()
    rec = onboard(store, "t1", opted_in=True)
    assert rec.state == STATE_OPTED_IN
    assert rec.clause_version == CLAUSE_VERSION
    assert rec.trade == "hvac" and rec.plan_id == "starter" and rec.signup_month == "2026-11"
    events = store.events("t1")
    assert [e["event"] for e in events] == ["presented_opt_in"]
    assert events[0]["surface"] == "onboarding"
    assert store.is_eligible_for_aggregation("t1") is True


def test_declined_record_written_and_never_eligible():
    store = make_store()
    rec = onboard(store, "t2", opted_in=False)
    assert rec.state == STATE_DECLINED
    assert store.is_eligible_for_aggregation("t2") is False


def test_opt_out_is_honored_and_logged():
    store = make_store()
    onboard(store, "t3", opted_in=True)
    assert store.is_eligible_for_aggregation("t3") is True
    rec = store.change_consent("t3", opted_in=False, surface="settings",
                               at="2026-12-01T00:00:00+00:00")
    assert rec.state == STATE_OPTED_OUT
    assert store.is_eligible_for_aggregation("t3") is False
    events = [e["event"] for e in store.events("t3")]
    assert events == ["presented_opt_in", "opt_out"]  # change of mind is on the record


def test_opt_back_in_after_opt_out_restores_eligibility_and_logs():
    store = make_store()
    onboard(store, "t4", opted_in=True)
    store.change_consent("t4", opted_in=False, at="2026-12-01T00:00:00+00:00")
    rec = store.change_consent("t4", opted_in=True, at="2026-12-05T00:00:00+00:00")
    assert rec.state == STATE_OPTED_IN
    assert store.is_eligible_for_aggregation("t4") is True
    assert [e["event"] for e in store.events("t4")] == ["presented_opt_in", "opt_out", "opt_in"]


# -- cohort math -----------------------------------------------------------------

def test_cohort_rate_math_on_seeded_records():
    store = make_store()
    # cohort A: 2026-11 / hvac / starter — 4 presented, 3 opt in, 1 of them later opts out
    for i, opt in enumerate([True, True, True, False]):
        onboard(store, f"a{i}", opted_in=opt)
    store.change_consent("a0", opted_in=False, at="2026-11-20T00:00:00+00:00")
    # cohort B: 2026-11 / roofing / pro — 2 presented, both decline
    for i in range(2):
        onboard(store, f"b{i}", trade="roofing", plan="pro", opted_in=False)

    rows = {(r["signup_month"], r["trade"], r["plan_id"]): r for r in cohort_report(store)}
    a = rows[("2026-11", "hvac", "starter")]
    assert a["presented"] == 4 and a["opted_in_now"] == 2 and a["declined"] == 1
    assert a["ever_opted_in"] == 3 and a["opted_out_ever"] == 1
    assert a["opt_in_rate"] == 0.5            # 2 of 4 currently in
    assert a["opt_out_rate"] == round(1 / 3, 4)  # 1 of the 3 who ever opted in withdrew

    b = rows[("2026-11", "roofing", "pro")]
    assert b["presented"] == 2 and b["opt_in_rate"] == 0.0 and b["opt_out_rate"] is None

    totals = overall_totals(list(rows.values()))
    assert totals["presented"] == 6 and totals["opted_in_now"] == 2


# -- suppression / re-identification --------------------------------------------

def test_suppression_fires_on_the_only_roofing_shop_in_town():
    # The named failure mode: one roofing business in a small town.
    cell = Cell(trade="roofing", metro="Smalltown (demo)", metro_population=8_400,
                market_trade_businesses=1, contributing_businesses=1,
                largest_contributor_share=1.0,
                attribute_bands={"revenue_band": "250k-500k"})
    v = evaluate_cell(cell)
    assert v.publishable is False
    joined = " ".join(v.flags)
    assert "single-business cell" in joined
    assert "SMALL-MARKET" in joined


def test_suppression_fires_on_small_cell_and_dominance():
    small = Cell(trade="hvac", metro="Mid-size (demo)", metro_population=210_000,
                 market_trade_businesses=140, contributing_businesses=4,
                 largest_contributor_share=0.30)
    assert evaluate_cell(small).publishable is False

    dominated = Cell(trade="plumbing", metro="Big City (demo)", metro_population=900_000,
                     market_trade_businesses=800, contributing_businesses=15,
                     largest_contributor_share=0.82)
    v = evaluate_cell(dominated)
    assert v.publishable is False and any("dominance" in f or "contributes 82%" in f for f in v.flags)


def test_healthy_big_market_cell_is_publishable():
    cell = Cell(trade="hvac", metro="Fort Worth", metro_population=980_000,
                market_trade_businesses=1_900, contributing_businesses=64,
                largest_contributor_share=0.06,
                attribute_bands={"revenue_band": "100k-250k"})
    v = evaluate_cell(cell)
    assert v.publishable is True and v.flags == []


def test_dataset_gate_blocks_whole_release_on_one_bad_cell():
    good = Cell(trade="hvac", metro="Fort Worth", metro_population=980_000,
                market_trade_businesses=1_900, contributing_businesses=64,
                largest_contributor_share=0.06)
    bad = Cell(trade="roofing", metro="Smalltown (demo)", metro_population=8_400,
               market_trade_businesses=1, contributing_businesses=1,
               largest_contributor_share=1.0)
    result = evaluate_dataset([good, bad])
    assert result["dataset_publishable"] is False
    assert result["cells_publishable"] == 1 and result["cells_flagged"] == 1
