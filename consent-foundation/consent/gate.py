"""Onboarding gate logic for the two data-release consents.

The rule that matters: each consent checkbox is INDEPENDENT of the main
terms and of the other consent. Account creation is gated on the main terms
only. Either consent can be accepted or declined freely, in any combination,
and declining either changes nothing about the service the tenant receives.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OnboardingDecision:
    can_create_account: bool
    block_reason: str | None
    data_release_opted_in: bool  # Scope A, passed through to the consent store
    licensing_opted_in: bool = False  # Scope B, independently passed through


def evaluate_onboarding(
    terms_accepted: bool,
    data_release_checked: bool,
    licensing_checked: bool = False,
) -> OnboardingDecision:
    """Decide whether onboarding may proceed.

    Both consent values are raw checkbox states (default False in the UI —
    never pre-ticked). They are carried through to their independent consent
    records and NEVER influence ``can_create_account``.
    """
    if not terms_accepted:
        return OnboardingDecision(
            can_create_account=False,
            block_reason="main terms not accepted",
            data_release_opted_in=bool(data_release_checked),
            licensing_opted_in=bool(licensing_checked),
        )
    return OnboardingDecision(
        can_create_account=True,
        block_reason=None,
        data_release_opted_in=bool(data_release_checked),
        licensing_opted_in=bool(licensing_checked),
    )
