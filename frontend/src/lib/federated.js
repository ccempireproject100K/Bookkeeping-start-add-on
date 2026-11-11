// Federated (social) sign-in — frontend half.
//
// The backend is the source of truth for which providers are ON:
// GET /api/auth/config returns per-provider {enabled} flags driven by
// server env keys (GOOGLE_*, FACEBOOK_*, MICROSOFT_*, LINKEDIN_*).
// A provider whose keys are unset is simply absent here — no button,
// no dead UI, email/password untouched. Apple is ready-only (its
// $99/yr developer gate is an owner decision) and never appears.
import { api } from "@/lib/api";

export const FEDERATED_PROVIDERS = [
  { name: "google", label: "Google", badge: "G", color: "#4285F4" },
  { name: "facebook", label: "Facebook", badge: "f", color: "#1877F2" },
  { name: "microsoft", label: "Microsoft", badge: "M", color: "#00A4EF" },
  { name: "linkedin", label: "LinkedIn", badge: "in", color: "#0A66C2" },
];

const API_BASE = `${process.env.REACT_APP_BACKEND_URL}/api`;

// Start URL for a provider: the backend redirects to the provider's
// consent screen and its callback signs the user in (or creates the
// account — a first social sign-in IS the signup).
export function federatedStartUrl(providerName) {
  return `${API_BASE}/auth/${providerName}`;
}

export async function fetchEnabledProviders() {
  try {
    const cfg = await api.get("/auth/config");
    const providers = cfg?.providers || {};
    return FEDERATED_PROVIDERS.filter((p) => providers[p.name]?.enabled);
  } catch {
    return []; // config unreachable = social sign-in off, never faked
  }
}
