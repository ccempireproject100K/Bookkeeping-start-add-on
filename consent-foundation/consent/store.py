"""Consent record store for the two data-release consents.

Model: tenant metadata + one current-state row per consent scope + an
append-only event log.

Scopes are independent:

- ``marketing_improvement`` — the owner's original clause (Scope A).
- ``licensing`` — third-party licensing (Scope B), with router-drafted
  wording awaiting the owner's stamp.

A record is created for each scope when it is presented at onboarding,
capturing the tenant's decision and the clause version shown for that
scope. Neither checkbox is a condition of service: declining writes a
record with state ``declined`` and onboarding proceeds identically.

Any later change of mind appends an event to the log and updates that
scope's current state. History is never rewritten. An opt-out is honored
going forward: eligibility returns True only for a tenant whose CURRENT
state for the requested scope is ``opted_in`` under that scope's CURRENT
clause version.

If a clause version changes, a prior opt-in does not carry forward. The
scope state becomes ``reconsent_required`` until the tenant decides on the
new version. Declined and opted-out states remain as recorded; they were
not eligible under the old version either.

Contributor model (owner decision, 2026-11-11): give-back first. A tenant
whose data contributed to a release is entitled to the benchmark report
for its own trade × metro from that release. The contributor receipt
records that entitlement per release; report delivery is a later wiring
step and no report is produced here.

Account deletion is terminal for consent eligibility: both scopes move to
``account_deleted``, the log carries reason ``account deleted``, and the
tenant is excluded from every future release. Past releases are history
and are not rewritten.

Yearly re-confirmation applies to licensing only. If the licensing consent
passes its anniversary unanswered, it moves to
``reconfirmation_required`` and is ineligible until re-confirmed. The
marketing/product-improvement scope is left exactly as the tenant set it.

Storage here is SQLite (stdlib) so the staged module runs anywhere; the
live wave swaps the connection for the AI Office database without changing
this API.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .clause import (
    CLAUSE_VERSIONS,
    CONSENT_SCOPES,
    SCOPE_LICENSING,
    SCOPE_MARKETING_IMPROVEMENT,
)

STATE_OPTED_IN = "opted_in"
STATE_DECLINED = "declined"      # shown the clause, did not opt in
STATE_OPTED_OUT = "opted_out"    # opted in once, later withdrew
STATE_RECONSENT_REQUIRED = "reconsent_required"
STATE_RECONFIRMATION_REQUIRED = "reconfirmation_required"
STATE_ACCOUNT_DELETED = "account_deleted"

ACCOUNT_DELETED_REASON = "account deleted"
ANNUAL_RECONFIRMATION_LAPSE_REASON = "annual re-confirmation unanswered"

DEMO_RELEASE_LABEL = "DEMO / SEED — not a real release"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_instant(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _coerce_instant(value: str | datetime | None) -> str:
    if value is None:
        return _now()
    if isinstance(value, datetime):
        return _parse_instant(value).isoformat()
    return _parse_instant(value).isoformat()


def _add_one_year(value: str) -> str:
    parsed = _parse_instant(value)
    try:
        due = parsed.replace(year=parsed.year + 1)
    except ValueError:
        # Feb 29 anniversaries fall on Feb 28 in non-leap years.
        due = parsed.replace(year=parsed.year + 1, month=2, day=28)
    return due.isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS consent_records (
    tenant_id       TEXT PRIMARY KEY,
    business_name   TEXT NOT NULL,
    signup_month    TEXT NOT NULL,          -- 'YYYY-MM', cohort dimension
    trade           TEXT NOT NULL,          -- tenant industry_slug, cohort dimension
    plan_id         TEXT NOT NULL,          -- cohort dimension
    clause_version  TEXT NOT NULL,          -- Scope A version of latest A decision
    state           TEXT NOT NULL,          -- Scope A state, retained for compatibility
    decided_at      TEXT NOT NULL,          -- when the Scope A state was set
    presented_at    TEXT NOT NULL,          -- when the clauses were first shown
    metro           TEXT NOT NULL DEFAULT 'Fort Worth'  -- tenant metro, coverage dimension
);
CREATE TABLE IF NOT EXISTS consent_scopes (
    tenant_id       TEXT NOT NULL,
    scope           TEXT NOT NULL,          -- marketing_improvement | licensing
    clause_version  TEXT NOT NULL,          -- version attached to the current decision
    state           TEXT NOT NULL,          -- opted_in | declined | opted_out | reconsent_required | reconfirmation_required | account_deleted
    decided_at      TEXT NOT NULL,
    presented_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, scope)
);
CREATE TABLE IF NOT EXISTS consent_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT NOT NULL,
    event           TEXT NOT NULL,
    scope           TEXT NOT NULL,
    clause_version  TEXT NOT NULL,
    surface         TEXT NOT NULL,          -- onboarding | settings | system
    reason          TEXT,                   -- e.g. 'account deleted'
    at              TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consent_clause_versions (
    scope           TEXT PRIMARY KEY,
    clause_version  TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_registry (
    release_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    release_date    TEXT NOT NULL,
    scope           TEXT NOT NULL,
    cell_description TEXT NOT NULL,
    is_demo         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS release_contributions (
    tenant_id       TEXT NOT NULL,
    release_id      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, release_id)
);
"""


DEFAULT_METRO = "Fort Worth"


@dataclass
class ConsentRecord:
    tenant_id: str
    business_name: str
    signup_month: str
    trade: str
    plan_id: str
    clause_version: str
    state: str
    decided_at: str
    presented_at: str
    metro: str = DEFAULT_METRO


@dataclass
class ScopeConsentRecord:
    tenant_id: str
    scope: str
    clause_version: str
    state: str
    decided_at: str
    presented_at: str


@dataclass
class ReleaseRecord:
    release_id: str
    name: str
    release_date: str
    scope: str
    cell_description: str
    is_demo: bool

    @property
    def demo_label(self) -> str | None:
        return DEMO_RELEASE_LABEL if self.is_demo else None

    @property
    def benchmark_report_entitlement(self) -> str:
        """The contributor give-back recorded with a contributed release."""
        return "Your benchmark report"

    @property
    def benchmark_report_entitlement_detail(self) -> str:
        return "Benchmark picture for your own trade × metro from this release."

    @property
    def benchmark_report_delivery_status(self) -> str:
        # The entitlement is recorded now; producing/delivering the report is
        # a later wiring step, and no report is fabricated in this build.
        return "Entitlement recorded — report delivery wired later."


# Seed data for the staged contributor-receipt proof only. These are not
# real releases and are always returned/displayed with DEMO_RELEASE_LABEL.
DEMO_RELEASES = (
    ReleaseRecord(
        release_id="demo-seed-metro-services-overview",
        name="Demo Metro Services Overview",
        release_date="2026-09-30",
        scope=SCOPE_MARKETING_IMPROVEMENT,
        cell_description="HVAC x Fort Worth metro x established shops (seed example)",
        is_demo=True,
    ),
    ReleaseRecord(
        release_id="demo-seed-licensed-financing-insights",
        name="Demo Licensed Financing Insights",
        release_date="2026-10-31",
        scope=SCOPE_LICENSING,
        cell_description="Roofing x Fort Worth metro x financing-offering shops (seed example)",
        is_demo=True,
    ),
)


class ConsentStore:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for scope, version in CLAUSE_VERSIONS.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO consent_clause_versions VALUES (?,?,?)",
                (scope, version, _now()),
            )
        self.conn.commit()

    # -- scope/version helpers -------------------------------------------
    @staticmethod
    def _validate_scope(scope: str) -> str:
        if scope not in CONSENT_SCOPES:
            raise ValueError(f"unknown consent scope {scope!r}")
        return scope

    def current_clause_version(self, scope: str = SCOPE_MARKETING_IMPROVEMENT) -> str:
        self._validate_scope(scope)
        row = self.conn.execute(
            "SELECT clause_version FROM consent_clause_versions WHERE scope=?", (scope,)
        ).fetchone()
        return row["clause_version"]

    def publish_clause_version(self, scope: str, new_version: str,
                               at: str | None = None) -> str:
        """Publish a new version for ONE scope without carrying opt-ins.

        Tenants who were opted in under the superseded version move to
        ``reconsent_required`` for this scope only. Their recorded version
        remains the version they actually accepted; the transition is in
        the append-only log under the new version.
        """
        self._validate_scope(scope)
        if not new_version or not new_version.strip():
            raise ValueError("new clause version must be non-empty")
        at = at or _now()
        old_version = self.current_clause_version(scope)
        if new_version == old_version:
            return old_version

        self.conn.execute(
            "UPDATE consent_clause_versions SET clause_version=?, updated_at=? WHERE scope=?",
            (new_version, at, scope),
        )
        stale = self.conn.execute(
            "SELECT * FROM consent_scopes WHERE scope=? AND state IN (?,?) "
            "AND clause_version<>?",
            (scope, STATE_OPTED_IN, STATE_RECONSENT_REQUIRED, new_version),
        ).fetchall()
        for row in stale:
            tenant_id = row["tenant_id"]
            self.conn.execute(
                "UPDATE consent_scopes SET state=?, decided_at=? WHERE tenant_id=? AND scope=?",
                (STATE_RECONSENT_REQUIRED, at, tenant_id, scope),
            )
            if scope == SCOPE_MARKETING_IMPROVEMENT:
                self.conn.execute(
                    "UPDATE consent_records SET state=?, decided_at=? WHERE tenant_id=?",
                    (STATE_RECONSENT_REQUIRED, at, tenant_id),
                )
            self._log(
                tenant_id,
                "clause_version_changed_reconsent_required",
                "system",
                at,
                scope=scope,
                clause_version=new_version,
            )
        self.conn.commit()
        return new_version

    # -- onboarding ------------------------------------------------------
    def _insert_tenant(self, tenant_id: str, business_name: str,
                       signup_month: str, trade: str, plan_id: str,
                       scope_a_opted_in: bool, at: str,
                       metro: str = DEFAULT_METRO) -> None:
        self.conn.execute(
            "INSERT INTO consent_records VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                tenant_id,
                business_name,
                signup_month,
                trade,
                plan_id,
                self.current_clause_version(SCOPE_MARKETING_IMPROVEMENT),
                STATE_OPTED_IN if scope_a_opted_in else STATE_DECLINED,
                at,
                at,
                metro or DEFAULT_METRO,
            ),
        )

    def _record_scope_onboarding(self, tenant_id: str, scope: str,
                                 opted_in: bool, at: str) -> ScopeConsentRecord:
        version = self.current_clause_version(scope)
        state = STATE_OPTED_IN if opted_in else STATE_DECLINED
        event = "presented_opt_in" if opted_in else "presented_declined"
        self.conn.execute(
            "INSERT INTO consent_scopes VALUES (?,?,?,?,?,?)",
            (tenant_id, scope, version, state, at, at),
        )
        self._log(tenant_id, event, "onboarding", at, scope=scope, clause_version=version)
        return self.get_scope(tenant_id, scope)

    def record_onboarding_decision(
        self,
        tenant_id: str,
        business_name: str,
        signup_month: str,
        trade: str,
        plan_id: str,
        opted_in: bool,
        at: str | None = None,
        metro: str = DEFAULT_METRO,
    ) -> ConsentRecord:
        """Legacy single-scope onboarding entry point for Scope A.

        ``opted_in`` is exactly the state of the separate Scope A checkbox.
        It has no effect on whether the account is created.
        """
        at = at or _now()
        self._insert_tenant(tenant_id, business_name, signup_month, trade, plan_id, opted_in, at, metro=metro)
        self._record_scope_onboarding(tenant_id, SCOPE_MARKETING_IMPROVEMENT, opted_in, at)
        self.conn.commit()
        return self.get(tenant_id)

    def record_onboarding_decisions(
        self,
        tenant_id: str,
        business_name: str,
        signup_month: str,
        trade: str,
        plan_id: str,
        marketing_improvement_opted_in: bool,
        licensing_opted_in: bool,
        at: str | None = None,
        metro: str = DEFAULT_METRO,
    ) -> dict[str, ScopeConsentRecord]:
        """Record both independent onboarding decisions for a tenant."""
        at = at or _now()
        self._insert_tenant(
            tenant_id, business_name, signup_month, trade, plan_id,
            marketing_improvement_opted_in, at, metro=metro,
        )
        records = {
            SCOPE_MARKETING_IMPROVEMENT: self._record_scope_onboarding(
                tenant_id, SCOPE_MARKETING_IMPROVEMENT,
                marketing_improvement_opted_in, at,
            ),
            SCOPE_LICENSING: self._record_scope_onboarding(
                tenant_id, SCOPE_LICENSING, licensing_opted_in, at,
            ),
        }
        self.conn.commit()
        return records

    # -- change of mind (settings / contributor receipt) ------------------
    def change_scope_consent(self, tenant_id: str, scope: str, opted_in: bool,
                             surface: str = "settings",
                             at: str | None = None) -> ScopeConsentRecord:
        """Record a later opt-in or opt-out for exactly one scope."""
        self._validate_scope(scope)
        rec = self.get_scope(tenant_id, scope)
        if rec is None:
            raise KeyError(f"no {scope} consent record for tenant {tenant_id!r}")
        if rec.state == STATE_ACCOUNT_DELETED:
            raise ValueError("account deleted")
        at = at or _now()
        current_version = self.current_clause_version(scope)

        if opted_in:
            new_state = STATE_OPTED_IN
            version = current_version
            if rec.state == STATE_RECONSENT_REQUIRED:
                event = "reconsent_opt_in"
            elif rec.state == STATE_RECONFIRMATION_REQUIRED:
                event = "annual_reconfirmation_confirmed"
            else:
                event = "opt_in"
        elif rec.state == STATE_OPTED_IN:
            new_state = STATE_OPTED_OUT
            version = rec.clause_version
            event = "opt_out"
        elif rec.state == STATE_RECONSENT_REQUIRED:
            new_state = STATE_DECLINED
            version = current_version
            event = "reconsent_declined"
        elif rec.state == STATE_RECONFIRMATION_REQUIRED:
            new_state = STATE_OPTED_OUT
            version = rec.clause_version
            event = "annual_reconfirmation_declined"
        else:
            new_state = STATE_DECLINED
            version = current_version
            event = "presented_declined"

        self.conn.execute(
            "UPDATE consent_scopes SET clause_version=?, state=?, decided_at=? "
            "WHERE tenant_id=? AND scope=?",
            (version, new_state, at, tenant_id, scope),
        )
        if scope == SCOPE_MARKETING_IMPROVEMENT:
            self.conn.execute(
                "UPDATE consent_records SET clause_version=?, state=?, decided_at=? WHERE tenant_id=?",
                (version, new_state, at, tenant_id),
            )
        self._log(tenant_id, event, surface, at, scope=scope, clause_version=version)
        self.conn.commit()
        return self.get_scope(tenant_id, scope)

    def change_consent(self, tenant_id: str, opted_in: bool,
                       surface: str = "settings", at: str | None = None,
                       scope: str = SCOPE_MARKETING_IMPROVEMENT):
        """Backward-compatible change API; Scope A returns ConsentRecord."""
        changed = self.change_scope_consent(tenant_id, scope, opted_in, surface, at)
        if scope == SCOPE_MARKETING_IMPROVEMENT:
            return self.get(tenant_id)
        return changed

    # -- yearly licensing re-confirmation ---------------------------------
    def annual_reconfirmation_due_at(self, tenant_id: str) -> str:
        """Anniversary of the licensing scope's last recorded decision."""
        rec = self.get_scope(tenant_id, SCOPE_LICENSING)
        if rec is None:
            raise KeyError(f"no licensing consent record for tenant {tenant_id!r}")
        return _add_one_year(rec.decided_at)

    def apply_annual_reconfirmation_check(
        self,
        tenant_id: str | None = None,
        now: str | datetime | None = None,
        at: str | datetime | None = None,
    ) -> list[str]:
        """Apply the annual lapse using an injectable clock.

        ``now`` (or its alias ``at``) may be an ISO string or datetime, so
        tests can cross an anniversary without waiting. Only an opted-in
        licensing scope that has reached its anniversary transitions. The
        marketing/product-improvement scope is never touched here.
        """
        checked_at = _coerce_instant(now if now is not None else at)
        if tenant_id is None:
            candidates = self.all_scope_records(SCOPE_LICENSING)
        else:
            rec = self.get_scope(tenant_id, SCOPE_LICENSING)
            candidates = [rec] if rec is not None else []

        transitioned: list[str] = []
        for rec in candidates:
            if rec.state != STATE_OPTED_IN:
                continue
            if _parse_instant(checked_at) < _parse_instant(_add_one_year(rec.decided_at)):
                continue
            # Preserve decided_at: it remains the last actual licensing
            # decision/confirmation, and therefore the anniversary anchor.
            self.conn.execute(
                "UPDATE consent_scopes SET state=? WHERE tenant_id=? AND scope=?",
                (STATE_RECONFIRMATION_REQUIRED, rec.tenant_id, SCOPE_LICENSING),
            )
            self._log(
                rec.tenant_id,
                "annual_reconfirmation_lapsed",
                "system",
                checked_at,
                scope=SCOPE_LICENSING,
                clause_version=rec.clause_version,
                reason=ANNUAL_RECONFIRMATION_LAPSE_REASON,
            )
            transitioned.append(rec.tenant_id)
        self.conn.commit()
        return transitioned

    # Alias with cycle language for callers wiring a scheduler later.
    def run_annual_reconfirmation_cycle(
        self,
        tenant_id: str | None = None,
        now: str | datetime | None = None,
        at: str | datetime | None = None,
    ) -> list[str]:
        return self.apply_annual_reconfirmation_check(tenant_id, now=now, at=at)

    def record_annual_reconfirmation(
        self,
        tenant_id: str,
        reconfirmed: bool,
        surface: str = "settings",
        at: str | datetime | None = None,
    ) -> ScopeConsentRecord:
        """Record the tenant's answer to the yearly licensing check.

        A yes restores/continues licensing under the current clause version
        and starts a new annual cycle. A no withdraws licensing. Scope A is
        not changed either way — it continues exactly as the tenant left it.
        """
        rec = self.get_scope(tenant_id, SCOPE_LICENSING)
        if rec is None:
            raise KeyError(f"no licensing consent record for tenant {tenant_id!r}")
        if rec.state == STATE_ACCOUNT_DELETED:
            raise ValueError("account deleted")
        if rec.state not in (STATE_OPTED_IN, STATE_RECONFIRMATION_REQUIRED):
            raise ValueError("annual re-confirmation applies only to an opted-in licensing consent")
        answered_at = _coerce_instant(at)

        if reconfirmed:
            new_state = STATE_OPTED_IN
            version = self.current_clause_version(SCOPE_LICENSING)
            event = "annual_reconfirmation_confirmed"
        else:
            new_state = STATE_OPTED_OUT
            version = rec.clause_version
            event = "annual_reconfirmation_declined"

        self.conn.execute(
            "UPDATE consent_scopes SET clause_version=?, state=?, decided_at=? "
            "WHERE tenant_id=? AND scope=?",
            (version, new_state, answered_at, tenant_id, SCOPE_LICENSING),
        )
        self._log(
            tenant_id, event, surface, answered_at,
            scope=SCOPE_LICENSING, clause_version=version,
        )
        self.conn.commit()
        return self.get_scope(tenant_id, SCOPE_LICENSING)

    # -- account deletion ---------------------------------------------------
    def delete_account(
        self,
        tenant_id: str,
        at: str | datetime | None = None,
        surface: str = "settings",
    ) -> ConsentRecord:
        """Propagate account deletion to both consent scopes.

        Both scopes become ``account_deleted`` and each append-only log entry
        carries reason ``account deleted``. Eligibility checks and future
        cohort reports therefore exclude the tenant automatically. Release
        history is not rewritten: a contribution already in a past release
        remains a past-release fact.
        """
        tenant = self.get(tenant_id)
        if tenant is None:
            raise KeyError(f"no consent record for tenant {tenant_id!r}")
        if tenant.state == STATE_ACCOUNT_DELETED:
            return tenant
        deleted_at = _coerce_instant(at)

        for scope in CONSENT_SCOPES:
            rec = self.get_scope(tenant_id, scope)
            if rec is None:
                continue
            self.conn.execute(
                "UPDATE consent_scopes SET state=?, decided_at=? WHERE tenant_id=? AND scope=?",
                (STATE_ACCOUNT_DELETED, deleted_at, tenant_id, scope),
            )
            self._log(
                tenant_id,
                "account_deleted",
                surface,
                deleted_at,
                scope=scope,
                clause_version=rec.clause_version,
                reason=ACCOUNT_DELETED_REASON,
            )
        self.conn.execute(
            "UPDATE consent_records SET state=?, decided_at=? WHERE tenant_id=?",
            (STATE_ACCOUNT_DELETED, deleted_at, tenant_id),
        )
        self.conn.commit()
        return self.get(tenant_id)

    # Readable alias for account-lifecycle callers.
    def record_account_deletion(
        self,
        tenant_id: str,
        at: str | datetime | None = None,
        surface: str = "settings",
    ) -> ConsentRecord:
        return self.delete_account(tenant_id, at=at, surface=surface)

    # -- reads ------------------------------------------------------------
    def get(self, tenant_id: str) -> ConsentRecord | None:
        row = self.conn.execute(
            "SELECT * FROM consent_records WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        return ConsentRecord(**dict(row)) if row else None

    def get_scope(self, tenant_id: str,
                  scope: str = SCOPE_MARKETING_IMPROVEMENT) -> ScopeConsentRecord | None:
        self._validate_scope(scope)
        row = self.conn.execute(
            "SELECT * FROM consent_scopes WHERE tenant_id=? AND scope=?", (tenant_id, scope)
        ).fetchone()
        return ScopeConsentRecord(**dict(row)) if row else None

    def all_records(self) -> list[ConsentRecord]:
        rows = self.conn.execute("SELECT * FROM consent_records").fetchall()
        return [ConsentRecord(**dict(r)) for r in rows]

    def all_scope_records(self, scope: str | None = None) -> list[ScopeConsentRecord]:
        if scope is None:
            rows = self.conn.execute(
                "SELECT * FROM consent_scopes ORDER BY tenant_id, scope"
            ).fetchall()
        else:
            self._validate_scope(scope)
            rows = self.conn.execute(
                "SELECT * FROM consent_scopes WHERE scope=? ORDER BY tenant_id", (scope,)
            ).fetchall()
        return [ScopeConsentRecord(**dict(r)) for r in rows]

    # -- nudge events (onboarding pop-up) ----------------------------------
    NUDGE_EVENTS = ("nudge_shown", "nudge_accepted", "nudge_dismissed")

    def record_nudge_event(self, tenant_id: str, scope: str, event: str,
                           surface: str = "onboarding",
                           at: str | None = None,
                           session_id: str | None = None) -> None:
        """Append one nudge event to the log for a tenant + scope.

        The log is the measurement source for nudge-shown → opted-in
        conversion in cohort reporting. Recording an event never changes
        any consent state — a box is checked only by the user's own tap,
        which the onboarding flow records separately as a consent decision.
        """
        self._validate_scope(scope)
        if event not in self.NUDGE_EVENTS:
            raise ValueError(f"unknown nudge event {event!r}")
        self._log(
            tenant_id, event, surface, at or _now(), scope=scope,
            reason=f"session:{session_id}" if session_id else None,
        )
        self.conn.commit()

    def all_events(self, scope: str | None = None) -> list[dict]:
        if scope is None:
            rows = self.conn.execute(
                "SELECT * FROM consent_events ORDER BY id"
            ).fetchall()
        else:
            self._validate_scope(scope)
            rows = self.conn.execute(
                "SELECT * FROM consent_events WHERE scope=? ORDER BY id", (scope,)
            ).fetchall()
        return [dict(r) for r in rows]

    def events(self, tenant_id: str, scope: str | None = None) -> list[dict]:
        if scope is None:
            rows = self.conn.execute(
                "SELECT * FROM consent_events WHERE tenant_id=? ORDER BY id", (tenant_id,)
            ).fetchall()
        else:
            self._validate_scope(scope)
            rows = self.conn.execute(
                "SELECT * FROM consent_events WHERE tenant_id=? AND scope=? ORDER BY id",
                (tenant_id, scope),
            ).fetchall()
        return [dict(r) for r in rows]

    def is_eligible_for_aggregation(self, tenant_id: str,
                                    scope: str = SCOPE_MARKETING_IMPROVEMENT) -> bool:
        """Only a CURRENT-version, CURRENT opt-in for this scope may contribute."""
        rec = self.get_scope(tenant_id, scope)
        return bool(
            rec
            and rec.state == STATE_OPTED_IN
            and rec.clause_version == self.current_clause_version(scope)
        )

    def is_eligible_for_scope(self, tenant_id: str, scope: str) -> bool:
        return self.is_eligible_for_aggregation(tenant_id, scope)

    def is_eligible_for_marketing_improvement(self, tenant_id: str) -> bool:
        return self.is_eligible_for_aggregation(tenant_id, SCOPE_MARKETING_IMPROVEMENT)

    def is_eligible_for_licensing(self, tenant_id: str) -> bool:
        return self.is_eligible_for_aggregation(tenant_id, SCOPE_LICENSING)

    # -- contributor receipt / seeded release registry --------------------
    def seed_demo_release_registry(self, tenant_ids: Iterable[str]) -> list[ReleaseRecord]:
        """Attach the staged DEMO releases to tenants for receipt proof.

        This creates registry fixtures only. It does not produce, share, or
        license any dataset.
        """
        tenant_ids = list(tenant_ids)
        for tenant_id in tenant_ids:
            if self.get(tenant_id) is None:
                raise KeyError(f"no consent record for tenant {tenant_id!r}")
        for release in DEMO_RELEASES:
            self.conn.execute(
                "INSERT OR IGNORE INTO release_registry VALUES (?,?,?,?,?,?)",
                (
                    release.release_id, release.name, release.release_date,
                    release.scope, release.cell_description, int(release.is_demo),
                ),
            )
            for tenant_id in tenant_ids:
                # A fixture contribution is attached only when the tenant
                # actually opted in to that release's scope at some point;
                # a declined scope is never shown as a contributor, and an
                # account-deleted tenant receives no new attachments.
                if self.get(tenant_id).state == STATE_ACCOUNT_DELETED:
                    continue
                event_names = [
                    e["event"] for e in self.events(tenant_id, scope=release.scope)
                ]
                ever_opted_in = any(
                    name in event_names
                    for name in ("presented_opt_in", "opt_in", "reconsent_opt_in")
                )
                if ever_opted_in:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO release_contributions VALUES (?,?)",
                        (tenant_id, release.release_id),
                    )
        self.conn.commit()
        attached: list[ReleaseRecord] = []
        seen: set[str] = set()
        for tenant_id in tenant_ids:
            for release in self.release_registry_for_tenant(tenant_id):
                if release.release_id not in seen:
                    seen.add(release.release_id)
                    attached.append(release)
        return attached

    def release_registry_for_tenant(self, tenant_id: str) -> list[ReleaseRecord]:
        rows = self.conn.execute(
            "SELECT r.* FROM release_registry r "
            "JOIN release_contributions c ON c.release_id=r.release_id "
            "WHERE c.tenant_id=? ORDER BY r.release_date, r.release_id",
            (tenant_id,),
        ).fetchall()
        releases = []
        for row in rows:
            data = dict(row)
            data["is_demo"] = bool(data["is_demo"])
            releases.append(ReleaseRecord(**data))
        return releases

    def contributor_receipt(self, tenant_id: str) -> dict:
        tenant = self.get(tenant_id)
        if tenant is None:
            raise KeyError(f"no consent record for tenant {tenant_id!r}")
        consents = {}
        for scope in CONSENT_SCOPES:
            rec = self.get_scope(tenant_id, scope)
            consents[scope] = {
                "record": rec,
                "current_clause_version": self.current_clause_version(scope),
                "eligible_now": self.is_eligible_for_aggregation(tenant_id, scope),
            }
        licensing = self.get_scope(tenant_id, SCOPE_LICENSING)
        return {
            "tenant": tenant,
            "account_deleted": tenant.state == STATE_ACCOUNT_DELETED,
            "consents": consents,
            "annual_reconfirmation": {
                "scope": SCOPE_LICENSING,
                "state": licensing.state if licensing else None,
                "pending": bool(licensing and licensing.state == STATE_RECONFIRMATION_REQUIRED),
                "due_at": self.annual_reconfirmation_due_at(tenant_id) if licensing else None,
            },
            "releases": self.release_registry_for_tenant(tenant_id),
        }

    def _log(self, tenant_id: str, event: str, surface: str, at: str,
             scope: str = SCOPE_MARKETING_IMPROVEMENT,
             clause_version: str | None = None,
             reason: str | None = None) -> None:
        self._validate_scope(scope)
        self.conn.execute(
            "INSERT INTO consent_events "
            "(tenant_id, event, scope, clause_version, surface, reason, at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                tenant_id,
                event,
                scope,
                clause_version or self.current_clause_version(scope),
                surface,
                reason,
                at,
            ),
        )
