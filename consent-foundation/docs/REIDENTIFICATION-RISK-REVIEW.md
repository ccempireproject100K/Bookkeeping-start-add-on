# Re-identification Risk Review — Aggregated Business Data Releases

**Status:** STAGED — method document, built 2026-11-11. **No dataset has been produced, shared, or licensed under this review. None may be, until a release passes every step below, carries a named human's sign-off, and has the owner's word.**

**Scope:** any dataset Mind At Ease might build from tenant business data under the independently versioned data-release consents. Scope A covers marketing and product improvement; Scope B is the separate third-party licensing opt-in. Both scopes start at clause version `v1-2026-11-11`, and future versions do not carry prior opt-ins forward.

---

## 1. The failure mode, named plainly

Removing names does not anonymize a small market.

If a dataset says *"roofing companies in a town of 8,000 people averaged $310k revenue last year,"* and that town has exactly **one** roofing company, the dataset has published that company's revenue. The name was removed; the identity was not. Anyone who knows the town — a competitor, a supplier, a customer — can point at the number and name the business.

The same leak happens in combinations: trade × small metro × a revenue band × years-in-business can narrow to one shop even when each field looks harmless alone. And it happens across time: if a monthly release changes by exactly one new shop's numbers, the difference between the two releases is that shop.

**Every rule below exists to stop that one thing: an "anonymized" aggregate that points back to a single business.**

## 2. Who may be in a dataset at all

- Only tenants whose **current** consent state is `opted_in` for the dataset's scope and that scope's clause version in force. Marketing/product-improvement use requires Scope A specifically; any licensed third-party release requires Scope B specifically. A consent record is required — no record, no contribution.
- A superseded opt-in is `reconsent_required`, not consent for the new version, and is excluded until the tenant opts in to the version in force.
- A licensing consent that passes its annual anniversary unanswered is `reconfirmation_required`, not a current opt-in, and is excluded until the tenant re-confirms. The annual cycle does not change the marketing/product-improvement scope.
- A tenant who opted out of the relevant scope is excluded from every dataset built **after** the opt-out. (Datasets already released are not recalled; that limit is stated to the tenant in Settings → Privacy, in plain words.)
- An account-deleted tenant is `account_deleted` in both scopes and is excluded from **every future release**. The append-only log carries reason `account deleted`. A contribution already baked into a past release cannot be removed from that past release; nothing new includes the business.
- Consent is not a license. An opted-in tenant's data may only leave in a dataset that passes this review **and** has the owner's word for that release.

## 3. The method — every dataset, every time, in order

### Step 1 — Cell construction
The dataset is built as cells: one **trade × geography × attribute band** combination per cell (e.g. HVAC × Fort Worth metro × revenue band $100k–$250k). No row-level records ever leave. Geography is metro-level or coarser — never ZIP, never address, never a named small town.

### Step 2 — Cell-size floor (k-anonymity)
A cell may publish only if at least **10 distinct businesses** contribute to it.
*Justification:* below 10, a cell behaves like a roster — a reader who knows a few members can back out the rest. 10 is a conservative floor in line with statistical-disclosure-control practice for commercial releases (public agencies suppress far smaller cells; we are releasing commercially, to parties with motive to re-identify, so the floor is set higher, not lower). Cells under the floor are **suppressed** — removed entirely, never merged silently into a neighbor cell without re-running the check on the merged cell.

### Step 3 — Dominance check
Even a cell of 10+ businesses fails if **one business contributes more than 50%** of the cell's published value. A cell where one shop is 80% of the revenue is that shop's revenue wearing camouflage. Dominant cells are suppressed or the value is not published (counts may publish if they pass on their own).

### Step 4 — Small-market flagging
A trade × metro market is a **small market** when the total number of businesses in that trade in that metro — counted across the whole market, not just Mind At Ease tenants — is **under 25**, or the metro population is **under 50,000**. Every cell from a small market is flagged and held for manual review even if it clears Steps 2–3, because in a small market the audience likely knows every shop by name. Default outcome of manual review is suppression unless the reviewer can state, in writing, why no single business can be pointed to.

### Step 5 — Quasi-identifier sweep
The fields an outsider who knows a target business would match on are: **trade, metro, revenue band, employee-count band, years-in-business band, plan tier**. For every published cell, the reviewer checks the *combination*, not each field alone: if the combination of bands narrows the cell below the Step 2 floor for any sub-group, the cell is re-banded coarser (wider bands, larger geography) and re-checked, or suppressed.

### Step 6 — Differencing check
Before a repeat release (e.g. monthly), the reviewer diffs it against the previous release. If any cell's change is attributable to a single business entering, leaving, or changing bands, that cell is held: publish it only when the change is no longer single-business-attributable, or suppress it this cycle.

### Step 7 — Free-text and outlier ban
No free-text fields, no "notable example" callouts, no top/bottom rankings of named or nameable markets, and no min/max values (a maximum in a small cell is usually the biggest shop in town — publish medians and banded distributions instead).

## 4. Threshold summary — ALL PROPOSED, AWAITING THE OWNER'S STAMP

| Rule | Proposed threshold | Where enforced |
|---|---|---|
| Minimum cell size | ≥ 10 contributing businesses | `consent/suppression.py` `MIN_CELL_BUSINESSES` |
| Dominance cap | one business ≤ 50% of a cell's value | `DOMINANCE_MAX_SHARE` |
| Small market (trade × metro) | < 25 businesses in the trade/metro = flagged, manual review | `SMALL_MARKET_TRADE_COUNT` |
| Small metro | population < 50,000 = flagged, manual review | `SMALL_METRO_POPULATION` |
| Quasi-identifiers | trade, metro, revenue band, employee band, years band, plan — combinations must clear the cell floor | Step 5 |
| Geography floor | metro or coarser; never ZIP/town for small markets | Step 1 |

These numbers are recommendations from the method, not settled policy. They take effect for real releases only when the owner stamps them.

## 5. Sign-off checklist — completed by a named human before any dataset leaves

No dataset is produced for a third party, shared, or licensed unless every line is checked and signed. A copy of the completed checklist is kept with the release record.

- [ ] Dataset described in one paragraph: source data, date range, intended recipient, stated purpose.
- [ ] Consent pull verified for the release's scope: every contributing tenant's current state is `opted_in` under the current version for that scope (Scope B for any licensed third-party release); `reconsent_required`, annual `reconfirmation_required`, account-deleted tenants, and opt-outs after the last release are excluded; the pull is logged.
- [ ] Contributor give-back recorded: each contributing tenant's receipt shows its benchmark-report entitlement for its own trade × metro from this release (delivery is a separate wiring step).
- [ ] Cell check run (`evaluate_dataset`): every cell passes size + dominance; flagged cells listed, each with its disposition (suppressed / re-banded / held).
- [ ] Small-market review done: every small-market cell has a written no-single-business-pointing note, or is suppressed.
- [ ] Quasi-identifier sweep done on the published combinations.
- [ ] Differencing check done against the previous release (or "first release" noted).
- [ ] No free text, no rankings, no min/max values anywhere in the file.
- [ ] Thresholds applied are the owner-stamped versions (or this release is explicitly internal-only).
- [ ] Reviewer name: ______________________  Date: ____________
- [ ] **Owner's word for this release obtained:** ☐ yes — date/channel: ____________

**If any line cannot be checked, the dataset does not leave. There is no partial pass.**

## 6. Standing gate

Consent collection (the onboarding checkbox) and this review are separate things. The checkbox being live someday does **not** authorize any release. **No third-party licensing deal, data share, or "insights" export happens without (a) a dataset that passes this review end-to-end, (b) a completed sign-off checklist with a named human on it, and (c) the owner's word for that specific release.**

## 7. Standing contributor model — give-back first (owner decision, 2026-11-11)

Contributors are not paid a cash percentage. The standing model is **give-back first**: a business whose data contributed to a release is entitled to the benchmark report for its own trade × metro from that release. The entitlement is recorded on the contributor receipt beside the release entry. Producing and delivering the report is a later wiring step; no report is fabricated by the staged consent build. This model is revisitable only on the owner's word.
