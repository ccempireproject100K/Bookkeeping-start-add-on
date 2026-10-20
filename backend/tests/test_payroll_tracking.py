"""Payroll TRACKING engine tests — real gross-to-net goldens + liability flow.

Every expected number below was computed BY HAND from the published tables
(IRS Pub 15-T 2026 Worksheet 1A + Percentage Method Tables; CA EDD Method B;
NYS-50-T-NYS Method II) and the hand computation is shown in each test — the
tests pin the engine to the publications, not to itself.

No live MongoDB needed (fully mocked, same fakes as test_payroll_polish.py).
    python -m pytest tests/test_payroll_tracking.py
"""
import asyncio
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from lib import payroll_calc as PC
from lib import payroll_config_2026 as CFG
from routers import payroll
from routers.payroll import LiabilityPatchIn, RunIn, RunItemIn, _build_item

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def paycheck(**kw):
    kw.setdefault("frequency", "biweekly")
    kw.setdefault("w4", {"filing_status": "single"})
    kw.setdefault("profile", {"work_state": None})
    kw.setdefault("deductions_in", [])
    kw.setdefault("ytd", {})
    kw.setdefault("tenant_suta_rates", {})
    return PC.compute_paycheck(**kw)


def tax_line(res, code):
    return next(t for t in res["taxes"] if t["code"] == code)


# =================================================================== federal

def test_config_constants_2026():
    fed = CFG.FEDERAL
    assert fed["ss_wage_base_cents"] == 184_500_00      # SSA/IRS Topic 751
    assert fed["ss_rate"] == 0.062 and fed["medicare_rate"] == 0.0145
    assert fed["additional_medicare_threshold_cents"] == 200_000_00
    assert fed["futa_wage_base_cents"] == 7_000_00
    assert CFG.CONFIG_VERSION and CFG.LAST_VERIFIED == "2026-10-20"


def test_federal_withholding_pub15t_golden():
    # Single, $3,000 biweekly. Annualized $78,000; line 1g -$8,600 ->
    # adjusted $69,400. Single standard table: $5,800 + 22% x ($69,400 -
    # $57,900) = $5,800 + $2,530 = $8,330/yr -> /26 = $320.38.
    res = paycheck(gross_cents=300_000)
    assert res["federal_withholding_cents"] == 32_038
    wp = tax_line(res, "federal_withholding")["workpaper"]
    assert wp["adjusted_annual_wages_cents"] == 6_940_000
    assert wp["tentative_annual_tax_cents"] == 833_000


def test_federal_step2_checkbox_uses_checkbox_schedule_not_a_divide():
    # MFJ, $3,000 biweekly, Step 2 box checked: line 1g = $0, adjusted
    # $78,000 on the CHECKBOX schedule: $5,800 + 22% x ($78,000 - $66,500)
    # = $8,330/yr -> $320.38/period.
    checked = paycheck(gross_cents=300_000,
                       w4={"filing_status": "married_filing_jointly",
                           "multiple_jobs": True})
    assert checked["federal_withholding_cents"] == 32_038
    # Unchecked MFJ: line 1g -$12,900 -> adjusted $65,100 on the STANDARD
    # schedule: $2,480 + 12% x ($65,100 - $44,100) = $5,000/yr -> $192.31.
    unchecked = paycheck(gross_cents=300_000,
                         w4={"filing_status": "married_filing_jointly"})
    assert unchecked["federal_withholding_cents"] == 19_231


def test_federal_w4_steps_3_and_4c():
    # Base case above ($320.38) minus Step 3 dependents $2,000/yr (/26 =
    # $76.92) plus Step 4(c) extra $50/period -> $293.46.
    res = paycheck(gross_cents=300_000,
                   w4={"filing_status": "single",
                       "dependents_cents": 200_000,
                       "extra_withholding_cents": 5_000})
    assert res["federal_withholding_cents"] == 29_346


def test_hourly_overtime_split_and_pay():
    member = {"hourly_rate_cents": 3000, "salary_monthly_cents": 0,
              "pay_type": "hourly"}
    item = _build_item(member, "biweekly",
                       RunItemIn(user_id="u9", hours=90.0), 0,
                       weekly_hours=[45.0, 45.0])
    assert item["regular_hours"] == 80.0 and item["overtime_hours"] == 10.0
    # gross = 80 x $30 + 10 x $45 = $2,850
    assert item["gross_cents"] == 285_000
    # Fed: annualized $74,100 - $8,600 = $65,500 -> $5,800 + 22% x $7,600
    # = $7,472/yr -> /26 = $287.38. FICA: SS $176.70 + Med $41.33.
    assert item["federal_withholding_cents"] == 28_738
    assert item["employee_fica_cents"] == 17_670 + 4_133
    assert item["net_cents"] == 285_000 - 28_738 - 21_803
    # Employer: FICA match + FUTA 0.6% net on first $7,000.
    assert item["employer"]["futa_cents"] == 1_710
    assert item["employer_cost_cents"] == 285_000 + 21_803 + 1_710


def test_salary_crossing_ss_cap_and_additional_medicare():
    # $25,000/mo salary; YTD FICA wages already $180,000.
    # SS: only $4,500 of room under the $184,500 base -> 6.2% = $279.00.
    # Medicare: 1.45% x $25,000 = $362.50. Additional Medicare: cumulative
    # wages cross $200,000 by $5,000 -> 0.9% = $45.00.
    res = paycheck(gross_cents=2_500_000, frequency="monthly",
                   ytd={"gross_cents": 18_000_000, "fica_wages_cents": 18_000_000})
    assert tax_line(res, "social_security")["wages_cents"] == 450_000
    assert tax_line(res, "social_security")["amount_cents"] == 27_900
    assert tax_line(res, "medicare")["amount_cents"] == 36_250
    assert tax_line(res, "additional_medicare")["amount_cents"] == 4_500
    assert res["employer"]["fica_ss_cents"] == 27_900  # employer capped too
    # Federal W/H: annualized $300,000 - $8,600 = $291,400 -> $58,448 +
    # 35% x $27,675 = $68,134.25/yr -> /12 = $5,677.85.
    assert res["federal_withholding_cents"] == 567_785
    assert any("wage base reached" in w for w in res["warnings"])


def test_pretax_treatments_fit_vs_fica():
    # 401(k) 10% of $3,000 = $300 reduces FIT wages only; $100 Section 125
    # premium reduces BOTH. FIT wages $2,600; FICA wages $2,900.
    res = paycheck(gross_cents=300_000, deductions_in=[
        {"label": "401(k)", "category": "pretax_retirement",
         "kind": "percent", "value": 10},
        {"label": "Health premium", "category": "pretax_section125",
         "kind": "amount", "amount_cents": 10_000},
    ])
    assert res["fit_wages_cents"] == 260_000
    assert res["fica_wages_cents"] == 290_000
    assert tax_line(res, "social_security")["amount_cents"] == 17_980  # 6.2% x 2900


def test_futa_wage_base_exhaustion():
    res = paycheck(gross_cents=300_000, ytd={"gross_cents": 650_000})
    # Only $500 of the $7,000 base remains: 6.0% gross $30, credit $27, net $3.
    assert res["employer"]["futa_taxable_wages_cents"] == 50_000
    assert res["employer"]["futa_cents"] == 300


# ====================================================================== CA

def test_ca_method_b_golden():
    # CA single, $3,000 biweekly, 1 regular DE 4 allowance.
    # Annual PIT wages $78,000 (over the $18,896 low-income threshold).
    # Taxable = $78,000 - $0 est. deductions - $5,706 standard = $72,294.
    # Table 5: $2,186.15 + 8.8% x ($72,294 - $57,542) = $3,484.33.
    # Less exemption credit $168.30 -> $3,316.03/yr -> /26 = $127.54.
    res = paycheck(gross_cents=300_000,
                   profile={"work_state": "CA", "filing_status": "single",
                            "state_allowances": 1})
    line = tax_line(res, "state_withholding")
    assert line["workpaper"]["annual_tax_cents"] == 348_433
    assert line["amount_cents"] == 12_754
    # CA UI 3.4% on first $7,000 + ETT 0.1%.
    assert res["employer"]["suta_cents"] == 10_200
    assert res["employer"]["ett_cents"] == 300


def test_ca_401k_does_not_reduce_ca_wages():
    res = paycheck(gross_cents=300_000,
                   profile={"work_state": "CA", "filing_status": "single",
                            "state_allowances": 0},
                   deductions_in=[{"label": "401(k)",
                                   "category": "pretax_retirement",
                                   "kind": "percent", "value": 10}])
    # Federal FIT wages drop to $2,700; CA PIT wages stay $3,000 (CA does
    # not conform to the 401(k) exclusion for PIT withholding).
    assert res["fit_wages_cents"] == 270_000
    assert tax_line(res, "state_withholding")["wages_cents"] == 300_000


def test_ca_low_income_exemption_zeroes_withholding():
    # $300/week single -> $15,600/yr, under the $18,896 Table 1 threshold.
    res = paycheck(gross_cents=30_000, frequency="weekly",
                   profile={"work_state": "CA", "filing_status": "single",
                            "state_allowances": 0})
    assert tax_line(res, "state_withholding")["amount_cents"] == 0
    assert tax_line(res, "state_withholding")["status"] == "ok"


# ====================================================================== NY

def test_ny_method_ii_golden():
    # NY single, $3,000 biweekly, 0 exemptions. Net wages = $78,000 -
    # $7,400 deduction allowance = $70,600. Method II single schedule:
    # $586 + 5.4% x ($70,600 - $13,900) = $3,647.80/yr -> /26 = $140.30.
    res = paycheck(gross_cents=300_000,
                   profile={"work_state": "NY", "filing_status": "single",
                            "state_allowances": 0})
    line = tax_line(res, "state_withholding")
    assert line["workpaper"]["method"] == "ny_method_ii"
    assert line["amount_cents"] == 14_030
    # NY UI new employer 4.100% total on the $17,600 wage base.
    assert res["employer"]["suta_cents"] == 12_300
    assert res["employer"]["suta"]["wage_base_cents"] == 17_600_00


def test_ny_head_of_household_uses_single_schedule():
    res = paycheck(gross_cents=300_000,
                   profile={"work_state": "NY",
                            "filing_status": "head_of_household",
                            "state_allowances": 0})
    line = tax_line(res, "state_withholding")
    assert line["workpaper"]["filing_status"] == "single"
    assert line["amount_cents"] == 14_030  # same as the single case


# ======================================================== states / garnishment

def test_unconfigured_state_is_explicit_not_zero():
    res = paycheck(gross_cents=300_000,
                   profile={"work_state": "PA", "filing_status": "single"})
    assert res["state_withholding_status"] == "NOT_IMPLEMENTED"
    assert res["state_withholding_cents"] == 0
    assert any("State withholding NOT_IMPLEMENTED for PA" in w
               for w in res["warnings"])
    assert any("SUTA NOT_IMPLEMENTED for PA" in w for w in res["warnings"])


def test_no_income_tax_state_is_a_real_zero():
    res = paycheck(gross_cents=300_000,
                   profile={"work_state": "TX", "filing_status": "single"})
    assert res["state_withholding_status"] == "no_income_tax"
    assert not any("State withholding NOT_IMPLEMENTED" in w
                   for w in res["warnings"])
    # SUTA still needs a rate: TX carries none by default -> NOT_IMPLEMENTED,
    # unless the tenant configures one.
    res2 = paycheck(gross_cents=300_000,
                    profile={"work_state": "TX", "filing_status": "single"},
                    tenant_suta_rates={"TX": 0.027})
    # tenant rate but no configured wage base -> still NOT_IMPLEMENTED
    assert res2["employer"]["suta"]["status"] == "NOT_IMPLEMENTED"
    res3 = paycheck(gross_cents=300_000,
                    profile={"work_state": "CA", "filing_status": "single"},
                    tenant_suta_rates={"CA": 0.05})
    assert res3["employer"]["suta"]["rate"] == 0.05  # tenant override wins
    assert res3["employer"]["suta_cents"] == 15_000


def test_garnishment_child_support_cap_and_priority():
    # $2,000 biweekly. Fed W/H $156.15, FICA $153.00 -> disposable $1,690.85.
    # Child support ordered $900; CCPA cap 50% of disposable = $845.43.
    # The creditor's $200 then gets $0: total garnishments cannot exceed
    # the support-order cap while a support order is being satisfied.
    res = paycheck(gross_cents=200_000, deductions_in=[
        {"label": "Child support", "category": "garnishment",
         "kind": "amount", "amount_cents": 90_000,
         "garnishment_type": "child_support"},
        {"label": "Creditor levy", "category": "garnishment",
         "kind": "amount", "amount_cents": 20_000,
         "garnishment_type": "other"},
    ])
    garn = {d["label"]: d for d in res["deductions"]
            if d.get("tax_class") == "garnishment"}
    assert garn["Child support"]["amount_cents"] == 84_543
    assert garn["Child support"]["capped"] is True
    assert garn["Creditor levy"]["amount_cents"] == 0
    assert garn["Creditor levy"]["capped"] is True
    assert res["net_cents"] == 200_000 - 15_615 - 15_300 - 84_543


def test_garnishment_ordinary_25_percent_cap():
    # Same paycheck, ordinary garnishment only, ordered $600:
    # cap = 25% x $1,690.85 disposable = $422.71.
    res = paycheck(gross_cents=200_000, deductions_in=[
        {"label": "Creditor levy", "category": "garnishment",
         "kind": "amount", "amount_cents": 60_000,
         "garnishment_type": "other"},
    ])
    garn = next(d for d in res["deductions"] if d.get("tax_class") == "garnishment")
    assert garn["amount_cents"] == 42_271
    assert garn["capped"] is True


# ============================================================ router flow

class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, length=None):
        docs = self._docs[:length] if length else list(self._docs)
        return [dict(d) for d in docs]


class _FakeColl:
    def __init__(self, docs=()):
        self.docs = [dict(d) for d in docs]

    @staticmethod
    def _matches(doc, q):
        for k, v in (q or {}).items():
            if isinstance(v, dict):
                if "$in" in v and doc.get(k) not in v["$in"]:
                    return False
                if "$ne" in v and doc.get(k) == v["$ne"]:
                    return False
                if "$gte" in v and not (doc.get(k) is not None and doc[k] >= v["$gte"]):
                    return False
                if "$lte" in v and not (doc.get(k) is not None and doc[k] <= v["$lte"]):
                    return False
            elif doc.get(k) != v:
                return False
        return True

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if self._matches(d, q):
                return dict(d)
        return None

    def find(self, q=None, projection=None):
        return _Cursor([d for d in self.docs if self._matches(d, q)])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, q, update):
        for d in self.docs:
            if self._matches(d, q):
                for k, v in update.get("$set", {}).items():
                    if "." in k:  # dotted pay_settings.* paths
                        head, _, tail = k.partition(".")
                        d.setdefault(head, {})[tail] = v
                    else:
                        d[k] = v
                return

    async def delete_one(self, q):
        for i, d in enumerate(self.docs):
            if self._matches(d, q):
                del self.docs[i]
                return


class _FakeLedger:
    def __init__(self):
        self.audits = []
        self.posted = []

    async def audit(self, db, tenant_id, user, action, detail=None):
        self.audits.append((action, detail))

    async def post_journal_entry(self, db, tenant_id, user, draft, status="posted"):
        lines = draft["lines"]
        dr = sum(l.get("debit", 0) or 0 for l in lines)
        cr = sum(l.get("credit", 0) or 0 for l in lines)
        assert dr == cr and dr > 0, f"unbalanced JE: dr={dr} cr={cr}"
        self.posted.append(draft)
        return {"id": f"je-{len(self.posted)}", "number": f"JE-{len(self.posted)}",
                "status": status, "lines": lines}


def _setup_db():
    users = _FakeColl([
        {"id": "owner1", "tenant_id": "t1", "name": "Owner", "email": "o@x.com",
         "role": "owner", "commission_rate": 0, "pay_settings": {}},
        # CA salaried employee, $6,000/mo, single, 1 DE 4 allowance, W-4 filed.
        {"id": "ca1", "tenant_id": "t1", "name": "Casey", "email": "c@x.com",
         "role": "barber", "commission_rate": 0,
         "pay_settings": {"salary_monthly_cents": 600000, "pay_type": "salary",
                          "work_state": "CA", "filing_status": "single",
                          "state_allowances": 1, "w4_on_file": True}},
    ])
    return SimpleNamespace(
        users=users, time_entries=_FakeColl(), pay_schedules=_FakeColl(),
        payroll_runs=_FakeColl(), payroll_settings=_FakeColl(),
        payroll_liabilities=_FakeColl(), payroll_pay_records=_FakeColl(),
        accounts=_FakeColl(),
        tenants=_FakeColl([{"id": "t1", "tenant_id": "t1", "name": "Test Shop"}]),
    )


@contextmanager
def _env():
    fake_db = _setup_db()
    fake_L = _FakeLedger()
    with patch.object(payroll, "db", fake_db), patch.object(payroll, "L", fake_L):
        yield fake_db, fake_L


def _user():
    return {"id": "owner1", "tenant_id": "t1", "role": "owner", "email": "o@x.com"}


def _posted_ca_run():
    body = RunIn(period_start="2026-10-01", period_end="2026-10-31",
                 pay_date="2026-10-30", items=[RunItemIn(user_id="ca1")])
    doc = _run(payroll.create_run(body, _user()))
    _run(payroll.approve_run(doc["id"], _user()))
    return _run(payroll.post_run(doc["id"], _user()))


def test_team_profile_roundtrip():
    with _env():
        team = _run(payroll.list_team(_user()))
    casey = next(m for m in team if m["id"] == "ca1")
    assert casey["work_state"] == "CA" and casey["pay_type"] == "salary"
    assert casey["w4"]["filing_status"] == "single"
    assert casey["state_allowances"] == 1 and casey["w4_on_file"] is True


def test_post_writes_pay_records_and_liabilities_with_due_dates():
    with _env() as (fake_db, fake_L):
        doc = _posted_ca_run()
        assert doc["status"] == "posted"
        item = doc["items"][0]
        # Casey, $6,000/mo CA single 1 allowance:
        # fed $584.17, FICA $459.00, CA W/H $232.34 -> net $4,724.49.
        assert item["federal_withholding_cents"] == 58_417
        assert item["employee_fica_cents"] == 45_900
        assert item["state_withholding_cents"] == 23_234
        assert item["net_cents"] == 472_449

        recs = fake_db.payroll_pay_records.docs
        assert len(recs) == 1
        assert recs[0]["immutable"] is True and recs[0]["reversed"] is False
        assert recs[0]["gross_cents"] == 600_000

        liabs = {(l["agency"], l["kind"]): l
                 for l in fake_db.payroll_liabilities.docs}
        # Federal deposit = fed W/H + both FICA shares = 58417 + 2 x 45900.
        fed = liabs[("IRS", "federal_deposit")]
        assert fed["amount_cents"] == 150_217
        # Monthly depositor (no lookback history): due the 15th of next
        # month; Nov 15 2026 is a Sunday -> Monday Nov 16.
        assert fed["due_date"] == "2026-11-16"
        assert liabs[("IRS", "futa")]["amount_cents"] == 3_600
        assert liabs[("IRS", "futa")]["due_date"] == "2027-01-31"  # Q4
        ca_wh = liabs[("CA", "state_withholding")]
        assert ca_wh["amount_cents"] == 23_234 and ca_wh["due_date"] == "2026-11-16"
        assert liabs[("CA", "suta")]["amount_cents"] == 20_400
        assert liabs[("CA", "suta")]["due_date"] == "2027-01-31"
        assert liabs[("CA", "ett")]["amount_cents"] == 600

        listing = _run(payroll.list_liabilities(_user()))
        assert listing["accrued_total_cents"] == sum(
            l["amount_cents"] for l in fake_db.payroll_liabilities.docs)


def test_liability_mark_remitted_flow():
    with _env() as (fake_db, _):
        _posted_ca_run()
        lid = fake_db.payroll_liabilities.docs[0]["id"]
        out = _run(payroll.patch_liability(
            lid, LiabilityPatchIn(status="remitted", reference="EFTPS-123"),
            _user()))
        assert out["status"] == "remitted" and out["reference"] == "EFTPS-123"
        with pytest.raises(HTTPException) as exc:  # cannot remit twice
            _run(payroll.patch_liability(
                lid, LiabilityPatchIn(status="remitted"), _user()))
        assert exc.value.status_code == 409


def test_workpapers_941_940_w2_accumulate_from_pay_records():
    with _env():
        _posted_ca_run()
        q4 = _run(payroll.form_941_workpaper(2026, 4, _user()))
        emp = q4["employees"][0]
        assert emp["fit_wages_cents"] == 600_000
        assert emp["federal_withholding_cents"] == 58_417
        assert emp["ss_wages_cents"] == 600_000 and emp["ss_tax_cents"] == 37_200
        assert emp["medicare_tax_cents"] == 8_700
        assert "does not file Form 941" in q4["disclaimer"]

        f940 = _run(payroll.form_940_workpaper(2026, _user()))
        assert f940["totals"]["futa_wages_cents"] == 600_000
        assert f940["totals"]["futa_net_cents"] == 3_600

        w2 = _run(payroll.w2_workpaper(2026, _user()))
        emp2 = w2["employees"][0]
        assert emp2["box1_wages_cents"] == 600_000
        assert emp2["box2_fed_withholding_cents"] == 58_417
        assert emp2["box4_ss_tax_cents"] == 37_200
        assert emp2["states"] == [{"state": "CA", "wages_cents": 600_000,
                                   "withholding_cents": 23_234}]


def test_reverse_run_reverses_liabilities_and_excludes_workpapers():
    with _env() as (fake_db, fake_L):
        doc = _posted_ca_run()
        rid = doc["id"]
        out = _run(payroll.reverse_run(rid, _user()))
        assert out["status"] == "reversed"
        assert all(l["status"] == "reversed"
                   for l in fake_db.payroll_liabilities.docs)
        assert fake_db.payroll_pay_records.docs[0]["reversed"] is True
        assert len(fake_L.posted) == 2  # original + offsetting entry
        q4 = _run(payroll.form_941_workpaper(2026, 4, _user()))
        assert q4["employees"] == []  # reversed runs leave the workpapers
        with pytest.raises(HTTPException) as exc:  # cannot reverse twice
            _run(payroll.reverse_run(rid, _user()))
        assert exc.value.status_code == 409


def test_deposit_schedule_semiweekly_due_date():
    # Wed 2026-10-28 payday, semiweekly -> due the following Wednesday.
    from datetime import date
    due = payroll._federal_due_date(date(2026, 10, 28), "semiweekly", 1_000)
    assert due.isoformat() == "2026-11-04"
    due2 = payroll._federal_due_date(date(2026, 10, 31), "semiweekly", 1_000)
    assert due2.isoformat() == "2026-11-06"  # Sat payday -> following Friday
    # $100k one-day rule beats the schedule.
    due3 = payroll._federal_due_date(date(2026, 10, 30), "monthly", 100_000_00)
    assert due3.isoformat() == "2026-11-02"  # next business day (Mon)
