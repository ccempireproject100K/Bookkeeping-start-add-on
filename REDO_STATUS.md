# Bookkeeping locale REDO status

Fresh from-scratch redo of all 19 non-baseline locales (everything except
`en`). `es` is the untouched Spanish baseline. No new locales added, none
removed; nothing deleted (no `_parked/` moves were needed).

Method per locale: translated fresh from the English source (+ Spanish
reference for tone and keep-signals), never reusing old locale files. The
104 flagged legal/financial/tax/privacy/safety keys from
`docs/TRANSLATION_PLAN.md` stay byte-identical English in live files;
review drafts live in `docs/review/protected-<lang>.json` in Groundwork's
protected-draft format (`_meta` + `strings: {key: {en, draft}}`, with the
approximate-translation notice in every `_meta` for every language other
than en/es). Placeholders (`{{name}}`, `{{role}}`), brand names, URLs,
emails, model strings, and code/file tokens kept byte-exact. No `⁇`
(U+2047) anywhere. No external translation APIs, web services, or package
installs were used (network use was declined); all strings are
self-authored translations.

## English-leftover counts (strings still equal to English, 1629 keys each)

Every locale passes: 1629-key parity with `en` in the same key order, no
`⁇`, all `{{...}}` placeholders preserved per key, 104/104 protected keys
byte-identical to English in live files. Counts below = protected 104 +
documented extras (all extras are brand names, emails, proper nouns,
US-locale examples, file/env/code tokens, IANA timezone, or
identical-spelling/standard-loanword keeps — no real untranslated strings
remain):

| locale | eng-eq total | protected | extra | extra character |
|---|---|---|---|---|
| am | 126 | 104 | 22 | tokens/brands only |
| ar | 126 | 104 | 22 | tokens/brands only |
| bn | 125 | 104 | 21 | tokens/brands/US-example |
| fa | 123 | 104 | 19 | tokens/brands/US-example |
| fr | 223 | 104 | 119 | identical FR spellings + tokens |
| hi | 130 | 104 | 26 | tokens/brands/US-example |
| ht | 152 | 104 | 48 | loanwords (Bank/Memo/Total) + tokens |
| ko | 122 | 104 | 18 | tokens/brands only |
| my | 121 | 104 | 17 | tokens only |
| ne | 121 | 104 | 17 | tokens only |
| pa | 125 | 104 | 21 | tokens/brands/US-example |
| pt | 169 | 104 | 65 | identical PT spellings + tokens |
| so | 132 | 104 | 28 | tokens/brands/examples |
| sw | 132 | 104 | 28 | tokens/brands/examples |
| te | 127 | 104 | 23 | tokens/brands/timezone |
| tl | 217 | 104 | 113 | Filipino business loanwords + tokens |
| ur | 123 | 104 | 19 | tokens/brands/US-example |
| vi | 134 | 104 | 30 | Email/AI/PO loanwords + tokens |
| zh-CN | 122 | 104 | 18 | tokens/brands only |

## Full leftover lists (non-protected keys still equal to English)

Shared token keeps recurring across locales: `brandingcard.acmeAccounting`
(brand), `brandingcard.helpAcmeaccountingCom` / `teaminvites.cpaFirmCom` /
`login.youShopCom` (emails), `aiplan.poS` (PO(s) code), `salestaxtab.st`
(ST code), `internationalfields.americaChicago` (IANA timezone),
`aging.csv` / `budgetvsactual.csv` / `businessreports.csv` / `cashflow.csv`
(CSV), `receipts.pdf` (PDF), `purchaseorders.po` / `purchaseorders.qty`
(PO/Qty codes), `banking.plaidCLIENTID` / `plaidSECRET` /
`tokenENCRYPTIONKEY` / `plaidSETUPMd` (env/file codes),
`banking.dateDescriptionAmount` (CSV header), `combo.empireCombo` /
`nav.empireCombo` (trade-face proper name), `copilot.ai` (AI),
`inviteaccept.jordanLee` / `login.mindAtEaseSolutionsHVAC` /
`login.mindAtEaseHVAC` (proper names), `teaminvites.cpaFirmCom`,
`taxforecast.ytd` (YTD), `salestaxtab.eGTravisCountyTX` /
`banking.eGHomeDepot` / `playground.eG4500` / `playground.eG30000`
(US-locale examples).

Per-locale extras beyond the shared set:

- am, ar (22): shared token set only.
- ko, zh-CN (18): shared token set only.
- my, ne (17): shared token set minus brand/timezone (brand and timezone
  translated).
- ur, fa (19): shared token set; fa also keeps `aiplan.aiGenerated`
  (AI badge).
- bn, pa (21): shared set + `login.mindAtEaseSolutionsHVAC`.
- te (23): shared set + `banking.dateDescriptionAmount`.
- hi (26): shared set + `businessreports.mo` (mo code).
- vi (30): shared set + `combo.email`, `customers.email`,
  `expenses.email`, `login.email`, `portal.email` (Email loanword).
- ht (48): shared set (incl. `salestaxtab.total`, `budget.total`,
  `estimates.total`, `invoices.total`, `portalview.total`,
  `purchaseorders.total`, `quicksell.total`, `receipts.total` = Total,
  identical spelling) + `brandingcard.logoURL` (Logo URL),
  `aging.totalAR/totalAP` (AR/AP), `banking/bills/expenses/invoices/
  receipts.bank` (Bank loanword), `businessreports.mo`,
  `dashboard/expenses/generalledger/invoices/journalentries.memo`
  (Memo loanword).
- fr (119): shared token set + identical French spellings, e.g. Date,
  Client(s), Total, Description(s), Action(s), Note(s), Type, Code,
  Message, Source, Commission, Contact, Services, Attention, Net,
  Status, Budget(s), Transaction(s), Memo, Item, Lead, Via, Radio,
  Volume, Miles, Manual, Combos/Jobs/Commissions/Portal nav labels,
  `copilot.aiCopilot` (AI Copilot brand), `taxforecast.note` (Note),
  `cashclose.notes`, `giftcards.initial`, `subcompliance.attention`,
  `combo.note(s)`, `combo.source`, `portal.*`, `inventory.type`,
  `entities.net`.
- pt (65): shared token set + identical Portuguese spellings, e.g.
  Total, Status, Subtotal, Memo, Manual, Item, Jobs/Job, Lead, Via,
  Volume, `combo.email`, `combo.lead`, `copilot.aiCopilot`,
  `dashboard.memo`, `expenses.memo`, `generalledger.memo`,
  `invoices.memo`, `journalentries.memo`, `purchaseorders.status`,
  `payroll.manual`, `payroll.status`, `portalview.subtotal`,
  `portal.status`, `jobprofitability.jobs`, `nav.jobs`.
- tl (113): shared token set + standard Filipino business loanwords
  kept per local usage, e.g. Vendor, Email, Cash, Card, Dashboard,
  Template, Password, Memo, Account, Debit, Credit, Ledger, Balance
  Sheet, Trial Balance, General Ledger, Chart of Accounts (`Chart of
  Accounts` kept verbatim as the standard Filipino accounting term),
  Payroll, Inventory, Margin, Rate, Invoice, Customer, Contact,
  Merchant, Workspace, Gross, Subtotal, Lead, Bookkeeper,
  `totpsettings.advancedOtpauthURI` (Advanced: + otpauth URI code),
  `workspaceswitcher.workspace`, `teamInvites.bookkeeper`,
  `nav.aICopilot`, `nav.auditLog`, `navGroup.ledger`.

## Verification (this session, independent of the translation passes)

- Key parity: 21/21 locales (incl. `te`) 1629/1629 keys, same order as
  `en` (independent `python3` audit over the merged tree).
- No U+2047 in any of the 38 files (19 live + 19 drafts).
- `{{...}}` placeholder sets match `en` per key in all live files and
  all drafts (`{{name}}` in protected `customers.deleteConfirm` drafts,
  `{{role}}` in live `layout.viewingAs` in all 19 locales).
- Protected 104/104 byte-identical to English in every live file;
  every draft holds exactly the 104 flagged keys with `en` + native
  `draft` and the `_meta` notice.
- Script check: every non-protected value in non-Latin-script locales
  carries target-script characters (Amharic, Arabic, Bengali,
  Devanagari, Gurmukhi, Hangul, Myanmar, Telugu, Han); zero empty or
  blank values in any locale.
- `python3 tests/i18n-parity.py`: PASS (en/es/fr/zh-CN full parity).
- `frontend` locales test (`craco test src/lib/locales.test.js`,
  updated to expect 21 locales incl. `te`): **41 passed**.
- Backend `pytest`: NOT runnable here — collection fails on
  `ModuleNotFoundError: No module named 'fastapi'` (no backend venv in
  this checkout, no network to install; pre-existing environmental
  limitation; no backend files were touched by this redo).
- `npm run build` (with the documented `REACT_APP_BACKEND_URL` from
  `render.yaml`): **passes**, bundle compiles with all 21 locales.

## Fixes applied during merge review (parent, post-workflow)

- `my`/`ne` drafts were structurally flat (`entries` at top level);
  restructured to Groundwork format (`_meta` + `strings`), `_meta`
  wording aligned to the required notice text.
- The ~80 keys per locale that mirrored `es`'s English keeps (fragments
  like `customers.save`, `backup.*`, `payroll.*`, `pricecompare.*`)
  were translated natively for bn/pa/hi/ht/vi/fr/pt/tl (formal
  register matching `es`); pure codes/brands/proper nouns kept.
- `fr`: pass also normalized informal `tu` forms to formal `vous`
  (matching `es` usted), and fixed two semantically inverted payroll
  fragments and several keys the first pass had left English
  (`Advanced: otpauth URI`, `rule→`, `(template — add AI_API_KEY …)`,
  `Reportable (≥$600)`, `Hrs`).
- `pt`: same class — translated `Answers from your live numbers…`,
  fixed Spanglish `cada job` → `cada serviço`, `h` → `Horas`, wrong
  gender `efetiva` → `efetivo`, English keeps (`rule→`, template key).
- `tl`: restored two regressions my repair had introduced
  (`Pahinasyon`, `Paunang silip —`).
- Empty/blank fragment values the first passes produced are now filled
  in `my`/`ne` (13 each: `as`, `to`, `by`, `for`, `of`, `when`,
  `As of`, `out.`), `hi`/`ht` (`purchaseorders.of`), `so`
  (`copilot.by`, `invoices.by`); also repaired a dropped word in
  `my`/`ne` `budget.in` (`', −'` → native `in` + `, −`).

## For Brann to review

- fr/pt/tl/ht large-but-legitimate identical-spelling/loanword lists
  above (esp. fr 119, tl 113) — spot-check preference for a few
  (`giftcards.initial`, `mileage.miles`, tl `Password`/`Dashboard`
  keeps follow Groundwork's Italian-`Password` loanword precedent).
- Machine-translation caveats from the passes: Amharic follows English
  word order; Arabic uses masculine-default MSA UI forms; Burmese/Nepali
  concatenated fragments keep English assembly order; Burmese/Nepali
  `budget.out` axis reads inflow/outside (`အထွက်.`/`बाहिर.`).
- `docs/review/protected-<lang>.json` drafts (19 files) need qualified
  reviewer (counsel/CPA where relevant) approval before any string is
  copied into live files — per the `_meta.status` in each file.

## Brann's pre-existing changes: disposition in this commit

- Staged `frontend/src/lib/i18n.js` (te registration): kept as-is,
  committed.
- Staged `frontend/src/locales/te/translation.json`: superseded by the
  fresh Telugu redo (parity kept, `⁇` markers eliminated) and committed.
- Staged `docs/review/protected-te.json`: superseded by the fresh
  Telugu draft (same format, `⁇` eliminated) and committed.
- Unstaged `frontend/src/locales/ar/translation.json` (whole file
  reverted to English in the working tree): superseded by the fresh
  Arabic redo and committed — the working-tree English content did NOT
  enter the commit.
- Untracked `frontend/package-lock.json`: left untracked, NOT committed
  (not part of this translation redo).
- Untracked empty `.muse_write_probe`: left untouched on disk, NOT
  committed.

## Marketing-locale and scope notes

- No locales added, none removed. No marketing targeting was added for
  any region. Existing Persian (`fa`) and Burmese (`my`) translations
  were redone in place as the task allows; there is no Russian locale
  in this repo. Telugu (`te`) ships because Brann had already staged it
  (registered in `i18n.js`, covered by the parity test); it was redone
  to the same standard rather than reverted.
- `en` and `es` live files are byte-identical to before (baselines
  untouched). No source, component, backend, or config files changed
  except the one-line test EXPECTED update that ships `te` coverage.
- Nothing was deleted, so nothing was parked in `_parked/`.
- Local commit only; never pushed.
