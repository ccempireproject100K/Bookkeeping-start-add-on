# Data-Release Consent — Build Report (STAGED, 2026-11-11)

**State: built + wired + tested + staged. NOT deployed. No live tree edited. $0 spent.**
Live onboarding wiring is a later wave under the owner's word.

## What was built

| Piece | File(s) |
|---|---|
| Clauses — Scope A is the owner's exact words; Scope B is the separate licensing opt-in (both independently versioned, starting at `v1-2026-11-11`) | `consent/clause.py` |
| Onboarding gate — both consent checkboxes fully independent of main terms and each other | `consent/gate.py` |
| Per-scope consent records + append-only change log (SQLite, stdlib; who/when/version/state/surface/scope/reason), account-deletion propagation, and the yearly licensing re-confirmation cycle | `consent/store.py` |
| Cohort opt-in / opt-out rates by scope, counting only current-version opt-ins and excluding account-deleted tenants (signup month × trade × plan; version-specific filtering available) | `consent/cohorts.py` |
| Re-identification cell check (executable half of the review) | `consent/suppression.py` |
| Risk review document — the licensing gate | `docs/REIDENTIFICATION-RISK-REVIEW.md` |
| Onboarding proof page (two separate consent cards, both unchecked by default, plus the “What this means” panel with give-back and account-deletion lines) | `ui/onboarding_step.html` |
| Settings → Privacy contributor receipt (per-scope states, leave controls, annual re-confirmation pending state, per-release benchmark-report entitlement, seeded demo release registry) | `ui/settings_privacy.html` |
| Cohort report (rendered from seeded demo records, rates computed per scope, not hard-coded) | `ui/cohort_report.html` via `scripts/seed_and_render.py` |
| The nudge (one-time per-scope pop-up session logic, copy, conversion measurement) | `consent/nudge.py` + pop-up in `ui/onboarding_step.html` |
| Coverage statement (trade × metro counts/rates, floor-suppressed, method on its face) | `consent/coverage.py` + `ui/coverage_report.html` via `scripts/render_coverage.py` |
| Tests | `tests/test_consent.py` + `tests/test_improvements.py` + `tests/test_nudge_coverage.py` |
| Screenshots | `screenshots/` via `scripts/shoot.py` |

Spec source: the real AI Office flow — `frontend/src/pages/Signup.jsx` (account creation, terms surface) and tenant fields in `backend/models.py`. Cohort dimensions are the flow's own fields: signup month, `industry_slug` (trade, applied at the signup trade step), `plan_id`.

## Behavior, as specified

- Each consent is its **own checkbox in its own card**, separate from the main terms line and from the other consent, **unchecked by default**, never pre-ticked, never bundled.
- **Declining either changes nothing**: account creation is gated on the main terms ONLY (proven in the interaction screenshot: terms accepted + both optional boxes unchecked → account created, both records = DECLINED — and checking either optional box while terms are unchecked keeps the button off).
- Every presentation writes a per-scope consent record (decision, clause version, timestamp, surface, scope). Any later change of mind appends to an append-only log and updates only that scope; an opt-out is honored going forward (the tenant drops out of that scope's aggregation eligibility immediately).
- Cohort report computes opt-in rate (currently in under the version in force ÷ presented) and opt-out rate (ever withdrew ÷ ever opted in) per cohort and scope from records only; a version-specific filter is available. Demo render: 101 presented per scope; Scope A 52.5% opt-in / 10.2% opt-out, Scope B 45.5% opt-in / 9.8% opt-out (invented tenants, labeled as demo on the page).
- **Give-back first:** a contributor's receipt records, beside each release, its entitlement to the benchmark report for its own trade × metro from that release. Report delivery is a later wiring step; no report is produced in this build.
- **Account deletion:** both scopes become `account_deleted`, each log entry carries reason `account deleted`, and future cohort rates are recomputed without the tenant. The plain limit is stated on both surfaces: a contribution already baked into a past release cannot be removed from that past release, but nothing new includes the business.
- **Yearly re-confirmation:** licensing only. An injectable-clock check moves an unanswered, anniversary-passed licensing consent to `reconfirmation_required` (ineligible) and logs the lapse; Scope A remains exactly as the tenant left it. A recorded answer starts a new annual cycle.

## Improvement pass — six owner-approved improvements + give-back entitlement

1. **Licensing is now Scope B, its own opt-in.** Scope A retains the owner's sentence **byte-identical** and remains the marketing/product-improvement consent record. Licensing eligibility now requires Scope B specifically. Scope B's draft line is: “I agree that Mind At Ease may license aggregated, anonymized insights based on my business data to third parties, with all identifying details removed.” **ROUTER-DRAFTED WORDING — awaiting the owner's stamp; it is not represented as owner-approved.**
2. **“What this means” panel.** The onboarding surface has a one-tap expandable panel with concrete aggregate examples (“average ticket size for roofers in a metro”; “share of shops offering financing”) and a short never-leaves list: business names, customer lists, exact revenue, and anything pointing at one business.
3. **No silent carry-over.** Clause versions are tracked independently per scope. Publishing a new version moves a prior opt-in for that scope only to `reconsent_required`, removes current-version eligibility, keeps the tenant visible in the cohort as `reconsent_required` with zero contribution to the opt-in rate, and appends the transition to the log. A fresh opt-in records the new version.
4. **Contributor receipt.** Settings → Privacy now shows both consent states, a leave control for each scope, and a release registry for releases the tenant's cohort contributed to. The staged registry contains two fixtures and is labeled **“DEMO / SEED — not a real release”** in the data model and on screen. No real releases exist in this build, and leaving a scope appends the opt-out and removes future eligibility for that scope only.

### Owner addition — give-back first (decision, 2026-11-11)

The standing contributor model is **give-back first — no cash percentage**. A business whose data contributed to a release is entitled to the benchmark report for its own trade × metro from that release. Each contributor-receipt release entry now shows **“Your benchmark report”** with the entitlement detail and the status **“Entitlement recorded — report delivery wired later.”** The “What this means” panel carries the plain line: “If your business contributes to a release, you get the benchmark picture for your trade and metro.” No report is fabricated in this staged build, and no percentage or payout mechanics were added. The model is revisitable on the owner's word.

5. **Account-deletion propagation.** `delete_account()` moves both scopes to `account_deleted`, appends one log event per scope with reason **`account deleted`**, and makes the tenant ineligible for all future releases automatically. Cohort reporting excludes account-deleted tenants, so rates going forward are recomputed without them. The contributor receipt and “What this means” copy state the honest limit in plain words: “If you delete your account, you are out of every future release. A contribution already baked into a past release cannot be removed from that past release.” Nothing new includes the business after deletion; past-release history is not rewritten.

6. **Yearly re-confirmation.** The licensing scope has an annual cycle driven by an injectable clock (`apply_annual_reconfirmation_check(..., now=...)`), so tests cross anniversaries without waiting. When the anniversary passes unanswered, licensing becomes `reconfirmation_required`, is ineligible, and the log records `annual_reconfirmation_lapsed` with reason “annual re-confirmation unanswered.” Marketing/product improvement stays exactly as the tenant left it. `record_annual_reconfirmation()` records the answer: a yes continues/restores licensing under the current version and starts a new cycle; a no withdraws licensing. Settings → Privacy surfaces the pending state in an “Annual re-confirmation — pending state” panel.

Propagation note: the split-consent change should propagate to the home-inventory app at its wiring wave; no edits were made to that tree in this pass.

## Owner additions — the nudge + the coverage statement (2026-11-11 evening)

Two additions ordered by the owner later the same evening, built in the same staged tree. Still NOT deployed, $0 spent, no live tree edited, no dataset produced.

### 1. The nudge (consent/nudge.py + the onboarding pop-up)

Owner's words: "before they leave the page, kind of like click it and then pop it up. To let them know about the benchmark report and how important it is for them." When the user clicks Continue with a scope UNCHECKED, a pop-up for that scope appears before they leave the page. It explains in plain words what they would get — the benchmark report for their trade and metro, how their numbers stack up against shops like theirs — and carries the owner's line in the pop-up body, visibly above the buttons (his wording, 2026-11-11 ~21:00 CST): **"It's 100% your choice and we respect your decision either way."**

Hard rules, all test-pinned: **(a)** at most once per scope per signup session — dismissing means it never pops again in that flow (a fresh session may show it again); **(b)** it never checks a box by itself — only the explicit **"Yes, count me in"** tap checks it, and that tap is the user's own click; showing the pop-up changes no checkbox and creates no consent record; **(c)** it never blocks account creation — after a dismissal the flow proceeds exactly as before, terms alone still gate the account; **(d)** it respects the split — a checked scope never nudges, both checked means no pop-up and no log noise; if one scope is checked, the pop-up covers only the unchecked one; **(e)** every show / accept / dismiss is appended to the log (`nudge_shown` / `nudge_accepted` / `nudge_dismissed`, with the session as the reason), and `nudge_conversion()` reports distinct-tenant shown → accepted conversion per scope for cohort reporting; **(f)** the copy is UI copy, not clause language — short, honest, no countdowns, no pressure language, and **"No thanks, continue"** shares the accept button's class, size, and weight (equal prominence, asserted in tests).

Screenshot (viewed firsthand): `screenshots/nudge-scope-b.png` — terms accepted, Scope A checked, Scope B unchecked, Continue clicked: the pop-up carries the licensing copy, the owner's respect line, and the two equal buttons.

### 2. Coverage statement (consent/coverage.py + ui/coverage_report.html via scripts/render_coverage.py)

Owner's words: "we need to be able to tell people what percentage of our contractors we actually got data from." A presentable coverage report per scope: one row per **trade × metro** cell with presented count, opted-in count (current opt-in under the version in force, as in cohorts), and opt-in rate %, plus rollups by trade, by metro, and overall. The report states its consent method on its face: **"active opt-in, unchecked by default."**

To support the metro dimension, tenant records gained an optional `metro` field (default "Fort Worth"); existing records, signatures, and tests are unaffected.

Privacy, test-pinned: the payload contains **counts and rates only** — no tenant names, no tenant ids, no per-business rows (asserted by scanning the serialized payload for every seeded tenant id and business name). A cell below the existing reporting floor (`MIN_CELL_BUSINESSES = 10` in `consent/suppression.py`, numbers unchanged) shows **"below reporting floor"** for presented, opted-in, and rate alike. Rollups aggregate **reportable cells only** and the payload says so (`rollup_basis`, plus the count of excluded cells): summing a below-floor cell into a published total would let a reader difference it back out — the differencing leak the risk review exists to stop.

Demo render (seeded in scripts/render_coverage.py, the roofing × Smalltown cell deliberately small): Scope A overall **32 of 57 reportable contractors opted in — 56.1%**; Scope B **26 of 57 — 45.6%**; the Smalltown cell renders as "below reporting floor" in every column. Screenshot (viewed firsthand): `screenshots/coverage-report.png`.

### Tests — 45 passed / 0 failed

`python3 -m pytest tests/ -q --capture=sys` → **45 passed**: the prior **31** untouched and green, plus **14** in `tests/test_nudge_coverage.py` pinning nudge rules (a)–(f), the owner's line verbatim in copy and page, nudge conversion math, coverage math on a seeded cohort, floor suppression (including that the small cell's exact counts appear nowhere in the payload), and the no-tenant-identifiers scan.

Proof screenshots (viewed firsthand): `screenshots/onboarding-split-consents-panel-open.png` shows both unchecked consent cards with the explainer, give-back line, and account-deletion limit open; `screenshots/contributor-receipt.png` shows both current consent states, per-scope leave controls, the annual re-confirmation pending state, per-release benchmark-report entitlements, and the labeled demo registry.

## Tests — 31 passed / 0 failed

`python3 -m pytest tests/ -q --capture=sys` → **31 passed**: the original **17** (clause verbatim, checkbox independence, default-unchecked, consent records, opt-out/opt-back-in logging, cohort math, and all suppression/gate cases) plus **14** improvement tests covering split-clause independence, all gate combinations, per-scope records/cohorts/eligibility, scoped changes, version publication without carry-over, decline-after-version-change, scope-correct demo-registry attachment, the labeled demo contributor receipt with give-back entitlement and scoped leave, account deletion across both scopes with cohort recomputation, both yearly re-confirmation branches (answered and unanswered) on an injectable clock, and both UI surfaces. The verbatim test still applies to Scope A's unchanged sentence; no original test was altered.

## The risk review (the gated deliverable)

`docs/REIDENTIFICATION-RISK-REVIEW.md` — names the failure mode plainly (in a small market, "anonymized" aggregates identify the only roofing shop in town), then sets the method every future dataset must pass: scope-specific consent-eligibility pull (excluding superseded, annual-lapsed, opted-out, and account-deleted tenants), contributor give-back recording, cell construction (trade × metro × band, metro-or-coarser geography only), cell-size floor, dominance cap, small-market flagging with manual review, quasi-identifier combination sweep, month-over-month differencing check, free-text/outlier ban, and a named-human sign-off checklist that ends with the owner's word for that specific release. The standing give-back-first contributor model (owner decision, 2026-11-11) is recorded in §7 of the review.

**ALL thresholds are PROPOSED and await the owner's stamp:** cell floor ≥ 10 businesses · dominance cap 50% · small market = trade×metro under 25 businesses · small metro = population under 50,000. Justifications are in the review; the numbers live in `consent/suppression.py` constants so a stamp changes them in exactly one place.

**Standing gate: no dataset is produced, shared, or licensed to any third party without (a) passing the review end-to-end, (b) a completed checklist with a named human on it, and (c) the owner's word for that release. No dataset was produced in this task.**

## Wording status

The owner's Scope A sentence was implemented exactly as given and remains byte-identical. Licensing is additionally broken out as Scope B under the owner's improvement-pass approval. Scope B's sentence is **router-drafted and awaits the owner's stamp** before it can be treated as settled wording.
