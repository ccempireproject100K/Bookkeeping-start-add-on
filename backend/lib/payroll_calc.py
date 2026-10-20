"""Payroll calculation engine — real gross-to-net, tracking only.

This module is PURE (no db, no I/O): every function takes integer cents and the
versioned tables in lib/payroll_config_2026.py and returns integer cents plus a
workpaper dict explaining the computation. The router (routers/payroll.py) owns
persistence; an external money-movement executor (a future payroll partner)
would consume the finalized outputs, never this module directly.

What is computed here:
  - Federal income-tax withholding: IRS Pub 15-T Percentage Method for
    automated payroll systems (Worksheet 1A), W-4 (2020+) elections.
  - FICA: Social Security 6.2% with the annual wage-base cap applied against
    the employee's YTD FICA wages; Medicare 1.45%; Additional Medicare Tax
    0.9% withheld on wages above $200,000 YTD (employer withholding rule).
  - Employer FICA match, FUTA (6.0% on the first $7,000 with the 5.4% state
    credit accrued -> net 0.6% when state taxes are paid on time), SUTA from
    per-state config / tenant override.
  - State income-tax withholding for configured states (CA, NY for 2026).
    A state with no configured withholding table returns an explicit
    NOT_IMPLEMENTED result — never a silent zero.
  - Deductions with correct tax treatment: pre-tax deductions may reduce
    income-tax wages, FICA wages, both, or neither (see DEDUCTION_TREATMENTS);
    garnishments are capped under the Consumer Credit Protection Act
    (25% of disposable earnings for ordinary garnishments; child support
    follows its own percentage and is satisfied first).

What is NOT done here (or anywhere in this app): paying employees, depositing
withheld taxes, or filing returns. See routers/payroll.py DISCLAIMER.
"""
from decimal import Decimal, ROUND_HALF_UP

from lib import payroll_config_2026 as CFG

PERIODS_PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12,
                    "semimonthly": 24}

# ------------------------------------------------------------------ money utils

def _round(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def pct_of(cents: int, rate) -> int:
    """rate (e.g. 0.062) applied to integer cents, rounded half-up."""
    if not cents:
        return 0
    return _round(Decimal(int(cents)) * Decimal(str(rate)))


def bracket_tax(annual_cents: int, rows) -> int:
    """Apply a progressive annual bracket table.

    rows: [{over_cents, up_to_cents|None, base_cents, rate}] sorted ascending —
    the Pub 15-T presentation: tax = base + rate x (wage - over) for the row
    whose (over, up_to] contains the wage.
    """
    if annual_cents <= 0:
        return 0
    tax = 0
    for row in rows:
        over = row["over_cents"]
        up_to = row["up_to_cents"]
        if annual_cents > over and (up_to is None or annual_cents <= up_to):
            return row["base_cents"] + pct_of(annual_cents - over, row["rate"])
        if up_to is not None and annual_cents > up_to:
            tax = row["base_cents"] + pct_of(up_to - over, row["rate"])
    return tax


# ------------------------------------------------------------------ deductions

# category -> (reduces_income_tax_wages, reduces_fica_wages) at the FEDERAL
# level. State treatment can differ (e.g. CA does not exclude 401(k) deferrals
# from PIT wages) — state configs carry their own flags.
DEDUCTION_TREATMENTS = {
    # 401(k)/403(b) elective deferrals: excluded from income-tax wages,
    # still fully subject to FICA. (The classic one-way case.)
    "pretax_retirement": (True, False),
    # Section 125 cafeteria premiums: excluded from BOTH income-tax and
    # FICA wages.
    "pretax_section125": (True, True),
    # HSA via cafeteria plan: both. (Direct HSA contributions outside a
    # cafeteria plan reduce income-tax wages only — model those as
    # pretax_retirement-style entries with a custom label if needed.)
    "pretax_hsa": (True, True),
    # Anything after-tax (Roth, union dues, charitable, ...): neither.
    "posttax": (False, False),
    # Garnishments are computed on disposable earnings AFTER taxes and never
    # reduce any wage base.
    "garnishment": (False, False),
    # Booth rent / shop charges (legacy product concept): reduces net only.
    "booth_rent": (False, False),
}


def deduction_amount(ded: dict, gross_cents: int) -> int:
    if ded.get("kind") == "percent":
        return pct_of(gross_cents, Decimal(str(ded.get("value", 0))) / 100)
    return int(ded.get("amount_cents") or 0)


# ------------------------------------------------------------------ federal W/H

def federal_withholding(*, fit_wages_period_cents: int, frequency: str,
                        w4: dict, tables: dict) -> dict:
    """Pub 15-T Percentage Method (automated), Worksheet 1A, per pay period.

    w4: {filing_status, multiple_jobs, dependents_cents (annual, Step 3),
         other_income_cents (annual, Step 4a), deductions_cents (annual,
         Step 4b), extra_withholding_cents (per period, Step 4c)}
    Returns {amount_cents, workpaper}.
    """
    periods = PERIODS_PER_YEAR.get(frequency, 12)
    filing = (w4 or {}).get("filing_status") or "single"
    if filing not in tables:
        filing = "single"
    step2 = bool((w4 or {}).get("multiple_jobs"))
    variant = "step2" if step2 else "standard"
    rows = tables[filing][variant]

    annualized = int(fit_wages_period_cents) * periods
    # Worksheet 1A line 1g: $12,900 (MFJ) / $8,600 (otherwise) is subtracted
    # when the Step 2 box is NOT checked; $0 when it IS checked. There is no
    # separate divide-by-2 step in the 2026 worksheet — the halving effect is
    # baked into the Checkbox schedules (Pub 15-T, verified 2026-10-20).
    line_1g = 0 if step2 else CFG.FEDERAL["step1g_adjustment_cents"].get(
        (w4 or {}).get("filing_status") or filing, 8_600_00)
    adjusted = max(annualized
                   + int((w4 or {}).get("other_income_cents") or 0)
                   - int((w4 or {}).get("deductions_cents") or 0)
                   - line_1g, 0)
    tentative_annual = bracket_tax(adjusted, rows)
    dependents_credit = int((w4 or {}).get("dependents_cents") or 0)
    # Steps 3-4 of Worksheet 1A operate per pay period: tentative / periods,
    # minus dependents / periods (floored at zero), plus Step 4(c) extra.
    per_period = max(_round(Decimal(tentative_annual) / Decimal(periods))
                     - _round(Decimal(dependents_credit) / Decimal(periods)), 0)
    extra = int((w4 or {}).get("extra_withholding_cents") or 0)
    return {
        "amount_cents": per_period + extra,
        "workpaper": {
            "method": "pub15t_percentage_automated",
            "filing_status": filing, "table_variant": variant,
            "periods_per_year": periods,
            "fit_wages_period_cents": fit_wages_period_cents,
            "annualized_wages_cents": annualized,
            "line_1g_adjustment_cents": line_1g,
            "adjusted_annual_wages_cents": adjusted,
            "tentative_annual_tax_cents": tentative_annual,
            "dependents_credit_cents": dependents_credit,
            "extra_withholding_cents": extra,
        },
    }


# ------------------------------------------------------------------ FICA / FUTA

def fica(*, fica_wages_period_cents: int, ytd_fica_wages_cents: int,
         fed: dict) -> dict:
    """Employee + employer FICA for one paycheck, caps applied against YTD."""
    wages = max(int(fica_wages_period_cents), 0)
    ytd = max(int(ytd_fica_wages_cents), 0)
    ss_base = fed["ss_wage_base_cents"]
    ss_taxable = max(min(wages, ss_base - ytd), 0)
    ss = pct_of(ss_taxable, fed["ss_rate"])
    med = pct_of(wages, fed["medicare_rate"])
    # Additional Medicare: the employer must withhold 0.9% on wages paid in
    # excess of $200,000 in the calendar year (withholding threshold is on
    # wages in the current period once cumulative wages cross it).
    thr = fed["additional_medicare_threshold_cents"]
    addl_base = max(ytd + wages - thr, 0) - max(ytd - thr, 0)
    addl = pct_of(addl_base, fed["additional_medicare_rate"])
    return {
        "ss_wages_cents": ss_taxable, "ss_cents": ss,
        "medicare_wages_cents": wages, "medicare_cents": med,
        "additional_medicare_wages_cents": addl_base,
        "additional_medicare_cents": addl,
        "employee_total_cents": ss + med + addl,
        # Employer: SS + Medicare at the same rates/cap; no Additional match.
        "employer_ss_cents": ss, "employer_medicare_cents": med,
        "employer_total_cents": ss + med,
        "ss_cap_reached": (ytd + wages) >= ss_base,
    }


def futa(*, gross_wages_period_cents: int, ytd_gross_wages_cents: int,
         fed: dict) -> dict:
    """FUTA on the first $7,000 of annual wages per employee (employer only).

    Returns the gross tax, the 5.4% credit accrued (state taxes assumed paid
    on time — if a state SUTA is NOT_IMPLEMENTED or unpaid, the credit is at
    risk and the workpaper says so), and the net liability.
    """
    base = fed["futa_wage_base_cents"]
    taxable = max(min(int(gross_wages_period_cents), base - int(ytd_gross_wages_cents)), 0)
    gross_tax = pct_of(taxable, fed["futa_rate"])
    credit = pct_of(taxable, fed["futa_credit_rate"])
    return {
        "taxable_wages_cents": taxable, "gross_cents": gross_tax,
        "credit_cents": credit, "net_cents": gross_tax - credit,
    }


def suta(*, state: str, wages_period_cents: int, ytd_wages_cents: int,
         tenant_rates: dict) -> dict:
    """State unemployment tax (employer). Rate resolution order:
    tenant override -> state config default (new-employer rate).
    No rate anywhere -> NOT_IMPLEMENTED (never a silent zero)."""
    st = CFG.STATES.get(state or "")
    cfg_rate = (st or {}).get("suta_new_employer_rate")
    cfg_base = (st or {}).get("suta_wage_base_cents")
    rate = (tenant_rates or {}).get(state) if tenant_rates else None
    source = "tenant_override"
    if rate is None:
        rate, source = cfg_rate, "config_default_new_employer"
    base = cfg_base
    if rate is None or base is None:
        return {"status": "NOT_IMPLEMENTED", "state": state,
                "taxable_wages_cents": 0, "amount_cents": 0,
                "note": "No SUTA rate configured for this state."}
    taxable = max(min(int(wages_period_cents), base - int(ytd_wages_cents)), 0)
    return {"status": "ok", "state": state, "rate": rate, "rate_source": source,
            "wage_base_cents": base, "taxable_wages_cents": taxable,
            "amount_cents": pct_of(taxable, rate)}


# ------------------------------------------------------------------ state W/H

def _state_wages_for_withholding(state_cfg: dict, gross_cents: int,
                                 pretax: list) -> int:
    """State withholding wage base: gross minus pre-tax deductions the state
    actually excludes (CA notably does NOT exclude 401(k) deferrals)."""
    wages = gross_cents
    for d in pretax:
        cat = d.get("category")
        if cat == "pretax_retirement" and not state_cfg.get("pretax_retirement_reduces_wages", True):
            continue
        if cat in ("pretax_section125", "pretax_hsa") and state_cfg.get("pretax_cafeteria_reduces_wages", True):
            wages -= d["amount_cents"]
        elif cat == "pretax_retirement" and state_cfg.get("pretax_retirement_reduces_wages", True):
            wages -= d["amount_cents"]
    return max(wages, 0)


def state_withholding(*, state: str, gross_cents: int, pretax: list,
                      frequency: str, profile: dict) -> dict:
    """State income-tax withholding for one paycheck.

    profile: {filing_status, allowances (state form), extra_withholding_cents,
              state_dependents_cents (NY-style exemption count lives in
              'allowances'), ...}
    Returns {status: ok|NOT_IMPLEMENTED|no_income_tax, amount_cents, workpaper}.
    """
    if state and state in CFG.NO_INCOME_TAX_STATES:
        return {"status": "no_income_tax", "state": state, "amount_cents": 0,
                "workpaper": {"reason": f"{state} has no personal income tax on wages."}}
    st = CFG.STATES.get(state or "")
    if not st:
        return {"status": "NOT_IMPLEMENTED", "state": state, "amount_cents": 0,
                "workpaper": {"reason": f"No withholding module configured for state '{state}'."}}
    method = st.get("withholding_method")
    if method == "ca_method_b":
        return _ca_withholding(st, gross_cents, pretax, frequency, profile)
    if method == "ny_exact":
        return _ny_withholding(st, gross_cents, pretax, frequency, profile)
    return {"status": "NOT_IMPLEMENTED", "state": state, "amount_cents": 0,
            "workpaper": {"reason": f"Withholding method '{method}' not implemented."}}


def _ca_withholding(st, gross_cents, pretax, frequency, profile):
    """California PIT withholding, EDD Method B (exact calculation):
    (1) low-income exemption check; (2) subtract the estimated-deduction
    amount (DE 4 additional allowances x $1,000/yr); (3) subtract the
    standard deduction for the Method B bucket; (4) apply the rate schedule
    for the filing status; (5) subtract the exemption credit (regular
    allowances x $168.30/yr, flat across statuses). CA keeps TWO allowance
    concepts on the DE 4: regular allowances earn the credit, additional
    allowances buy the $1,000 deduction — the profile carries both."""
    periods = PERIODS_PER_YEAR.get(frequency, 12)
    filing = (profile or {}).get("filing_status") or "single"
    ca_table = {"single": "single", "married_filing_separately": "single",
                "married_filing_jointly": "married",
                "head_of_household": "head_of_household"}.get(filing, "single")
    allowances = int((profile or {}).get("state_allowances") or 0)
    ded_allowances = int((profile or {}).get("state_deduction_allowances") or 0)
    # Method B Tables 1/3 bucket: a married employee with 0-1 regular
    # allowances uses the SMALLER standard deduction / exemption threshold.
    if filing == "married_filing_jointly":
        bucket = "married" if allowances >= 2 else "married_low"
    else:
        bucket = ca_table
    wages_period = _state_wages_for_withholding(st, gross_cents, pretax)
    annual = wages_period * periods
    est_deduction = ded_allowances * st["allowance_deduction_cents"]
    std_deduction = st["standard_deduction_cents"][bucket]
    taxable = max(annual - est_deduction - std_deduction, 0)
    if annual <= st["low_income_exemption_cents"][bucket]:
        tax_annual = 0
    else:
        tax_annual = bracket_tax(taxable, st["brackets"][ca_table])
    credit = allowances * st["exemption_credit_per_allowance_cents"]
    net_annual = max(tax_annual - credit, 0)
    per_period = _round(Decimal(net_annual) / Decimal(periods))
    extra = int((profile or {}).get("extra_withholding_state_cents") or 0)
    return {"status": "ok", "state": "CA", "amount_cents": per_period + extra,
            "wages_cents": wages_period,
            "workpaper": {"method": "ca_method_b", "filing_status": ca_table,
                          "bucket": bucket,
                          "allowances": allowances,
                          "deduction_allowances": ded_allowances,
                          "annual_wages_cents": annual,
                          "estimated_deduction_cents": est_deduction,
                          "standard_deduction_cents": std_deduction,
                          "taxable_annual_cents": taxable,
                          "annual_tax_cents": tax_annual,
                          "exemption_credit_cents": credit,
                          "extra_withholding_cents": extra}}


def _ny_withholding(st, gross_cents, pretax, frequency, profile):
    """New York State withholding, exact calculation Method II
    (NYS-50-T-NYS): annualize wages, subtract the Table B deduction
    allowance (Single $7,400 / Married $7,950) plus $1,000 per IT-2104
    exemption, apply the Method II annual schedule (which embeds the
    tax-benefit recapture), de-annualize, add extra. Withholding has only
    Single and Married statuses — Head of Household withholds on the Single
    schedule (IT-2104 'Single or Head of household' box). Above the Method
    II ceiling, Method III takes a flat percentage of net wages.
    NYC/Yonkers local withholding is NOT computed (see workpaper note)."""
    periods = PERIODS_PER_YEAR.get(frequency, 12)
    filing = (profile or {}).get("filing_status") or "single"
    ny_status = {"single": "single", "married_filing_separately": "single",
                 "married_filing_jointly": "married",
                 "head_of_household": "single"}.get(filing, "single")
    allowances = int((profile or {}).get("state_allowances") or 0)
    wages_period = _state_wages_for_withholding(st, gross_cents, pretax)
    annual = wages_period * periods
    ded_allowance = st["deduction_allowance_cents"][ny_status]
    allowance_amt = allowances * st["allowance_amount_cents"]
    taxable = max(annual - ded_allowance - allowance_amt, 0)
    m3 = st.get("method_iii", {}).get(ny_status) or []
    if m3 and taxable >= m3[0][0]:
        rate = m3[0][1]
        for threshold, r in m3:
            if taxable >= threshold:
                rate = r
        tax_annual = pct_of(taxable, rate)
        method_used = "ny_method_iii"
    else:
        tax_annual = bracket_tax(taxable, st["brackets"][ny_status])
        method_used = "ny_method_ii"
    per_period = _round(Decimal(tax_annual) / Decimal(periods))
    extra = int((profile or {}).get("extra_withholding_state_cents") or 0)
    return {"status": "ok", "state": "NY", "amount_cents": per_period + extra,
            "wages_cents": wages_period,
            "workpaper": {"method": method_used, "filing_status": ny_status,
                          "allowances": allowances,
                          "annual_wages_cents": annual,
                          "deduction_allowance_cents": ded_allowance,
                          "allowance_amount_cents": allowance_amt,
                          "taxable_annual_cents": taxable,
                          "annual_tax_cents": tax_annual,
                          "extra_withholding_cents": extra,
                          "note": "NYC / Yonkers local withholding NOT_IMPLEMENTED."}}


# ------------------------------------------------------------------ garnishments

def apply_garnishments(garnishments: list, disposable_cents: int) -> list:
    """CCPA-capped garnishment application, in priority order:
    child support first (its own percentage of disposable earnings, default
    50%), then ordinary garnishments. Ordinary garnishments are limited to
    25% of disposable earnings AND to what remains under the child-support
    cap when a support order is present (the DOL combined-cap rule: total
    garnishments may not exceed the support percentage). Each returned line
    carries requested vs applied amounts so the stub shows any amount that
    could NOT legally be taken."""
    out = []
    disposable = max(disposable_cents, 0)
    remaining_disposable = disposable
    ordinary_cap = pct_of(disposable, CFG.CCPA_ORDINARY_RATE)
    support_rates = [g.get("percent_of_disposable") or CFG.CCPA_CHILD_SUPPORT_DEFAULT_RATE
                     for g in garnishments if g.get("garnishment_type") == "child_support"]
    combined_cap = pct_of(disposable, max(support_rates)) if support_rates else ordinary_cap
    total_used = 0
    ordinary_used = 0
    ordered = sorted(garnishments,
                     key=lambda g: 0 if g.get("garnishment_type") == "child_support" else 1)
    for g in ordered:
        requested = deduction_amount(g, disposable) \
            if g.get("kind") == "percent" else int(g.get("amount_cents") or 0)
        if g.get("garnishment_type") == "child_support":
            cap_rate = g.get("percent_of_disposable") or CFG.CCPA_CHILD_SUPPORT_DEFAULT_RATE
            cap = pct_of(disposable, cap_rate)
            applied = min(requested, cap, remaining_disposable)
        else:
            room = max(min(ordinary_cap - ordinary_used,
                           combined_cap - total_used), 0)
            applied = min(requested, room, remaining_disposable)
            ordinary_used += applied
        total_used += applied
        remaining_disposable -= applied
        out.append({**g, "amount_cents": applied,
                    "requested_cents": requested,
                    "capped": applied < requested})
    return out


# ------------------------------------------------------------------ one paycheck

def compute_paycheck(*, gross_cents: int, frequency: str, w4: dict,
                     profile: dict, deductions_in: list,
                     ytd: dict, tenant_suta_rates: dict) -> dict:
    """Full gross-to-net for one employee for one period.

    deductions_in: standing + ad-hoc deductions, each
      {label, category, kind: percent|amount, value | amount_cents,
       garnishment_type?, percent_of_disposable?}
    ytd: {gross_cents, fica_wages_cents} BEFORE this paycheck (finalized runs
      only — drafts must not move YTD).
    profile: {work_state, filing_status, state_allowances, ...}
    Returns the calculation core of a run item (router adds identity fields).
    """
    fed = CFG.FEDERAL
    gross = int(gross_cents)
    ytd = ytd or {}
    ytd_fica_wages = int(ytd.get("fica_wages_cents") or 0)
    ytd_gross = int(ytd.get("gross_cents") or 0)

    warnings = []

    # ---- pre-tax deductions split by wage-base treatment
    pretax, posttax, garnish_in, booth = [], [], [], []
    for d in deductions_in or []:
        cat = d.get("category") or "posttax"
        line = {**d, "category": cat, "amount_cents": deduction_amount(d, gross)}
        if cat in ("pretax_retirement", "pretax_section125", "pretax_hsa"):
            pretax.append(line)
        elif cat == "garnishment":
            garnish_in.append(line)  # amount decided after taxes (disposable)
        elif cat == "booth_rent":
            booth.append(line)
        else:
            posttax.append(line)

    fit_reduction = sum(d["amount_cents"] for d in pretax
                        if DEDUCTION_TREATMENTS[d["category"]][0])
    fica_reduction = sum(d["amount_cents"] for d in pretax
                         if DEDUCTION_TREATMENTS[d["category"]][1])
    fit_wages = max(gross - fit_reduction, 0)
    fica_wages = max(gross - fica_reduction, 0)

    # ---- federal withholding (Pub 15-T) on FIT wages
    fed_wh = federal_withholding(fit_wages_period_cents=fit_wages,
                                 frequency=frequency, w4=w4 or {},
                                 tables=fed["withholding_tables"])

    # ---- FICA on FICA wages, caps vs YTD
    fica_res = fica(fica_wages_period_cents=fica_wages,
                    ytd_fica_wages_cents=ytd_fica_wages, fed=fed)
    if fica_res["ss_cap_reached"]:
        warnings.append("Social Security wage base reached for this employee.")

    # ---- state withholding
    state = (profile or {}).get("work_state") or None
    state_wh = state_withholding(state=state, gross_cents=gross, pretax=pretax,
                                 frequency=frequency, profile=profile or {})
    if state and state_wh["status"] == "NOT_IMPLEMENTED":
        warnings.append(
            f"State withholding NOT_IMPLEMENTED for {state} — no state tax was "
            "computed; the stub understates deductions until that state ships.")
    state_wh_cents = state_wh["amount_cents"] if state_wh["status"] == "ok" else 0

    taxes = [
        {"code": "federal_withholding", "label": "Federal withholding",
         "amount_cents": fed_wh["amount_cents"], "workpaper": fed_wh["workpaper"]},
        {"code": "social_security", "label": "FICA — Social Security",
         "amount_cents": fica_res["ss_cents"],
         "wages_cents": fica_res["ss_wages_cents"]},
        {"code": "medicare", "label": "FICA — Medicare",
         "amount_cents": fica_res["medicare_cents"],
         "wages_cents": fica_res["medicare_wages_cents"]},
    ]
    if fica_res["additional_medicare_cents"]:
        taxes.append({"code": "additional_medicare",
                      "label": "FICA — Additional Medicare",
                      "amount_cents": fica_res["additional_medicare_cents"],
                      "wages_cents": fica_res["additional_medicare_wages_cents"]})
    if state:
        taxes.append({"code": "state_withholding",
                      "label": f"State withholding ({state})",
                      "amount_cents": state_wh_cents, "state": state,
                      "status": state_wh["status"],
                      "wages_cents": state_wh.get("wages_cents"),
                      "workpaper": state_wh.get("workpaper")})

    tax_total = sum(t["amount_cents"] for t in taxes)

    # ---- disposable earnings -> garnishments (CCPA)
    disposable = max(gross - tax_total, 0)
    garnishments = apply_garnishments(garnish_in, disposable)
    for g in garnishments:
        if g["capped"]:
            warnings.append(
                f"Garnishment '{g.get('label')}' capped by CCPA: requested "
                f"{g['requested_cents']}c, applied {g['amount_cents']}c.")

    ordered_deductions = (
        [{**d, "tax_class": "pretax"} for d in pretax]
        + [{**t, "tax_class": "tax"} for t in taxes]
        + [{**d, "tax_class": "posttax"} for d in posttax]
        + [{**g, "tax_class": "garnishment"} for g in garnishments]
        + [{**d, "tax_class": "booth_rent"} for d in booth]
    )
    total_deductions = sum(d["amount_cents"] for d in ordered_deductions)
    net = gross - total_deductions
    if net < 0:
        raise ValueError("Deductions exceed gross pay")

    # ---- employer taxes
    futa_res = futa(gross_wages_period_cents=gross, ytd_gross_wages_cents=ytd_gross,
                    fed=fed)
    suta_res = suta(state=state, wages_period_cents=gross,
                    ytd_wages_cents=ytd_gross, tenant_rates=tenant_suta_rates)
    if state and suta_res["status"] == "NOT_IMPLEMENTED":
        warnings.append(f"SUTA NOT_IMPLEMENTED for {state} — employer cost understates.")
    # CA Employment Training Tax (employer, 0.1% on the first $7,000) —
    # states whose config carries an ett_* block get it accrued alongside SUTA.
    ett_cents = 0
    st_cfg = CFG.STATES.get(state or "") or {}
    if st_cfg.get("ett_rate"):
        ett_taxable = max(min(gross, st_cfg["ett_wage_base_cents"] - ytd_gross), 0)
        ett_cents = pct_of(ett_taxable, st_cfg["ett_rate"])
    employer = {
        "fica_ss_cents": fica_res["employer_ss_cents"],
        "fica_medicare_cents": fica_res["employer_medicare_cents"],
        "fica_total_cents": fica_res["employer_total_cents"],
        "futa_taxable_wages_cents": futa_res["taxable_wages_cents"],
        "futa_gross_cents": futa_res["gross_cents"],
        "futa_credit_cents": futa_res["credit_cents"],
        "futa_cents": futa_res["net_cents"],
        "suta": suta_res,
        "suta_cents": suta_res["amount_cents"] if suta_res["status"] == "ok" else 0,
        "ett_cents": ett_cents,
    }
    employer["total_tax_cents"] = (employer["fica_total_cents"] + employer["futa_cents"]
                                   + employer["suta_cents"] + ett_cents)

    return {
        "gross_cents": gross,
        "fit_wages_cents": fit_wages, "fica_wages_cents": fica_wages,
        "deductions": ordered_deductions,
        "taxes": taxes,
        "total_deductions_cents": total_deductions,
        "net_cents": net,
        "federal_withholding_cents": fed_wh["amount_cents"],
        "employee_fica_cents": fica_res["employee_total_cents"],
        "state_withholding_cents": state_wh_cents,
        "state_withholding_status": state_wh["status"] if state else "no_state",
        "employer": employer,
        "employer_fica_cents": employer["fica_total_cents"],
        "employer_cost_cents": gross + employer["total_tax_cents"],
        "ytd_before": {"gross_cents": ytd_gross, "fica_wages_cents": ytd_fica_wages},
        "ytd_after": {"gross_cents": ytd_gross + gross,
                      "fica_wages_cents": ytd_fica_wages + fica_wages,
                      "federal_withholding_cents": int(ytd.get("federal_withholding_cents") or 0) + fed_wh["amount_cents"],
                      "net_cents": int(ytd.get("net_cents") or 0) + net},
        "warnings": warnings,
        "config_version": CFG.CONFIG_VERSION,
    }
