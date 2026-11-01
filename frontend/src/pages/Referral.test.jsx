/** Referral member page: renders the ledger summary/terms payload —
 * link/code, qualification progress (0-4), adopted schedule bands,
 * friend benefit, per-referral band, and the $2 check-fee disclosure.
 * The API layer is mocked; money math is covered backend-side. */
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import "@/lib/i18n";
import { api } from "@/lib/api";
import Referral from "./Referral";

jest.mock("@/lib/api", () => ({
  api: { post: jest.fn(), get: jest.fn() },
  errMsg: (e) => e?.response?.data?.detail?.message || "Request failed",
}));

jest.mock("sonner", () => ({
  toast: { error: jest.fn(), success: jest.fn() },
}));

jest.mock("@/context/NicheContext", () => ({
  useNiche: () => ({
    pack: { product_name: "The HVAC Ledger" },
    market: { name: "Dallas–Fort Worth" },
  }),
}));

const SUMMARY = {
  code: "SMPL1234",
  link: "/register?ref=SMPL1234",
  referral_active: true,
  sites: {
    books: {
      qualified: false, qualified_at: null, qualify_at: 4,
      paying_referrals: 3, earned_cents: 0, payable: 0, paid: 0,
    },
  },
  referrals: [
    { product: "books", account_id: "shop-1", current_band_rate: null,
      bonus_year_locked: false, friend_benefit: { type: "tier_up", tier: "medium", days: 14 } },
  ],
  free_months: [
    { product: "books", start: "2026-11-01T00:00:00+00:00",
      end: "2026-12-01T00:00:00+00:00", source: "third_paying_referral", plan: "medium" },
  ],
  terms: {
    bands: [
      { through_month: 6, rate: 0.25 }, { through_month: 12, rate: 0.1 },
      { through_month: 24, rate: 0.02 }, { through_month: 36, rate: 0.01 },
    ],
    bonus_band: { through_month: 48, rate: 0.01, threshold: 250 },
    bonus_threshold: 250, grace_months: 3, clearing_days: 60,
  },
};

beforeEach(() => {
  jest.clearAllMocks();
  api.get.mockResolvedValue(SUMMARY);
});

test("renders link, progress 3/4, adopted bands and disclosures", async () => {
  render(
    <MemoryRouter>
      <Referral />
    </MemoryRouter>
  );
  expect((await screen.findByTestId("referral-code")).textContent).toContain("SMPL1234");
  expect(screen.getByTestId("referral-link").textContent).toContain("ref=SMPL1234");
  expect(screen.getByTestId("referral-progress").textContent).toContain("3 / 4");
  expect(screen.getByText(/Months 1–6/)).toBeTruthy();
  expect(screen.getByText(/Months 7–12/)).toBeTruthy();
  expect(screen.getByText(/Months 13–24/)).toBeTruthy();
  expect(screen.getByText(/Months 25–36/)).toBeTruthy();
  expect(screen.getByText(/25%/)).toBeTruthy();
  expect(screen.getByText(/10%/)).toBeTruthy();
  expect(screen.getByText(/\$2 fee is deducted/)).toBeTruthy();
  expect(screen.getByTestId("referral-free-month")).toBeTruthy();
  expect(screen.getByText(/Fourth year \(months 37–48\)/)).toBeTruthy();
  expect(screen.getByText(/250 or more active paying referrals/)).toBeTruthy();
});
