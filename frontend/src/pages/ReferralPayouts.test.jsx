/** Referral payouts operator surface: balances and Melio queue amounts
 * are formatted in the workspace's market currency via the shared
 * moneyFor (the PR #187 pattern from the member Referral page) — a GB
 * workspace reads £, and a face with no currency falls back to USD.
 * The API layer is mocked; money math is covered backend-side. */
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import { api } from "@/lib/api";
import ReferralPayouts from "./ReferralPayouts";

jest.mock("@/lib/api", () => ({
  __esModule: true,
  default: { post: jest.fn() },
  api: { post: jest.fn(), get: jest.fn() },
  errMsg: (e) => e?.response?.data?.detail?.message || "Request failed",
}));

jest.mock("sonner", () => ({
  toast: { error: jest.fn(), success: jest.fn(), info: jest.fn() },
}));

jest.mock("@/context/AuthContext", () => ({
  useAuth: () => ({ user: { role: "owner", email: "owner@example.com" } }),
}));

const mockNiche = { current: null };
jest.mock("@/context/NicheContext", () => ({
  useNiche: () => mockNiche.current,
}));

const US_FACE = {
  pack: { product_name: "The HVAC Ledger" },
  market: { name: "United States", currency: "USD" },
};
const GB_FACE = {
  pack: { product_name: "The Plumber's Ledger" },
  market: { name: "United Kingdom", currency: "GBP" },
};

const BALANCES = [
  {
    empire_id: "emp-1", name: "Pat Referrer", email: "pat@example.com",
    code: "PAT12345", payout_preference: "melio",
    frozen: false, under_review: false,
    sites: {
      books: {
        paying_referrals: 5, unlock_at: 3,
        payable: 12345, locked: true, locked_cents: 5000,
      },
    },
  },
];
const QUEUE = [
  {
    id: "q1", referrer_email: "pat@example.com", referrer_code: "PAT12345",
    product: "books", preference: "paper_check",
    gross_cents: 20000, fee_cents: 200, net_cents: 19800,
    status: "queued",
  },
];

beforeEach(() => {
  jest.clearAllMocks();
  mockNiche.current = US_FACE;
  api.get.mockImplementation((url) =>
    Promise.resolve(
      url.startsWith("/referrals/admin/balances") ? BALANCES : QUEUE));
});

test("amounts are formatted in the workspace's market currency (GBP face)", async () => {
  mockNiche.current = GB_FACE;
  render(<ReferralPayouts />);
  // Balances table: payable + locked.
  expect(await screen.findByText("£123.45")).toBeTruthy();
  expect(screen.getByText("£50.00")).toBeTruthy();
  // Melio queue: gross + fee + net.
  expect(screen.getByText("£200.00")).toBeTruthy();
  expect(screen.getByText("£2.00")).toBeTruthy();
  expect(screen.getByText("£198.00")).toBeTruthy();
  // No data amount renders in dollars. (The "$2.00 fee" wording in the
  // card heading is settled copy stating the fixed fee, not an amount.)
  expect(screen.queryByText("$123.45")).toBeNull();
  expect(screen.queryByText("$50.00")).toBeNull();
  expect(screen.queryByText("$200.00")).toBeNull();
  expect(screen.queryByText("$198.00")).toBeNull();
});

test("amounts fall back to USD when the face carries no currency", async () => {
  mockNiche.current = {
    pack: { product_name: "The HVAC Ledger" },
    market: { name: "Dallas–Fort Worth" },
  };
  render(<ReferralPayouts />);
  expect(await screen.findByText("$123.45")).toBeTruthy();
  expect(screen.getByText("$50.00")).toBeTruthy();
  expect(screen.getByText("$200.00")).toBeTruthy();
  expect(screen.getByText("$198.00")).toBeTruthy();
});
