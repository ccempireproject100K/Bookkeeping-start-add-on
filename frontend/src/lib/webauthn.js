import { api } from "@/lib/api";

/** base64url <-> ArrayBuffer conversions for the WebAuthn browser API. */
export function b64urlToBuffer(b64url) {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

export function bufferToB64url(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

const supported = () =>
  typeof window !== "undefined" && !!window.PublicKeyCredential;

function toPublicKeyCreation(options) {
  return {
    ...options,
    challenge: b64urlToBuffer(options.challenge),
    user: { ...options.user, id: b64urlToBuffer(options.user.id) },
    excludeCredentials: (options.excludeCredentials || []).map((c) => ({
      ...c, id: b64urlToBuffer(c.id),
    })),
  };
}

function toPublicKeyRequest(options) {
  return {
    ...options,
    challenge: b64urlToBuffer(options.challenge),
    allowCredentials: (options.allowCredentials || []).map((c) => ({
      ...c, id: b64urlToBuffer(c.id),
    })),
  };
}

function credentialToJson(cred) {
  const out = { id: cred.id, type: cred.type, rawId: bufferToB64url(cred.rawId), response: {} };
  for (const k of ["clientDataJSON", "attestationObject", "authenticatorData", "signature", "userHandle"]) {
    const v = cred.response[k];
    if (v) out.response[k] = bufferToB64url(v);
  }
  return out;
}

/** Full registration ceremony. Returns when the server has stored the passkey. */
export async function registerPasskey(name) {
  if (!supported()) throw new Error("This browser doesn't support passkeys");
  const options = await api.post("/webauthn/register/options", {});
  const cred = await navigator.credentials.create({
    publicKey: toPublicKeyCreation(options),
  });
  if (!cred) throw new Error("Passkey creation was cancelled");
  await api.post("/webauthn/register/verify", {
    credential: credentialToJson(cred), name: name || "Passkey",
  });
}

/** Full passwordless login ceremony. Returns the /auth/me-style user object. */
export async function loginWithPasskey(email) {
  if (!supported()) throw new Error("This browser doesn't support passkeys");
  const options = await api.post("/webauthn/login/options", { email });
  const cred = await navigator.credentials.get({
    publicKey: toPublicKeyRequest(options),
  });
  if (!cred) throw new Error("Passkey sign-in was cancelled");
  return api.post("/webauthn/login/verify", {
    email, credential: credentialToJson(cred),
  });
}

export const passkeysSupported = supported;
