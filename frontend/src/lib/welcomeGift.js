/**
 * Welcome Gift helpers — the pure logic behind the onboarding gift
 * page (WelcomeGift.jsx), kept separate so it is unit-testable like
 * datacleanStudio.js.
 *
 * The gift runs on the tenant's own books session through
 * /welcome-gift (no DataClean API key, no plan): the server cleans
 * the uploaded file with the same engine path the DataClean Studio
 * uses and runs the tax engine for the estimate. What the UI must
 * never fudge, mirrored here:
 *  - a jurisdiction the engine marks NOT_IMPLEMENTED is shown as
 *    exactly that, with the engine's own note;
 *  - an unbuilt engine arrives as error code `engine_not_built` and
 *    renders an honest unavailable state, never a zero.
 */
export const GIFT_KINDS = ['customers', 'vendors', 'items'];

export const KIND_FIELDS = {
  customers: ['name', 'email', 'phone', 'address'],
  vendors: ['name', 'email', 'phone', 'address'],
  items: ['name', 'price', 'sku', 'kind'],
};

const money = (cents) =>
  '$' + ((Number(cents) || 0) / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Cleaned gift records -> the CSV the tenant downloads and keeps. */
export function giftRecordsToCsv(records, kind) {
  const fields = KIND_FIELDS[kind] || KIND_FIELDS.customers;
  const esc = (v) => {
    const s = v === null || v === undefined ? '' : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [fields.join(',')];
  for (const r of records || []) lines.push(fields.map((f) => esc(r[f])).join(','));
  return lines.join('\n');
}

/** Duplicate pairs -> a small readable report (also a download). */
export function duplicateReportText(pairs, kind) {
  const lines = [
    `Duplicate report — ${kind}`,
    pairs && pairs.length
      ? `${pairs.length} likely duplicate pair(s) found by the DataClean engine.`
      : 'No likely duplicates found.',
    '',
  ];
  (pairs || []).forEach((p, i) => {
    const a = (p.records && p.records[0]) || {};
    const b = (p.records && p.records[1]) || {};
    lines.push(`${i + 1}. ${a.name || a.id || '?'}  <>  ${b.name || b.id || '?'}`);
    lines.push(`   score ${p.score} — ${(p.evidence || []).join('; ') || 'no evidence listed'}`);
  });
  return lines.join('\n');
}

/** Download a text body as a file (the cleaned CSV / the report). */
export function downloadText(text, filename) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

/** 'engine_not_built' when the 503 says the engine isn't built here. */
export function giftErrorCode(err) {
  const d = err && err.response && err.response.data && err.response.data.detail;
  return (d && typeof d === 'object' && d.code) || null;
}

export function giftErrorMessage(err, fallback) {
  const d = err && err.response && err.response.data && err.response.data.detail;
  if (d && typeof d === 'object' && d.message) return d.message;
  if (typeof d === 'string') return d;
  return fallback;
}

/**
 * The estimate, flattened for display. `partial` is true whenever the
 * engine (or the federal-only fallback) says the total does not cover
 * everything — the UI then shows the notes before the number and
 * labels the total as partial. Nothing here invents a figure: a
 * NOT_IMPLEMENTED state simply has no state component in the
 * breakdown, and its note says why.
 */
export function estimateSummary(result) {
  if (!result) return null;
  const breakdown = (result.breakdown || []).map((c) => ({
    key: `${c.jurisdiction}-${c.type}`,
    label: c.label,
    jurisdiction: c.jurisdiction,
    amount: money(c.amountCents),
  }));
  const schedule = (result.schedule || []).map((p) => ({
    quarter: p.quarter,
    dueDate: p.dueDate,
    amount: money(p.amountDueCents),
  }));
  const safe = (result.safeHarbor && result.safeHarbor.federal) || {};
  const workpapers = (result.workpapers || []).map((w) => ({
    jurisdiction: w.jurisdiction,
    config: `${w.configVersion} · hash ${String(w.configHash || '').slice(0, 12)}…`,
    provisional: !!w.provisional,
    steps: (w.steps || []).map((s) => ({
      label: s.label,
      detail: s.detail,
      amount: s.amountCents === null || s.amountCents === undefined ? null : money(s.amountCents),
    })),
  }));
  return {
    status: result.status,
    partial: result.status !== 'OK',
    notes: result.notes || [],
    total: money(result.totalLiabilityCents),
    breakdown,
    schedule,
    safeHarbor: safe.requiredAnnualPaymentCents === undefined
      ? null
      : { amount: money(safe.requiredAnnualPaymentCents), basis: safe.basis },
    workpapers,
  };
}

/** The short estimate form -> the API body (dollars stay dollars). */
export function estimateBody(form) {
  const num = (v) => (v === '' || v === null || v === undefined ? undefined : Number(v));
  const body = {
    net_profit: Number(form.netProfit || 0),
    filing_status: form.filingStatus,
    state: form.state ? form.state.toUpperCase() : undefined,
  };
  const w2 = num(form.w2Wages);
  if (w2 !== undefined) body.w2_wages = w2;
  const paid = num(form.paymentsMade);
  if (paid !== undefined) body.payments_already_made = paid;
  return body;
}
