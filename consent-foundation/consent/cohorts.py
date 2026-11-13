"""Cohort opt-in / opt-out reporting, computed from consent records only.

Cohorts are reported independently for each consent scope. The default
scope report keeps every non-deleted tenant presented with that scope in
the cohort denominator, while ``opted_in_now`` counts only an opt-in that
is current for the scope's clause version in force. A superseded opt-in
therefore appears as ``reconsent_required`` and contributes 0 to the
opt-in rate. Account-deleted tenants are excluded entirely, so future
cohort rates are recomputed without them. Pass a specific
``clause_version`` to filter the report to records made under that version.

Cohort dimensions are the AI Office tenant fields as they exist in the real
flow: signup month (from the tenant's signup date), trade (the tenant's
``industry_slug``, applied at the signup trade step), and plan (``plan_id``).

Definitions within the selected scope (and version, when one is specified):
- presented       = non-deleted tenants with a consent record for that scope
- opted_in_now    = current state is opted_in under the version in force
- opted_out_ever  = tenants who opted in at some point and later withdrew
                    (counted from the append-only event log)
- declined        = presented, never opted in under that version
- opt_in_rate     = opted_in_now / presented
- opt_out_rate    = opted_out_ever / ever_opted_in   (None if nobody ever opted in)
"""

from __future__ import annotations

from collections import defaultdict

from .clause import CONSENT_SCOPES, SCOPE_MARKETING_IMPROVEMENT
from .store import (
    ConsentStore,
    STATE_ACCOUNT_DELETED,
    STATE_OPTED_IN,
    STATE_OPTED_OUT,
    STATE_RECONFIRMATION_REQUIRED,
    STATE_RECONSENT_REQUIRED,
)


def cohort_report(
    store: ConsentStore,
    scope: str = SCOPE_MARKETING_IMPROVEMENT,
    clause_version: str | None = None,
) -> list[dict]:
    """One row per (signup_month, trade, plan_id) cohort for one scope."""
    selected_version = clause_version
    scope_records = [
        r for r in store.all_scope_records(scope)
        if r.state != STATE_ACCOUNT_DELETED
    ]
    if selected_version is not None:
        scope_records = [r for r in scope_records if r.clause_version == selected_version]

    tenants = {r.tenant_id: r for r in store.all_records()}
    ever_in: dict[str, bool] = {}
    out_ever: dict[str, bool] = {}
    for rec in scope_records:
        events = store.events(rec.tenant_id, scope=scope)
        if selected_version is not None:
            events = [e for e in events if e["clause_version"] == selected_version]
        event_names = [e["event"] for e in events]
        ever_in[rec.tenant_id] = any(
            name in event_names
            for name in ("presented_opt_in", "opt_in", "reconsent_opt_in")
        )
        out_ever[rec.tenant_id] = "opt_out" in event_names

    groups: dict[tuple[str, str, str], list] = defaultdict(list)
    for rec in scope_records:
        tenant = tenants.get(rec.tenant_id)
        if tenant is not None:
            groups[(tenant.signup_month, tenant.trade, tenant.plan_id)].append(rec)

    rows = []
    current_version = store.current_clause_version(scope)
    for (month, trade, plan_id), members in sorted(groups.items()):
        presented = len(members)
        opted_in_now = sum(
            1 for m in members
            if m.state == STATE_OPTED_IN and m.clause_version == current_version
        )
        ever_opted_in = sum(1 for m in members if ever_in[m.tenant_id])
        opted_out_ever = sum(1 for m in members if out_ever[m.tenant_id])
        declined = sum(1 for m in members if not ever_in[m.tenant_id])
        rows.append({
            "scope": scope,
            "clause_version": selected_version or "all",
            "signup_month": month,
            "trade": trade,
            "plan_id": plan_id,
            "presented": presented,
            "opted_in_now": opted_in_now,
            "ever_opted_in": ever_opted_in,
            "opted_out_ever": opted_out_ever,
            "opted_out_state_now": sum(1 for m in members if m.state == STATE_OPTED_OUT),
            "reconsent_required": sum(
                1 for m in members if m.state == STATE_RECONSENT_REQUIRED
            ),
            "reconfirmation_required": sum(
                1 for m in members if m.state == STATE_RECONFIRMATION_REQUIRED
            ),
            "declined": declined,
            "opt_in_rate": round(opted_in_now / presented, 4) if presented else 0.0,
            "opt_out_rate": round(opted_out_ever / ever_opted_in, 4) if ever_opted_in else None,
        })
    return rows


def cohort_reports_by_scope(store: ConsentStore) -> dict[str, list[dict]]:
    """Cohort rows for every consent scope, with current opt-ins counted."""
    return {scope: cohort_report(store, scope=scope) for scope in CONSENT_SCOPES}


def overall_totals(rows: list[dict]) -> dict:
    presented = sum(r["presented"] for r in rows)
    opted_in_now = sum(r["opted_in_now"] for r in rows)
    ever_opted_in = sum(r["ever_opted_in"] for r in rows)
    opted_out_ever = sum(r["opted_out_ever"] for r in rows)
    return {
        "presented": presented,
        "opted_in_now": opted_in_now,
        "ever_opted_in": ever_opted_in,
        "opted_out_ever": opted_out_ever,
        "declined": sum(r["declined"] for r in rows),
        "opt_in_rate": round(opted_in_now / presented, 4) if presented else 0.0,
        "opt_out_rate": round(opted_out_ever / ever_opted_in, 4) if ever_opted_in else None,
    }
