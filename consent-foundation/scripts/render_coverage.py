#!/usr/bin/env python3
"""Seed demo consent records and render the presentable coverage statement.

Demo data only — invented tenants, created deterministically. The report
itself is computed by consent/coverage.py from the records: counts and
rates only, consent method stated on its face, and any trade x metro
cell below the reporting floor rendered as "below reporting floor"
(the roofing x Smalltown cell is seeded small on purpose to prove it).

Usage: python3 scripts/render_coverage.py [out.html]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from consent.clause import SCOPE_LICENSING, SCOPE_MARKETING_IMPROVEMENT  # noqa: E402
from consent.coverage import coverage_report  # noqa: E402
from consent.store import ConsentStore  # noqa: E402

# (trade, metro, presented, scope A opt-ins, scope B opt-ins)
SEED = [
    ("hvac",       "Fort Worth", 20, 12, 9),
    ("roofing",    "Fort Worth", 14, 8, 7),
    ("plumbing",   "Dallas",     12, 5, 6),
    ("electrical", "Dallas",     11, 7, 4),
    ("roofing",    "Smalltown",   4, 3, 2),  # below the floor on purpose
]


def seed(store: ConsentStore) -> None:
    n = 0
    for trade, metro, presented, a_in, b_in in SEED:
        for i in range(presented):
            n += 1
            store.record_onboarding_decisions(
                tenant_id=f"coverage-demo-{n:03d}",
                business_name=f"Demo {trade.title()} Shop {n}",
                signup_month="2026-11", trade=trade, plan_id="starter",
                marketing_improvement_opted_in=i < a_in,
                licensing_opted_in=i < b_in,
                at="2026-11-15T12:00:00+00:00",
                metro=metro,
            )


def _val(v) -> str:
    return f"<strong>{v}</strong>" if isinstance(v, str) else str(v)


def _rate(v) -> str:
    return f"<strong>{v}</strong>" if isinstance(v, str) else f"<strong>{v:.1f}%</strong>"


def render_scope(label: str, payload: dict) -> str:
    rows = "\n".join(
        f"<tr><td class='l'>{r['trade']}</td><td class='l'>{r['metro']}</td>"
        f"<td>{_val(r['presented'])}</td><td>{_val(r['opted_in'])}</td>"
        f"<td>{_rate(r['opt_in_rate_pct'])}</td></tr>"
        for r in payload["rows"]
    )
    by_trade = " · ".join(
        f"{r['trade']}: {_rate(r['opt_in_rate_pct'])} of {_val(r['presented'])}"
        for r in payload["rollups"]["by_trade"]
    )
    by_metro = " · ".join(
        f"{r['metro']}: {_rate(r['opt_in_rate_pct'])} of {_val(r['presented'])}"
        for r in payload["rollups"]["by_metro"]
    )
    o = payload["rollups"]["overall"]
    return f"""
<h2>{label}</h2>
<table>
<tr><th class='l'>Trade</th><th class='l'>Metro</th><th>Contractors shown</th><th>Opted in</th><th>Opt-in rate</th></tr>
{rows}
</table>
<div class="tot"><strong>Overall:</strong> {_val(o['opted_in'])} of {_val(o['presented'])} contractors opted in — <strong>opt-in rate {_rate(o['opt_in_rate_pct'])}</strong>.<br>
<strong>By trade:</strong> {by_trade}<br><strong>By metro:</strong> {by_metro}</div>"""


def render(payload_a: dict, payload_b: dict) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Mind At Ease — Data coverage statement (staged proof, demo data)</title>
<style>
  body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#101418;background:#f7f8f9;margin:0}}
  .wrap{{max-width:860px;margin:0 auto;padding:36px 20px 60px}}
  h1{{font-size:23px;margin:0 0 4px}} h2{{font-size:18px;margin:24px 0 8px}}
  .sub{{color:#5b6572;font-size:13.5px;margin:0 0 18px;line-height:1.5}}
  .method{{background:#eafaf3;border:1px solid #a7e8c8;border-radius:12px;padding:14px 18px;font-size:14.5px;line-height:1.6}}
  table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e7ec;border-radius:12px;overflow:hidden;font-size:13.5px}}
  th,td{{padding:9px 12px;text-align:right;border-bottom:1px solid #eef1f4}}
  th{{background:#0d2b26;color:#fff;font-weight:650}} td.l,th.l{{text-align:left}} tr:last-child td{{border-bottom:0}}
  .tot{{margin-top:12px;background:#fff;border:1px solid #e3e7ec;border-radius:12px;padding:14px 18px;font-size:14px;line-height:1.7}}
  .gate{{margin-top:16px;background:#fff8e6;border:1px solid #ecd28a;border-radius:12px;padding:14px 18px;font-size:13.5px;line-height:1.6}}
</style></head><body><div class="wrap">
<h1>Data coverage — what share of our contractors opted in</h1>
<p class="sub">Counts and rates only — no business is named, and no per-business rows exist in this statement. Cells with fewer than {payload_a['reporting_floor']} contractors shown are reported as "below reporting floor" rather than an exact count, and rollups cover reportable cells only ({payload_a['excluded_below_floor_cells']} cell(s) excluded) so a small cell cannot be differenced back out of a total. <strong>Demo data</strong> (invented tenants) for this staged proof.</p>
<div class="method"><strong>Consent method: active opt-in, unchecked by default.</strong> Every contractor counted here saw the consent unchecked, read it, and checked the box themselves. Nobody was enrolled by default, and declining changed nothing about their account.</div>
{render_scope("Scope A — marketing + product improvement", payload_a)}
{render_scope("Scope B — third-party licensing", payload_b)}
<div class="gate"><strong>Standing gate:</strong> consent is not a license. No dataset is produced, shared, or licensed to a third party unless it passes the re-identification risk review (docs/REIDENTIFICATION-RISK-REVIEW.md) with a named human sign-off — and the owner's word.</div>
</div></body></html>"""


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "ui" / "coverage_report.html"
    store = ConsentStore()
    seed(store)
    payload_a = coverage_report(store, scope=SCOPE_MARKETING_IMPROVEMENT)
    payload_b = coverage_report(store, scope=SCOPE_LICENSING)
    out.write_text(render(payload_a, payload_b))
    print(f"wrote {out}")
    print(f"scope A overall: {payload_a['rollups']['overall']}")
    print(f"scope B overall: {payload_b['rollups']['overall']}")


if __name__ == "__main__":
    main()
