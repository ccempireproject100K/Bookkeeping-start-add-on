"""The one-time onboarding nudge — inform, never manufacture consent.

Owner's words (2026-11-11): "before they leave the page, kind of like
click it and then pop it up. To let them know about the benchmark report
and how important it is for them."

When a user tries to continue past the consent step with a scope
UNCHECKED, a pop-up for that scope appears BEFORE they leave the page. It
explains, in plain words, what they would get — the benchmark report for
their trade and metro — and carries the owner's line verbatim:

    "It's 100% your choice and we respect your decision either way."

Hard rules (all test-pinned):

(a) At most ONCE per scope per signup session. Dismissing it means it
    never pops again in that flow.
(b) The nudge never checks a box by itself. Only the user's explicit
    "Yes, count me in" tap checks it — and that tap is the user's own
    click, recorded as such.
(c) It never blocks account creation. After a dismissal the flow
    continues exactly as before (the gate in gate.py is untouched:
    terms alone gate the account).
(d) It respects the split: a checked scope never nudges; an unchecked
    scope nudges on its own. Both checked -> no nudge at all.
(e) Every show / accept / dismiss is written to the append-only log
    (``nudge_shown`` / ``nudge_accepted`` / ``nudge_dismissed``) so
    cohort reporting can measure nudge-shown -> opted-in conversion.
(f) Copy is UI copy, not clause language: short, honest, no pressure
    language, no countdowns, no dark patterns. The dismissal button is
    as prominent as the accept button.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .clause import CONSENT_SCOPES, SCOPE_LICENSING, SCOPE_MARKETING_IMPROVEMENT
from .store import ConsentStore

# The owner's line, in substance verbatim (2026-11-11). It sits visibly in
# the pop-up body — not buried — so the respect for a "no" is part of what
# the user reads before choosing.
OWNER_RESPECT_LINE = "It's 100% your choice and we respect your decision either way."

ACCEPT_LABEL = "Yes, count me in"
DISMISS_LABEL = "No thanks, continue"


@dataclass(frozen=True)
class NudgeCopy:
    scope: str
    title: str
    body: str
    owner_line: str = OWNER_RESPECT_LINE
    accept_label: str = ACCEPT_LABEL
    dismiss_label: str = DISMISS_LABEL


NUDGE_COPY = {
    SCOPE_MARKETING_IMPROVEMENT: NudgeCopy(
        scope=SCOPE_MARKETING_IMPROVEMENT,
        title="Before you go — your benchmark report",
        body=(
            "Shops that share their numbers get the benchmark report for "
            "their trade and metro: how your ticket size and workload "
            "stack up against shops like yours. It only exists because "
            "shops contribute, and your business is never named in it."
        ),
    ),
    SCOPE_LICENSING: NudgeCopy(
        scope=SCOPE_LICENSING,
        title="Before you go — your benchmark report",
        body=(
            "Shops that opt in to licensing get the benchmark report for "
            "their trade and metro: how your numbers stack up against "
            "shops like yours. Pooled insights may be licensed to third "
            "parties with all identifying details removed — your "
            "business is never named."
        ),
    ),
}


@dataclass
class NudgeSession:
    """One signup session's nudge state for one prospective tenant.

    ``checked`` maps scope -> the user's current checkbox state. The
    session object never mutates it: showing a prompt changes nothing,
    and ``accept`` returns the scope whose box the user's tap checks —
    the tap itself does the checking in the UI.
    """

    store: ConsentStore
    tenant_id: str
    session_id: str = "signup-session"
    _shown: set = field(default_factory=set)
    _resolved: set = field(default_factory=set)

    def next_prompt(self, checked: dict) -> NudgeCopy | None:
        """Return (and log) the next nudge to show, or None to proceed.

        One prompt at a time, in scope order. A scope prompts only if it
        is unchecked AND has not been shown yet in this session.
        """
        for scope in CONSENT_SCOPES:
            if checked.get(scope, False):
                continue
            if scope in self._shown:
                continue
            self._shown.add(scope)
            self.store.record_nudge_event(
                self.tenant_id, scope, "nudge_shown",
                session_id=self.session_id,
            )
            return NUDGE_COPY[scope]
        return None

    def accept(self, scope: str) -> str:
        """Record the user's own "count me in" tap for a shown scope.

        Returns the scope so the caller checks that scope's box — the
        check is the user's click, never the nudge acting on its own.
        """
        self._require_shown_unresolved(scope)
        self._resolved.add(scope)
        self.store.record_nudge_event(
            self.tenant_id, scope, "nudge_accepted",
            session_id=self.session_id,
        )
        return scope

    def dismiss(self, scope: str) -> None:
        """Record a dismissal. The scope never prompts again this session."""
        self._require_shown_unresolved(scope)
        self._resolved.add(scope)
        self.store.record_nudge_event(
            self.tenant_id, scope, "nudge_dismissed",
            session_id=self.session_id,
        )

    def _require_shown_unresolved(self, scope: str) -> None:
        if scope not in CONSENT_SCOPES:
            raise ValueError(f"unknown consent scope {scope!r}")
        if scope not in self._shown:
            raise ValueError(f"nudge for scope {scope!r} was not shown in this session")
        if scope in self._resolved:
            raise ValueError(f"nudge for scope {scope!r} already resolved in this session")

    @property
    def shown_scopes(self) -> tuple:
        return tuple(s for s in CONSENT_SCOPES if s in self._shown)


def nudge_conversion(store: ConsentStore, scope: str | None = None) -> dict:
    """Nudge-shown -> opted-in conversion, from the append-only log.

    Counts DISTINCT tenants per scope: shown, accepted (the user tapped
    "count me in"), dismissed, and the accept rate among tenants shown.
    """
    scopes = (scope,) if scope else CONSENT_SCOPES
    report = {}
    for sc in scopes:
        shown, accepted, dismissed = set(), set(), set()
        for e in store.all_events(scope=sc):
            if e["event"] == "nudge_shown":
                shown.add(e["tenant_id"])
            elif e["event"] == "nudge_accepted":
                accepted.add(e["tenant_id"])
            elif e["event"] == "nudge_dismissed":
                dismissed.add(e["tenant_id"])
        report[sc] = {
            "shown_tenants": len(shown),
            "accepted_tenants": len(accepted),
            "dismissed_tenants": len(dismissed),
            "accept_rate": round(len(accepted) / len(shown), 4) if shown else None,
        }
    return report
