# 90-locale expansion status (Groundwork parity, minus Hmong)

Groundwork has 90 locale dirs and no Hmong dir, so the target set is all 90.
bk-latest now ships 91 locale dirs: all 90 Groundwork locales plus the
pre-existing `zh-CN` (kept as-is). `en` is the source baseline; `es` is the
untouched reference baseline. The 19 pre-existing finished locales were not
modified. This expansion added the other 70 locales (68 live translations
plus `zh`, Groundwork's Mandarin code, alongside pre-existing `zh-CN`).

Method per new locale, translated by hand (no outside services, no network,
no package installs): `frontend/src/locales/<lang>/translation.json` with
full 1629-key parity in exact `en` key order, intact `{{...}}` placeholders,
zero `⁇` (U+2047); the 104 flagged legal/financial/tax/privacy/safety keys
from `docs/TRANSLATION_PLAN.md` stay byte-identical English in live files,
with real translations living only in `docs/review/protected-<lang>.json`
(`_meta` + `strings: {key: {en, draft}}`, approximate-translation notice in
every `_meta`). Crisis/legal/money copy follows the Groundwork approach
(English in live files until a qualified reviewer approves the draft).

## Verification (all observed this session)

- `python3 tests/i18n-parity.py` — PASS, all 91 locales, 1629 keys each.
- `npx craco test` (CI) — 5 suites, 206/206 tests pass, incl. 181 locale
  checks (`locales.test.js` now covers all 91 locales: existence, key
  parity, placeholder preservation).
- Full per-locale audit (`/tmp/audit.py`: order, placeholders incl. drafts,
  zero U+2047, protected byte-identical, no empties, `_meta` shape) — 89/89
  translated locales ok. `en`/`es` have no protected drafts by design
  (source / untouched reference baseline).
- `npm run build` (with `REACT_APP_BACKEND_URL` set per the build guard) —
  production bundle compiles with all 91 locales wired.

## Wiring

- `frontend/src/lib/i18n.js`: static imports + `resources` + double-check).
- `frontend/src/lib/locales.test.js`: `EXPECTED` expanded 21 → 91.
- `tests/i18n-parity.py`: hardcoded 4-language list replaced with automatic
  discovery of every locale dir (key parity, orphans, empty values,
  placeholder check for all).
- No backend locale files exist; nothing to wire there.

## Per-locale results (70 new; eng-leftover = non-protected keys still equal to English)

All leftovers are documented keeps: brand names, emails, PO/Qty/ST codes,
IANA timezone, CSV/PDF tokens, code/file/env tokens, or
identical-spelling/standard-loanword keeps. No real untranslated strings
remain. Generation unknowns were driven to 0 for every locale.

| locale | eng-eq | character of extras |
|---|---|---|
| az | 47 | loanwords + tokens |
| bg | 24 | tokens/brands only |
| bho | 23 | tokens/brands only |
| bs | 52 | identical spellings + tokens |
| ca | 64 | identical spellings + tokens |
| ceb | 35 | loanwords + tokens |
| cs | 27 | tokens/brands only |
| da | 86 | identical spellings + tokens |
| de | 74 | identical spellings + tokens |
| el | 29 | tokens/brands only |
| et | 32 | tokens/brands only |
| ff | 24 | tokens/brands only |
| fi | 25 | tokens/brands only |
| gu | 23 | tokens/brands only |
| ha | 27 | loanwords + tokens |
| he | 22 | tokens/brands only |
| hr | 52 | identical spellings + tokens |
| hu | 29 | tokens/brands only |
| hy | 23 | tokens/brands only |
| id | 87 | business loanwords + tokens |
| ig | 27 | loanwords + tokens |
| ilo | 42 | loanwords + tokens |
| it | 40 | identical spellings + tokens |
| ja | 22 | tokens/brands only |
| jv | 86 | business loanwords + tokens |
| ka | 23 | tokens/brands only |
| kk | 24 | tokens/brands only |
| km | 24 | tokens/brands only |
| kn | 23 | tokens/brands only |
| ku | 45 | loanwords + tokens |
| ln | 26 | loanwords (Tableau/Facture) + tokens |
| lo | 24 | tokens/brands only |
| lt | 21 | tokens/brands only |
| lv | 22 | tokens/brands only |
| mg | 26 | loanwords + tokens |
| mk | 24 | tokens/brands only |
| ml | 23 | tokens/brands only |
| mr | 23 | tokens/brands only |
| ms | 70 | business loanwords + tokens |
| nl | 95 | identical spellings + tokens |
| no | 72 | identical spellings + tokens |
| om | 22 | tokens/brands only |
| pl | 54 | identical spellings + tokens |
| prs | 22 | tokens/brands only |
| ps | 22 | tokens/brands only |
| qu | 30 | loanwords + tokens |
| rn | 25 | loanwords + tokens |
| ro | 69 | identical spellings + tokens |
| ru | 28 | tokens/brands only |
| rw | 25 | loanwords + tokens |
| si | 23 | tokens/brands only |
| sk | 27 | tokens/brands only |
| sl | 26 | tokens/brands only |
| sm | 28 | loanwords + tokens |
| sq | 45 | loanwords + tokens |
| sr | 55 | identical spellings + tokens |
| sv | 72 | identical spellings + tokens |
| ta | 23 | tokens/brands only |
| th | 22 | tokens/brands only |
| ti | 23 | tokens/brands only |
| tr | 29 | tokens/brands only |
| tw | 31 | loanwords + tokens |
| uk | 28 | tokens/brands only |
| uz | 33 | loanwords + tokens |
| wo | 31 | loanwords + tokens |
| yi | 28 | loanwords + tokens |
| yo | 27 | loanwords + tokens |
| yue | 22 | tokens/brands only |
| zh | 20 | tokens/brands only |
| zu | 22 | tokens/brands only |

Pre-existing locales were re-audited green (am 22, ar 22, bn 21, fa 19,
fr 119, hi 26, ht 48, ko 18, my 17, ne 17, pa 21, pt 65, so 28, sw 28,
te 23, tl 113, ur 19, vi 30, zh-CN 18; `en` source, `es` reference).

## Constraints honored

- Never pushed; commit is local only. Nothing deleted (no `_parked/` moves
  needed). Existing finished locales untouched except registry/test files.
- No marketing copy targets Russia/Iran/Myanmar/Cuba; flags in the language
  picker are labels only.
- `.muse_write_probe` left untouched (still untracked).
- No uncommitted changes by others were present in the tree at commit time;
  nothing was reverted.
