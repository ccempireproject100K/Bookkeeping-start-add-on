"""Labor-burden seam for job costing (job-costing requests report, item 2).

The full burden feature — per-employee / per-tenant burden rates, overhead
allocation, burden in the profit view — is being built on its own branch
(feat/job-costing-committed-burden). This module is the READ seam the rest of
the posting code uses today, so that when that lane lands its config, field
capture (and any other poster) picks the rate up with no rework:

  * `tenant_burden_percent(db, tenant_id)` looks for a configured burden
    percent on the tenant document, in priority order:
        tenant.job_costing.burden_percent
        tenant.settings.burden_percent
        tenant.payroll.burden_percent
        tenant.burden_percent
    and returns it as a float percent (e.g. 44.0), or None when nothing is
    configured — the state of main today, where burden simply does not exist.

  * `burdened_cents(amount_cents, percent)` returns the fully-loaded figure.

Posting rule (owned by the burden lane, honored here): the GL always posts
the BASE amount; the burdened figure is stored ALONGSIDE the job-cost line
(`burdened_amount_cents`) rather than overwriting `amount_cents`.
"""


async def tenant_burden_percent(db, tenant_id: str):
    """Configured burden % for the tenant, or None when unconfigured."""
    t = await db.tenants.find_one({"id": tenant_id}, {"_id": 0}) or {}
    candidates = (
        (t.get("job_costing") or {}).get("burden_percent"),
        (t.get("settings") or {}).get("burden_percent"),
        (t.get("payroll") or {}).get("burden_percent"),
        t.get("burden_percent"),
    )
    for val in candidates:
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def burdened_cents(amount_cents: int, percent) -> int:
    """Base amount grossed up by `percent` (44.0 -> x1.44), rounded to cents."""
    if percent is None:
        return amount_cents
    return int(round(amount_cents * (1 + float(percent) / 100.0)))
