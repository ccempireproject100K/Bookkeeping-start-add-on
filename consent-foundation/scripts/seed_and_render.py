#!/usr/bin/env python3
"""Seed demo consent records and render the internal cohort report.

Demo data only — invented tenants, created deterministically so the report
(and its screenshot) is reproducible. Rates are computed independently for
each consent scope by consent/cohorts.py from the records, never hard-coded
here.

Usage: python3 scripts/seed_and_render.py [out.html]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from consent.clause import SCOPE_LICENSING, SCOPE_MARKETING_IMPROVEMENT  # noqa: E402
from consent.cohorts import cohort_report, overall_totals  # noqa: E402
from consent.store import ConsentStore  # noqa: E402

# (month, trade, plan, presented,
#  marketing opt-ins, marketing later opt-outs,
#  licensing opt-ins, licensing later opt-outs)
SEED = [
    ("2026-09", "hvac",       "starter", 14, 9, 1, 6, 0),
    ("2026-09", "plumbing",   "lite",    11, 5, 0, 7, 1),
    ("2026-09", "roofing",    "pro",      6, 4, 1, 3, 0),
    ("2026-10", "hvac",       "starter", 18, 12, 2, 9, 1),
    ("2026-10", "hvac",       "pro",      7, 5, 0, 4, 0),
    ("2026-10", "plumbing",   "lite",    13, 6, 1, 5, 1),
    ("2026-10", "electrical", "starter",  9, 5, 0, 6, 0),
    ("2026-11", "hvac",       "starter", 10, 7, 0, 5, 1),
    ("2026-11", "roofing",    "pro",      5, 3, 0, 2, 0),
    ("2026-11", "electrical", "lite",     8, 3, 1, 4, 1),
]


def seed(store: ConsentStore) -> None:
    n = 0
    for month, trade, plan, presented, m_in, m_out, l_in, l_out in SEED:
        for i in range(presented):
            n += 1
            tid = f"demo-{n:03d}"
            store.record_onboarding_decisions(
                tenant_id=tid,
                business_name=f"Demo {trade.title()} Shop {n}",
                signup_month=month, trade=trade, plan_id=plan,
                marketing_improvement_opted_in=i < m_in,
                licensing_opted_in=i < l_in,
                at=f"{month}-15T12:00:00+00:00",
            )
            if i < m_out:  # first marketing opt-outs withdraw from Scope A
                store.change_scope_consent(
                    tid, SCOPE_MARKETING_IMPROVEMENT, False,
                    surface="settings", at=f"{month}-28T12:00:00+00:00",
                )
            if i < l_out:  # first licensing opt-outs withdraw from Scope B
                store.change_scope_consent(
                    tid, SCOPE_LICENSING, False,
                    surface="settings", at=f"{month}-28T12:00:00+00:00",
                )


def pct(x):
    return "—" if x is None else f"{x * 100:.1f}%"


def render_scope(scope_label: str, rows, totals) -> str:
    body = "\n".join(
        f"<tr><td>{r['signup_month']}</td><td>{r['trade']}</td><td>{r['plan_id']}</td>"
        f"<td>{r['presented']}</td><td>{r['opted_in_now']}</td><td>{r['declined']}</td>"
        f"<td>{r['opted_out_ever']}</td><td><strong>{pct(r['opt_in_rate'])}</strong></td>"
        f"<td>{pct(r['opt_out_rate'])}</td></tr>"
        for r in rows
    )
    return f"""
<h2>{scope_label}</h2>
<table>
<tr><th>Signup month</th><th>Trade</th><th>Plan</th><th>Presented</th><th>Opted in now</th><th>Declined</th><th>Opted out (ever)</th><th>Opt-in rate</th><th>Opt-out rate</th></tr>
{body}
</table>
<div class="tot"><strong>{scope_label} totals:</strong> presented {totals['presented']} · opted in now {totals['opted_in_now']} · declined {totals['declined']} · opted out ever {totals['opted_out_ever']} · <strong>opt-in rate {pct(totals['opt_in_rate'])}</strong> · opt-out rate {pct(totals['opt_out_rate'])} (of tenants who ever opted in)</div>"""


def render(marketing_rows, marketing_totals, licensing_rows, licensing_totals) -> str:
    marketing = render_scope(
        "Scope A — marketing + product improvement", marketing_rows, marketing_totals
    )
    licensing = render_scope(
        "Scope B — third-party licensing", licensing_rows, licensing_totals
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Mind At Ease — Internal: consent by cohort and scope (staged proof, demo data)</title>
<style>
  body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#101418;background:#f7f8f9;margin:0}}
  .wrap{{max-width:860px;margin:0 auto;padding:36px 20px 60px}}
  h1{{font-size:23px;margin:0 0 4px}} h2{{font-size:18px;margin:24px 0 8px}}
  .sub{{color:#5b6572;font-size:13.5px;margin:0 0 18px;line-height:1.5}}
  table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e7ec;border-radius:12px;overflow:hidden;font-size:13.5px}}
  th,td{{padding:9px 12px;text-align:right;border-bottom:1px solid #eef1f4}}
  th{{background:#0d2b26;color:#fff;font-weight:650}} td:nth-child(2),td:nth-child(3){{text-align:left}}
  th:nth-child(2),th:nth-child(3){{text-align:left}} tr:last-child td{{border-bottom:0}}
  .tot{{margin-top:12px;background:#fff;border:1px solid #e3e7ec;border-radius:12px;padding:14px 18px;font-size:14px;line-height:1.7}}
  .gate{{margin-top:16px;background:#fff8e6;border:1px solid #ecd28a;border-radius:12px;padding:14px 18px;font-size:13.5px;line-height:1.6}}
</style></head><body><div class="wrap">
<h1>Data-release consent — cohort report by scope</h1>
<p class="sub">Internal view. Computed from consent records only, independently for each clause scope and current clause version. Cohort = signup month × trade (industry_slug) × plan — the AI Office tenant fields as they exist in the flow. <strong>Demo data</strong> (invented tenants) for this staged proof.</p>
{marketing}
{licensing}
<div class="gate"><strong>Standing gate:</strong> consent is not a license. No dataset is produced, shared, or licensed to a third party unless it passes the re-identification risk review (docs/REIDENTIFICATION-RISK-REVIEW.md) with a named human sign-off — and the owner's word.</div>
</div></body></html>"""


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "ui" / "cohort_report.html"
    store = ConsentStore()
    seed(store)
    marketing_rows = cohort_report(store, scope=SCOPE_MARKETING_IMPROVEMENT)
    licensing_rows = cohort_report(store, scope=SCOPE_LICENSING)
    marketing_totals = overall_totals(marketing_rows)
    licensing_totals = overall_totals(licensing_rows)
    out.write_text(render(marketing_rows, marketing_totals, licensing_rows, licensing_totals))
    print(f"wrote {out}")
    print(f"marketing totals: {marketing_totals}")
    print(f"licensing totals: {licensing_totals}")


if __name__ == "__main__":
    main()
