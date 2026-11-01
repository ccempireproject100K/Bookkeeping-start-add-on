/** Platform credentials operator console: the referrals host's mint
 * screen. Pins the operator gating (owner-only, mirroring
 * ReferralPayouts), the metadata-only list (the GET endpoint returns
 * public_view rows — no secret material exists to render), and the
 * once-only secret handling: the minted/rotated value appears in the
 * reveal dialog, is never written to localStorage, and is gone after
 * the dialog closes and after navigation (unmount/remount).
 * The API layer is mocked; the endpoints are covered backend-side. */
import { render, screen, fireEvent } from "@testing-library/react";
import "@/lib/i18n";
import { api } from "@/lib/api";
import PlatformCredentials from "./PlatformCredentials";

jest.mock("@/lib/api", () => ({
  __esModule: true,
  default: { post: jest.fn() },
  api: { post: jest.fn(), get: jest.fn() },
  errMsg: (e) => e?.response?.data?.detail?.message || "Request failed",
}));

jest.mock("sonner", () => ({
  toast: { error: jest.fn(), success: jest.fn(), info: jest.fn() },
}));

const mockAuth = { current: { user: { role: "owner", email: "o@x.com" } } };
jest.mock("@/context/AuthContext", () => ({
  useAuth: () => mockAuth.current,
}));

// Exactly the backend public_view shape — metadata only, no secret
// fields exist in this payload.
const ROWS = [
  {
    id: "cred-1", kind: "platform", platform: "momo",
    client_id: "mae1_client_aaa", label: "AI Office production",
    scopes: ["referrals:sync"],
    created_at: "2026-11-01T10:00:00+00:00",
    created_by: "owner@example.com",
    expires_at: "2027-11-01T10:00:00+00:00",
    revoked_at: null, rotated_at: null, last_used_at: null,
  },
  {
    id: "cred-2", kind: "platform", platform: "trade_circle",
    client_id: "mae1_client_bbb", label: "Trade Circle (old)",
    scopes: ["referrals:sync"],
    created_at: "2026-10-01T10:00:00+00:00",
    created_by: "owner@example.com",
    expires_at: "2027-10-01T10:00:00+00:00",
    revoked_at: "2026-10-15T10:00:00+00:00",
    rotated_at: null,
    last_used_at: "2026-10-10T10:00:00+00:00",
  },
];
const MINTED = "mae1_mintclient_MINTEDSECRETVALUE";
const ROTATED = "mae1_mintclient_ROTATEDSECRETVALUE";

beforeEach(() => {
  jest.clearAllMocks();
  localStorage.clear();
  mockAuth.current = { user: { role: "owner", email: "o@x.com" } };
  window.confirm = jest.fn(() => true);
  api.get.mockResolvedValue(ROWS);
  api.post.mockImplementation((url) => {
    if (url === "/referrals/admin/platform-credentials") {
      return Promise.resolve({ ...ROWS[0], credential: MINTED });
    }
    if (url.endsWith("/rotate")) {
      return Promise.resolve({ ...ROWS[0], credential: ROTATED });
    }
    return Promise.resolve({ ok: true, id: "cred-1" });
  });
});

test("owner session sees the console: mint form and credential list", async () => {
  render(<PlatformCredentials />);
  expect(await screen.findByText("AI Office production")).toBeTruthy();
  expect(screen.getByText("Mint a credential")).toBeTruthy();
  expect(screen.getByText("Mint credential")).toBeTruthy();
  expect(screen.getByText("Existing credentials")).toBeTruthy();
  expect(api.get).toHaveBeenCalledWith(
    "/referrals/admin/platform-credentials");
});

test("list renders metadata only — no credential value anywhere", async () => {
  render(<PlatformCredentials />);
  expect(await screen.findByText("AI Office production")).toBeTruthy();
  // Metadata fields render: platform, client_id, status, created_by.
  // "momo" appears in the table row AND the mint form's platform select.
  expect(screen.getAllByText("momo").length).toBeGreaterThanOrEqual(1);
  expect(screen.getByText("mae1_client_aaa")).toBeTruthy();
  expect(screen.getByText("Active")).toBeTruthy();
  expect(screen.getByText("Revoked")).toBeTruthy();
  expect(screen.getAllByText("by owner@example.com").length).toBe(2);
  // Nothing credential-shaped (mae1_<client>_<secret>) and no hash
  // vocabulary leaks into the list render.
  expect(document.body.textContent).not.toMatch(/mae1_\w+_\w{8,}/);
  expect(document.body.textContent).not.toMatch(/secret_hash/);
});

test("non-owner session gets the owner-only notice and no console", async () => {
  mockAuth.current = { user: { role: "bookkeeper", email: "b@x.com" } };
  render(<PlatformCredentials />);
  expect(await screen.findByText(
    "Platform credentials are managed by the workspace owner."))
    .toBeTruthy();
  expect(screen.queryByText("Mint credential")).toBeNull();
  expect(screen.queryByText("Existing credentials")).toBeNull();
  // The admin endpoint is never even called for a non-owner.
  expect(api.get).not.toHaveBeenCalled();
});

test("mint reveals the value once; closing and navigating away clears it",
  async () => {
    const { unmount } = render(<PlatformCredentials />);
    expect(await screen.findByText("AI Office production")).toBeTruthy();
    fireEvent.change(
      screen.getByPlaceholderText("e.g. AI Office production"),
      { target: { value: "AI Office production" } });
    fireEvent.click(screen.getByText("Mint credential"));
    // The reveal dialog shows the value and the once-only warning.
    expect(await screen.findByText(MINTED)).toBeTruthy();
    expect(screen.getByText(/only time the full credential is shown/))
      .toBeTruthy();
    expect(api.post).toHaveBeenCalledWith(
      "/referrals/admin/platform-credentials",
      { platform: "momo", label: "AI Office production",
        expires_in_days: 365 });
    // Never persisted.
    expect(JSON.stringify(localStorage)).not.toContain(MINTED);
    // Closing the dialog destroys the only copy.
    fireEvent.click(screen.getByText("Done"));
    expect(screen.queryByText(MINTED)).toBeNull();
    // Navigation (unmount + fresh mount) cannot resurrect it.
    unmount();
    render(<PlatformCredentials />);
    expect(await screen.findByText("AI Office production")).toBeTruthy();
    expect(screen.queryByText(MINTED)).toBeNull();
  });

test("rotate reveals the new value once; revoke posts after confirm",
  async () => {
    render(<PlatformCredentials />);
    expect(await screen.findByText("AI Office production")).toBeTruthy();
    const rotateBtns = screen.getAllByText("Rotate");
    expect(rotateBtns.length).toBe(1); // revoked row offers no actions
    fireEvent.click(rotateBtns[0]);
    expect(await screen.findByText(ROTATED)).toBeTruthy();
    expect(api.post).toHaveBeenCalledWith(
      "/referrals/admin/platform-credentials/cred-1/rotate", {});
    fireEvent.click(screen.getByText("Done"));
    expect(screen.queryByText(ROTATED)).toBeNull();

    fireEvent.click(screen.getAllByText("Revoke")[0]);
    expect(api.post).toHaveBeenCalledWith(
      "/referrals/admin/platform-credentials/cred-1/revoke", {});
  });
