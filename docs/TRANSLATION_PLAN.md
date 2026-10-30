# Translation Plan — 20 Languages

**Branch:** `i18n-21-languages` (local only — never pushed, merged, or deployed by the builder)
**Builder:** Grok Bot translation pass (machine translation, token-protected), pending human review.
**Hosting:** unchanged (stays on Emergent). No Plaid work, no price changes in this branch.

## Target: 20 languages

> **Russian (ru) removed per Brann 2026-10-30 (US sanctions). Target is now 20 languages** (originally 21).


| Code | Language | Status |
|---|---|---|
| en | English (source) | Done before |
| es | Spanish | Done before |
| fr | French | Done before |
| zh-CN | Chinese (Simplified) | Done before |
| ht | Haitian Creole | Added in this branch |
| ar | Arabic (RTL) | Added in this branch |
| vi | Vietnamese | Added in this branch |
| tl | Tagalog | Added in this branch |
| hi | Hindi | Added in this branch |
| ko | Korean | Added in this branch |
| pt | Portuguese | Added in this branch |
| so | Somali | Added in this branch |
| ur | Urdu (RTL) | Added in this branch |
| bn | Bengali | Added in this branch |
| pa | Punjabi | Added in this branch |
| fa | Farsi / Persian (RTL) | Added in this branch |
| am | Amharic | Added in this branch |
| my | Burmese | Added in this branch |
| ne | Nepali | Added in this branch |
| sw | Swahili | Added in this branch |

Files: `frontend/src/locales/<code>/translation.json`. Registered in `frontend/src/lib/i18n.js` (`SUPPORTED_LANGUAGES`); the picker (`LanguageSwitcher.jsx`) reads that list. RTL (`ar`, `ur`, `fa`) sets `<html dir="rtl">` on language change.

## Required: missing-text test
`frontend/src/lib/locales.test.js` fails if any of the 20 locales is missing, if any locale lacks a key that `en` has, or if a `{{token}}` interpolation differs from `en`. Any new English key must be added to all 20 locales.

## Human review required before release
New translations are machine-generated. Strings below touch **legal, financial, tax, privacy, or safety/crisis** wording and must be reviewed by a fluent human (and counsel/CPA where relevant) in every new language before release. Any string that could not be translated falls back to English text.

Flagged keys (`en` key paths):

- `salestaxtab.taxJurisdictions`
- `salestaxtab.everywhereYouCollectSalesTaxState`
- `salestaxtab.noJurisdictionsYetAddTheFirst`
- `salestaxtab.ofSalesTaxIsnTTagged`
- `salestaxtab.taxCollected`
- `teaminvites.inviteYourAccountantOrTaxPro`
- `auditlog.eGTaxInvoiceLogin`
- `cpapackage.mileageLogWithIRSRatesApplied`
- `customers.delete`
- `customers.deleteConfirm`
- `estimates.tax`
- `expenses.taxID`
- `expenses.taxYear`
- `expenses.tax`
- `expenses.taxIDEIN`
- `invoices.refund`
- `invoices.taxJurisdictionOptional`
- `invoices.tax`
- `invoices.refundCredit`
- `invoices.refundTheRemaining`
- `mileage.irsLogCSV`
- `payroll.deleteDraft`
- `payroll.partnershipStubWhenAProviderIs`
- `payroll.employerTaxEst`
- `portalview.tax`
- `recurringinvoices.tax`
- `simulator.safetyFloor`
- `tax.nameIsRequired`
- `tax.contractorAdded`
- `tax.w9StatusUpdated`
- `tax.contractorRemoved`
- `tax.enterAPositiveAmount`
- `tax.paymentRecorded`
- `tax.paymentDeleted`
- `tax.statusRefreshed`
- `tax.addContractor`
- `tax.contractors1099s`
- `tax.salesTax`
- `tax.taxbanditsNotConnected`
- `tax.taxbanditsSETUPMd`
- `tax.inTheRepoNoIRSFiling`
- `tax.taxbanditsConnectedIn`
- `tax.sandbox`
- `tax.modeNoRealFilingsFlip`
- `tax.taxbanditsSANDBOXFalse`
- `tax.toGoLive`
- `tax.newContractor`
- `tax.name`
- `tax.businessName`
- `tax.email`
- `tax.phone`
- `tax.entityType`
- `tax.ssnEIN`
- `tax.storedSecurely`
- `tax.street`
- `tax.city`
- `tax.state`
- `tax.zip`
- `tax.cancel`
- `tax.contractor`
- `tax.paid`
- `tax.w9`
- `tax.NEC`
- `tax.eFile`
- `tax.actions`
- `tax.viewPaymentHistory`
- `tax.toggleW9Received`
- `tax.required`
- `tax.viewFilingStatus`
- `tax.notFiled`
- `tax.recordPayment`
- `tax.noContractorsYet`
- `tax.NECDataPreview`
- `tax.recipientBox1DataShapedFor`
- `tax.loadPreview`
- `tax.recipient`
- `tax.tin`
- `tax.address`
- `tax.box1`
- `tax.missing`
- `tax.reveal`
- `tax.noContractorsMeetThe`
- `tax.reportingThresholdFor`
- `tax.recordPayment2`
- `tax.date`
- `tax.amount`
- `tax.description`
- `tax.eGMarchBoothRentPayout`
- `tax.record`
- `tax.paymentHistory`
- `tax.noPaymentsRecordedFor`
- `tax.amount2`
- `tax.deletePayment`
- `tax.yearTotal`
- `tax.NECFiling`
- `tax.thisDraftLivesInTaxBanditsReview`
- `tax.NECIsRequiredForNon`
- `tax.inTheYearEFilingCreates`
- `taxforecast.taxForecast`
- `taxforecast.OfPriorYearTax`
- `taxforecast.irsDirectPay`
- `nav.taxForecast`
- `nav.tax1099s`
- `teamInvites.accountantTaxPro`