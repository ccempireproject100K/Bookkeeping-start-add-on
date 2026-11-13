"""The data-release consent clauses, independently versioned by scope.

Scope A is the owner's exact sentence. DO NOT edit CLAUSE_TEXT without a
new CLAUSE_VERSION and the owner's word: his exact-wording discipline
applies, and every consent record stores the version the tenant actually
saw.

Scope B separates third-party licensing into its own opt-in. Its wording is
ROUTER-DRAFTED and awaits the owner's stamp; it must not be represented as
owner-approved wording.
"""

# -- Scope A: marketing + product improvement ------------------------------
CLAUSE_VERSION = "v1-2026-11-11"

CLAUSE_TEXT = (
    "I agree that Mind At Ease may use my business data in aggregated, "
    "anonymized form for marketing, product improvement, and licensed "
    "insights to third parties, with all identifying details removed."
)

SCOPE_MARKETING_IMPROVEMENT = "marketing_improvement"
SCOPE_LICENSING = "licensing"
CONSENT_SCOPES = (SCOPE_MARKETING_IMPROVEMENT, SCOPE_LICENSING)

# -- Scope B: third-party licensing (ROUTER-DRAFTED) -----------------------
LICENSING_CLAUSE_VERSION = "v1-2026-11-11"
LICENSING_CLAUSE_TEXT = (
    "I agree that Mind At Ease may license aggregated, anonymized insights "
    "based on my business data to third parties, with all identifying "
    "details removed."
)
LICENSING_CLAUSE_IS_ROUTER_DRAFT = True

# Current published version for each independently versioned scope.
CLAUSE_VERSIONS = {
    SCOPE_MARKETING_IMPROVEMENT: CLAUSE_VERSION,
    SCOPE_LICENSING: LICENSING_CLAUSE_VERSION,
}

CLAUSE_TEXTS = {
    SCOPE_MARKETING_IMPROVEMENT: CLAUSE_TEXT,
    SCOPE_LICENSING: LICENSING_CLAUSE_TEXT,
}

# Short explanatory UI copy for the "What this means" panel. This is not
# clause language and does not expand or narrow either consent.
WHAT_THIS_MEANS_EXAMPLES = (
    "Average ticket size for roofers in a metro.",
    "Share of shops offering financing.",
)
WHAT_NEVER_LEAVES = (
    "Business names.",
    "Customer lists.",
    "Exact revenue.",
    "Anything pointing at one business.",
)
WHAT_THIS_MEANS_GIVE_BACK = (
    "If your business contributes to a release, you get the benchmark "
    "picture for your trade and metro."
)
WHAT_THIS_MEANS_ACCOUNT_DELETION = (
    "If you delete your account, you are out of every future release. "
    "A contribution already baked into a past release cannot be removed "
    "from that past release."
)

# Where a tenant can be shown either clause.
SURFACE_ONBOARDING = "onboarding"
SURFACE_SETTINGS = "settings"
