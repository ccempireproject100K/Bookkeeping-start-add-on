// Pure helpers for the Cleanup Console — kept framework-free so the
// sorting, grading, and diff rules are unit-testable without rendering.

/** Color semantics shared with the books health score: green is good,
 * amber needs a look, red needs action. Returns a tone key. */
export function gradeTone(score) {
  if (score == null || Number.isNaN(Number(score))) return "gray";
  const s = Number(score);
  if (s >= 75) return "emerald";
  if (s >= 55) return "amber";
  return "rose";
}

export const TONE_CLASSES = {
  emerald: { text: "text-emerald-400", bar: "bg-emerald-400", ring: "border-emerald-500/40" },
  amber: { text: "text-amber-400", bar: "bg-amber-400", ring: "border-amber-500/40" },
  rose: { text: "text-rose-400", bar: "bg-rose-400", ring: "border-rose-500/40" },
  gray: { text: "text-gray-400", bar: "bg-gray-500", ring: "border-[#222838]" },
};

/** A file's mess health, 0–100, derived ONLY from the preview payload:
 * start at 100, lose points for empty cells, invalid values, and any
 * blocking schema problem. Mirrors the health-score banding so the two
 * meters read the same way. */
export function messHealth(preview) {
  if (!preview) return null;
  const cols = preview.mess_report?.columns || [];
  if (!cols.length) return null;
  const rows = Math.max(preview.mess_report?.rows || preview.rows || 0, 1);
  let penalty = 0;
  for (const c of cols) {
    penalty += (c.empty_rate || 0) * 10;
    const issues = c.issues || {};
    const bad = (issues.invalid_emails || 0) + (issues.invalid_phones || 0)
      + (issues.unparseable_dates || 0);
    penalty += Math.min(25, (bad / rows) * 50);
    if (issues.mixed_date_formats) penalty += 5;
  }
  if ((preview.schema?.blocking || []).length) penalty += 40;
  penalty += Math.min(10, (preview.schema?.row_error_count || 0) * 0.5);
  return Math.max(0, Math.min(100, Math.round(100 - penalty)));
}

/** Dedupe confidence → translation key for the plain-language label.
 * The bar shows the number; the label is what the owner reads. */
export function confidenceLabelKey(score) {
  const s = Number(score) || 0;
  if (s >= 0.97) return "cleanupConsole.confidenceVeryHigh";
  if (s >= 0.9) return "cleanupConsole.confidenceHigh";
  if (s >= 0.8) return "cleanupConsole.confidenceLikely";
  return "cleanupConsole.confidencePossible";
}

/** Sort + filter for the duplicates queue. Sort keys: "score"
 * (default, strongest first), "recent" (newest first), "name". */
export function visibleSuggestions(suggestions, { entity = "all", minScore = 0, sort = "score" } = {}) {
  const out = (suggestions || []).filter(
    (s) => (entity === "all" || s.entity === entity) && (Number(s.score) || 0) >= minScore);
  const by = {
    score: (a, b) => (b.score || 0) - (a.score || 0),
    recent: (a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")),
    name: (a, b) => String(a.a_name || "").localeCompare(String(b.a_name || "")),
  }[sort] || (() => 0);
  return out.sort(by);
}

/** Fields shown side by side for a record pair, with a differs flag.
 * Customers store address in locations[] in the main CRUD API while
 * imported records carry a flat address — read both shapes. */
export function recordFieldRows(a, b) {
  const addr = (r) => r?.address
    || (Array.isArray(r?.locations) && r.locations[0]?.address)
    || "";
  const fields = [
    { key: "name", a: a?.name || "", b: b?.name || "" },
    { key: "email", a: a?.email || "", b: b?.email || "" },
    { key: "phone", a: a?.phone || "", b: b?.phone || "" },
    { key: "address", a: addr(a), b: addr(b) },
  ];
  return fields.map((f) => ({ ...f, differs: String(f.a) !== String(f.b) }));
}

/** Flatten the preview's per-field change buckets into sample rows for
 * the before/after compare. Each change already carries its rule and
 * confidence from the dataclean layer. */
export function previewChangeRows(preview, limit = 12) {
  const buckets = preview?.normalization_preview || {};
  const rows = [];
  for (const field of Object.keys(buckets)) {
    for (const ch of buckets[field]) rows.push({ field, ...ch });
  }
  return rows.slice(0, limit);
}

/** Count normalization changes by rule for the import success state. */
export function countChangesByRule(preview) {
  const counts = {};
  for (const ch of previewChangeRows(preview, Number.MAX_SAFE_INTEGER)) {
    counts[ch.rule] = (counts[ch.rule] || 0) + 1;
  }
  return counts;
}

/* ------------------------- Phase 2 additions ------------------------- */

/** Plain-language label key per cleanup rule. Unknown rules fall back
 * to showing the raw rule name (backend prose stays English). */
export const RULE_LABEL_KEYS = {
  "name:whitespace": "cleanupConsole.ruleNameWhitespace",
  "email:trim-and-lowercase": "cleanupConsole.ruleEmailTrim",
  "phone:libphonenumber-e164": "cleanupConsole.rulePhoneFormat",
  "address:usaddress-canonical": "cleanupConsole.ruleAddressFormat",
};

export function ruleLabelKey(rule) {
  return RULE_LABEL_KEYS[rule] || null;
}

/** The stable order rules are shown in (matches the engine's firing
 * order); unknown rules sort after, alphabetically. */
const RULE_ORDER = Object.keys(RULE_LABEL_KEYS);

/** Effective rule states from a cleanup preview, as display rows:
 * [{rule, enabled, source}] — source is "profile" | "run" | "default",
 * the layer that decided the rule (shown honestly in the UI). */
export function ruleStateRows(preview) {
  const states = preview?.rule_states || {};
  return Object.keys(states)
    .map((rule) => ({ rule, enabled: !!states[rule]?.enabled, source: states[rule]?.source || "default" }))
    .sort((a, b) => {
      const ia = RULE_ORDER.indexOf(a.rule), ib = RULE_ORDER.indexOf(b.rule);
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.rule.localeCompare(b.rule);
    });
}

/** Flatten the preview's HELD change buckets — changes a profile/run
 * rule toggle suppressed. Same row shape as previewChangeRows. */
export function heldChangeRows(preview, limit = 12) {
  const buckets = preview?.held_changes || {};
  const rows = [];
  for (const field of Object.keys(buckets)) {
    for (const ch of buckets[field]) rows.push({ field, ...ch });
  }
  return rows.slice(0, limit);
}

/** A weekly trend is only shown when real persisted runs exist in at
 * least two different weeks — otherwise the tile shows the count alone
 * and never draws a one-point "trend". */
export function hasWeeklyTrend(series) {
  return (series || []).filter((b) => (b?.files || 0) > 0).length >= 2;
}

/** SVG polyline points for the files-per-week sparkline, scaled to the
 * series max. Returns null when there is no honest trend to draw. */
export function sparkPoints(series, width = 96, height = 24, pad = 2) {
  if (!hasWeeklyTrend(series)) return null;
  const vals = series.map((b) => b?.files || 0);
  const max = Math.max(...vals, 1);
  const stepX = (width - pad * 2) / Math.max(vals.length - 1, 1);
  return vals.map((v, i) => {
    const x = pad + i * stepX;
    const y = height - pad - (v / max) * (height - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}
