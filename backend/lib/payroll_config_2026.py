"""Payroll tax tables — tax year 2026 (versioned config; data, not code).

Same discipline as the tax-estimation engine: every figure carries its source
and verification date; when the IRS / EDD / NYS publish next year's tables,
this file is replaced — the calculation code in lib/payroll_calc.py does not
change.

SOURCES (all read 2026-10-20):
  Federal withholding: IRS Publication 15-T (2026), Worksheet 1A + Percentage
    Method Tables for Automated Payroll Systems — irs.gov (verified direct).
  FICA / FUTA: IRS Topic 751 (SS wage base $184,500 for 2026 confirmed) and
    Topic 759 (FUTA 6.0% on first $7,000, max 5.4% state credit).
  CA withholding: CA EDD 2026 Withholding Schedules, Method B (exact
    calculation), Tables 1-7 — edd.ca.gov (verified direct).
  CA UI/ETT: CA EDD tax-rated employers page (UI new employer 3.4% on
    $7,000, Schedule F+ for 2026; ETT 0.1% on $7,000).
  NY withholding: NYS-50-T-NYS (1/26), Method II annual schedules +
    Table A/B/C deduction & exemption allowances — tax.ny.gov (verified).
  NY UI: NY DOL rate information + NYS-50 (2026 UI wage base $17,600 under
    the new 18%-of-average-wage formula; new employer UI 4.025% + RSF
    0.075% = 4.100% total).

All amounts are integer cents. Bracket rows are
{over_cents, up_to_cents|None, base_cents, rate} — tax on an annual wage W in
the row is base + rate x (W - over).
"""

CONFIG_VERSION = "2026.1.0"
TAX_YEAR = 2026
LAST_VERIFIED = "2026-10-20"


def _rows(spec):
    """spec: [(over_$, up_to_$|None, base_$, rate), ...] in dollars -> cents."""
    return [{"over_cents": int(round(o * 100)),
             "up_to_cents": (int(round(u * 100)) if u is not None else None),
             "base_cents": int(round(b * 100)), "rate": r}
            for (o, u, b, r) in spec]


# --------------------------------------------------------------------- federal

FEDERAL = {
    "ss_rate": 0.062,
    "ss_wage_base_cents": 184_500_00,
    "medicare_rate": 0.0145,
    "additional_medicare_rate": 0.009,
    # Employer withholding threshold for Additional Medicare (wages in the
    # calendar year, regardless of filing status — IRS Topic 751).
    "additional_medicare_threshold_cents": 200_000_00,
    "futa_rate": 0.06,
    "futa_credit_rate": 0.054,
    "futa_wage_base_cents": 7_000_00,
    # Pub 15-T Worksheet 1A line 1g: subtracted from the adjusted annual wage
    # when the W-4 Step 2 box is NOT checked (the amount already accounts for
    # the standard deduction in the tables). Zero when the box IS checked.
    "step1g_adjustment_cents": {
        "single": 8_600_00, "married_filing_jointly": 12_900_00,
        "married_filing_separately": 8_600_00, "head_of_household": 8_600_00,
    },
    # Percentage Method Tables for Automated Payroll Systems (2026, annual).
    # Key "single" serves Single AND Married Filing Separately (one IRS table).
    "withholding_tables": {
        "married_filing_jointly": {
            "standard": _rows([
                (0, 19300, 0, 0.0), (19300, 44100, 0, 0.10),
                (44100, 120100, 2480, 0.12), (120100, 230700, 11600, 0.22),
                (230700, 422850, 35932, 0.24), (422850, 531750, 82048, 0.32),
                (531750, 788000, 116896, 0.35), (788000, None, 206583.50, 0.37)]),
            "step2": _rows([
                (0, 16100, 0, 0.0), (16100, 28500, 0, 0.10),
                (28500, 66500, 1240, 0.12), (66500, 121800, 5800, 0.22),
                (121800, 217875, 17966, 0.24), (217875, 272325, 41024, 0.32),
                (272325, 400450, 58448, 0.35), (400450, None, 103291.75, 0.37)]),
        },
        "single": {
            "standard": _rows([
                (0, 7500, 0, 0.0), (7500, 19900, 0, 0.10),
                (19900, 57900, 1240, 0.12), (57900, 113200, 5800, 0.22),
                (113200, 209275, 17966, 0.24), (209275, 263725, 41024, 0.32),
                (263725, 648100, 58448, 0.35), (648100, None, 192979.25, 0.37)]),
            "step2": _rows([
                (0, 8050, 0, 0.0), (8050, 14250, 0, 0.10),
                (14250, 33250, 620, 0.12), (33250, 60900, 2900, 0.22),
                (60900, 108938, 8983, 0.24), (108938, 136163, 20512, 0.32),
                (136163, 328350, 29224, 0.35), (328350, None, 96489.63, 0.37)]),
        },
        "head_of_household": {
            "standard": _rows([
                (0, 15550, 0, 0.0), (15550, 33250, 0, 0.10),
                (33250, 83000, 1770, 0.12), (83000, 121250, 7740, 0.22),
                (121250, 217300, 16155, 0.24), (217300, 271750, 39207, 0.32),
                (271750, 656150, 56631, 0.35), (656150, None, 191171, 0.37)]),
            "step2": _rows([
                (0, 12075, 0, 0.0), (12075, 20925, 0, 0.10),
                (20925, 45800, 885, 0.12), (45800, 64925, 3870, 0.22),
                (64925, 112950, 8077.50, 0.24), (112950, 140175, 19603.50, 0.32),
                (140175, 332375, 28315.50, 0.35), (332375, None, 95585.50, 0.37)]),
        },
    },
}

# CCPA (15 USC 1673) garnishment caps, as fractions of disposable earnings.
CCPA_ORDINARY_RATE = 0.25
CCPA_CHILD_SUPPORT_DEFAULT_RATE = 0.50  # 50%; 60% if not supporting another
# family, +5% when arrears exceed 12 weeks — set per garnishment line.

# ----------------------------------------------------------------------- states

STATES = {
    "CA": {
        "withholding_method": "ca_method_b",
        # CA does not conform to the federal exclusions for PIT wages:
        # 401(k) deferrals and cafeteria-plan amounts stay IN CA PIT wages.
        "pretax_retirement_reduces_wages": False,
        "pretax_cafeteria_reduces_wages": False,
        # DE 4: estimated-deduction allowances are worth $1,000/yr each;
        # regular allowances earn a flat exemption credit of $168.30/yr.
        "allowance_deduction_cents": 1_000_00,
        "exemption_credit_per_allowance_cents": 168_30,
        # Method B Table 3 standard deduction + Table 1 low-income exemption,
        # by Method B status bucket (see payroll_calc._ca_bucket).
        "standard_deduction_cents": {
            "single": 5_706_00, "married_low": 5_706_00,
            "married": 11_412_00, "head_of_household": 11_412_00},
        "low_income_exemption_cents": {
            "single": 18_896_00, "married_low": 18_896_00,
            "married": 37_791_00, "head_of_household": 37_791_00},
        # Method B Tables 5/6/7 — WITHHOLDING rate schedules (statutory x 1.1;
        # top rows include the 1% Mental Health Services Tax). Not return rates.
        "brackets": {
            "single": _rows([
                (0, 11079, 0, 0.011), (11079, 26264, 121.87, 0.022),
                (26264, 41452, 455.94, 0.044), (41452, 57542, 1124.21, 0.066),
                (57542, 72724, 2186.15, 0.088), (72724, 371479, 3522.17, 0.1023),
                (371479, 445771, 34084.81, 0.1133), (445771, 742953, 42502.09, 0.1243),
                (742953, 1000000, 79441.81, 0.1353), (1000000, None, 114220.27, 0.1463)]),
            "married": _rows([
                (0, 22158, 0, 0.011), (22158, 52528, 243.74, 0.022),
                (52528, 82904, 911.88, 0.044), (82904, 115084, 2248.42, 0.066),
                (115084, 145448, 4372.30, 0.088), (145448, 742958, 7044.33, 0.1023),
                (742958, 891542, 68169.60, 0.1133), (891542, 1000000, 85004.17, 0.1243),
                (1000000, 1485906, 98485.50, 0.1353), (1485906, None, 164228.58, 0.1463)]),
            "head_of_household": _rows([
                (0, 22173, 0, 0.011), (22173, 52530, 243.90, 0.022),
                (52530, 67716, 911.75, 0.044), (67716, 83805, 1579.93, 0.066),
                (83805, 98990, 2641.80, 0.088), (98990, 505208, 3978.08, 0.1023),
                (505208, 606251, 45534.18, 0.1133), (606251, 1000000, 56982.35, 0.1243),
                (1000000, 1010417, 105925.35, 0.1353), (1010417, None, 107334.77, 0.1463)]),
        },
        "suta_new_employer_rate": 0.034,
        "suta_wage_base_cents": 7_000_00,
        # Employment Training Tax: 0.1% on the first $7,000 (employer).
        "ett_rate": 0.001,
        "ett_wage_base_cents": 7_000_00,
    },
    "NY": {
        "withholding_method": "ny_exact",
        # NY follows the federal wage exclusions for 401(k) / cafeteria plans.
        "pretax_retirement_reduces_wages": True,
        "pretax_cafeteria_reduces_wages": True,
        # NYS-50-T-NYS Method II, Table B deduction allowance (+ $1,000 per
        # IT-2104 exemption, Table C). Withholding statuses are only Single
        # and Married — Head of Household withholds on the Single schedule.
        "deduction_allowance_cents": {"single": 7_400_00, "married": 7_950_00},
        "allowance_amount_cents": 1_000_00,
        # Method II annual rate schedules (embed the tax-benefit recapture —
        # these are the booklet's exact rows, NOT the bare statutory rates).
        "brackets": {
            "single": _rows([
                (0, 8500, 0, 0.039), (8500, 11700, 332, 0.044),
                (11700, 13900, 472, 0.0515), (13900, 80650, 586, 0.054),
                (80650, 96800, 4190, 0.059), (96800, 107650, 5143, 0.0703),
                (107650, 157650, 5906, 0.0753), (157650, 215400, 9673, 0.064),
                (215400, 265400, 13369, 0.1144), (265400, 1077550, 19091, 0.0735)]),
            "married": _rows([
                (0, 8500, 0, 0.039), (8500, 11700, 332, 0.044),
                (11700, 13900, 472, 0.0515), (13900, 80650, 586, 0.054),
                (80650, 96800, 4190, 0.059), (96800, 107650, 5143, 0.0657),
                (107650, 157650, 5855, 0.0707), (157650, 211550, 9388, 0.0801),
                (211550, 323200, 13708, 0.064), (323200, 373200, 20854, 0.1349),
                (373200, 1077550, 27600, 0.0735), (1077550, 2155350, 79369, 0.0765)]),
        },
        # Method III: above the Method II ceiling, withholding is a flat
        # percentage of the full annualized net wages.
        "method_iii": {
            "single": [(1_077_550_00, 0.1045), (5_000_000_00, 0.1110),
                       (25_000_000_00, 0.1170)],
            "married": [(2_155_350_00, 0.1045), (5_000_000_00, 0.1110),
                        (25_000_000_00, 0.1170)],
        },
        # NY UI: 2026 wage base $17,600; new-employer total 4.100%
        # (UI 4.025% + Re-employment Service Fund 0.075%).
        "suta_new_employer_rate": 0.041,
        "suta_wage_base_cents": 17_600_00,
    },
}

# States with no personal income tax on wages: withholding is a REAL zero
# (not NOT_IMPLEMENTED). SUTA still applies and needs a rate — these states
# carry no SUTA default here, so SUTA resolves via tenant override or
# NOT_IMPLEMENTED (never a silent zero).
NO_INCOME_TAX_STATES = {
    "AK", "FL", "NV", "NH", "SD", "TN", "TX", "WA", "WY",
}
