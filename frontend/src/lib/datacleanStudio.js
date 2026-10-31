// Pure helpers for the DataClean Studio page — the signed-in customer
// surface over the key-authenticated DataClean API
// (backend/routers/dataclean_api.py). Framework-free so the shaping
// rules are unit-tested without rendering. Every shape here mirrors
// the API as implemented: kinds + fields come from the same FIELDS
// table the API validates against (backend/routers/importer.py), and
// the result shapes are the ones the phase-3 backend tests pin.

/** Kinds + fields, mirroring backend FIELDS in routers/importer.py —
 * the API rejects any field outside this table with unknown_field. */
export const KIND_FIELDS = {
  customers: ["name", "email", "phone", "address"],
  vendors: ["name", "email", "phone", "address"],
  items: ["name", "price", "sku", "kind"],
};

export const STUDIO_KINDS = Object.keys(KIND_FIELDS);

/** The API's per-call ceilings (routers/dataclean_api.py) — the page
 * states them up front instead of letting the server refuse. */
export const STUDIO_CAPS = {
  maxRows: 5000,
  maxDedupeRecords: 500,
  maxNormalizeValues: 1000,
};

/** A DataClean key is `mdck_` + a urlsafe secret (lib/dataclean_api.py).
 * Shape check for the connect form only — the server is the authority
 * and answers a generic 401 for anything it does not know. */
export function keyLooksValid(key) {
  return /^mdck_[A-Za-z0-9_-]{8,}$/.test(String(key || "").trim());
}

/** CSV-escape one cell per RFC 4180: quote when the value carries a
 * comma, quote, or newline; double internal quotes. */
function csvCell(v) {
  const s = v == null ? "" : String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** Serialize cleaned records back to CSV in the kind's field order —
 * the file the customer takes away. Columns are exactly the kind's
 * fields, even when a record omits one. */
export function recordsToCsv(records, kind) {
  const fields = KIND_FIELDS[kind] || [];
  const lines = [fields.join(",")];
  for (const r of records || []) {
    lines.push(fields.map((f) => csvCell(r?.[f])).join(","));
  }
  return lines.join("\n") + "\n";
}

/** Summary of a cleanup/clean result: row + change totals and the
 * per-rule counts, busiest rule first (ties by rule name). */
export function cleanSummary(clean) {
  const byRule = Object.entries(clean?.changes_by_rule || {})
    .map(([rule, count]) => ({ rule, count: Number(count) || 0 }))
    .sort((a, b) => b.count - a.count || a.rule.localeCompare(b.rule));
  return {
    rows: Number(clean?.rows) || 0,
    totalChanges: (clean?.changes || []).length,
    byRule,
  };
}

/** Resolve a dedupe pair's ids back to the submitted records. The API
 * assigns `row-N` ids to records that arrive without one (1-based, in
 * submission order); a record's own `id` wins when present. Either
 * record may come back null when the id is unknown. */
export function resolvePair(pair, records) {
  const byId = new Map();
  (records || []).forEach((r, i) => {
    byId.set(String(r?.id || `row-${i + 1}`), r);
  });
  const a = byId.get(String(pair?.a_id)) || null;
  const b = byId.get(String(pair?.b_id)) || null;
  return {
    a,
    b,
    aName: a?.name || String(pair?.a_id || ""),
    bName: b?.name || String(pair?.b_id || ""),
    score: Number(pair?.score) || 0,
    evidence: pair?.evidence || {},
  };
}

/** The records a duplicate scan sends: the first 500 cleaned records
 * (the endpoint's per-scan cap), each projected onto the kind's flat
 * fields. The projection matters: clean_row's output also carries
 * engine helper keys (name_key, address_parts) that are not part of
 * the API's flat record shape — the scan's records are the same flat
 * shape the cleanup endpoints accept. */
export function dedupeBatch(records, kind) {
  const fields = KIND_FIELDS[kind] || [];
  const all = records || [];
  const projected = all.map((r) => {
    const out = {};
    for (const f of fields) {
      if (r?.[f] != null && r[f] !== "") out[f] = r[f];
    }
    return out;
  });
  return {
    records: projected.slice(0, STUDIO_CAPS.maxDedupeRecords),
    truncated: all.length > STUDIO_CAPS.maxDedupeRecords,
  };
}

/** Split the normalize textarea into values: one per line, blanks and
 * surrounding whitespace dropped. */
export function splitValues(text) {
  return String(text || "")
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}
