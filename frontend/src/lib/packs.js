// Response-shape guards for niche-pack payloads.
//
// API contract: GET /niches returns an ARRAY of pack objects;
// GET /niches/resolve and GET /edition return ONE pack object. Anything
// else — e.g. an HTML page that a misconfigured API base tricked axios
// into accepting as data — must degrade to the pages' empty/error
// states, never reach .map/.find/.length as if it were pack data.
// (That exact failure blanked the whole SPA in production.)

export function asPackList(data) {
  return Array.isArray(data) ? data : null;
}

export function findPack(data, id) {
  const list = asPackList(data);
  if (!list || !id) return null;
  return list.find((p) => p && p.id === id) || null;
}

export function asFace(data) {
  return data && typeof data === "object" && !Array.isArray(data) ? data : null;
}

// The niche id a /niches/resolve face represents, for preselecting the
// signup trade. The generic trade-neutral face (apex, www, unknown
// subdomain) yields "" — no preselection, the picker stays untouched.
export function nicheIdFromFace(face) {
  const f = asFace(face);
  if (!f || !f.niche_id || f.niche_id === "generic") return "";
  return f.niche_id;
}
