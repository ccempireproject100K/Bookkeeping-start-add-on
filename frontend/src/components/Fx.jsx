import { money, fmtDate } from "@/lib/format";

// Multi-currency display bits (#16). Foreign-currency documents show BOTH
// amounts everywhere: their own (transaction) currency first, the home
// equivalent under it. Home-currency documents render exactly as before.

export function DualAmount({ doc, homeCents, txnCents }) {
  if (!doc?.is_foreign) return <span>{money(homeCents, doc?.home_currency)}</span>;
  return (
    <span>
      {money(txnCents, doc.txn_currency)}
      <span className="block text-[11px] text-gray-500">
        ≈ {money(homeCents, doc.home_currency)} at the stamped rate
      </span>
    </span>
  );
}

// The document's stamp, small and plain: rate · source · date, with the
// stale flag said out loud when the rate table couldn't refresh.
export function FxStamp({ doc }) {
  if (!doc?.is_foreign) return null;
  return (
    <div className="text-[11px] text-gray-500 mt-0.5" data-testid={`fx-stamp-${doc.id}`}>
      1 {doc.txn_currency} = {doc.fx_rate} {doc.home_currency} · {doc.fx_source} · {fmtDate(doc.fx_rate_date)}
      {doc.fx_stale && <span className="text-amber-500"> · stale rate — last stored rate used</span>}
    </div>
  );
}

// Realized gain/loss line for a payment result (both rates shown).
export function fxResultText(r, homeCurrency) {
  const gl = r?.fx_gain_loss_cents || 0;
  if (!gl) return null;
  const kind = gl > 0 ? "FX gain" : "FX loss";
  return `${kind} ${money(Math.abs(gl), homeCurrency)} (invoice rate ${r.fx_rate_invoice || r.fx_rate_bill}, paid at ${r.fx_rate_payment})`;
}

export const FX_OUT_OF_SCOPE_NOTE =
  "Multi-currency covers invoices & bills with realized gain/loss at payment. Not yet: period-end revaluation of open balances, foreign-currency bank accounts, multi-currency payroll & tax.";
