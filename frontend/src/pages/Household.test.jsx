/** Household page (personal-finance layer, slice 2): the wall and the
 * home screen. Business workspace -> create/switch explanation, no
 * personal data rendered. Household workspace -> net worth, accounts,
 * budgets, bills, goals, insights from the one-call /personal/summary,
 * with goal fund-due catch-up fired on load. API layer is mocked; the
 * money math is covered backend-side. */
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import "@/lib/i18n";
import { api } from "@/lib/api";
import Household from "./Household";

jest.mock("@/lib/api", () => ({
  api: { post: jest.fn(), get: jest.fn(), delete: jest.fn(), del: jest.fn() },
  errMsg: (e) => e?.response?.data?.detail || "Request failed",
}));

jest.mock("sonner", () => ({
  toast: { error: jest.fn(), success: jest.fn() },
}));

const mockAuth = { current: null };
jest.mock("@/context/AuthContext", () => ({
  useAuth: () => mockAuth.current,
}));

const SUMMARY = {
  period: "2026-11",
  net_worth: {
    as_of: "2026-11-18",
    base_currency: "USD",
    assets_cents: 1500000,
    liabilities_cents: 520000,
    net_worth_cents: 980000,
    fx_pending_count: 0,
    manual_items: [
      { id: "a1", name: "House", kind: "property", value_cents: 1000000,
        currency: "USD", converted_cents: 1000000, fx_applied: true },
    ],
    accounts: [],
  },
  accounts: [
    { bank_account_id: "ba1", name: "Checking", kind: "bank",
      currency: null, current_balance_cents: 500000 },
  ],
  balance_history: {
    manual_items_held_flat: true,
    manual_net_cents: 480000,
    points: [
      { date: "2026-11-01", accounts_balance_cents: 400000, net_worth_cents: 880000 },
      { date: "2026-11-18", accounts_balance_cents: 500000, net_worth_cents: 980000 },
    ],
  },
  cash_flow: { period: "2026-11", in_cents: 200000, out_cents: 50000,
               net_cents: 150000, transaction_count: 9 },
  budgets: {
    period: "2026-11",
    rows: [
      { category: "Groceries", budgeted_cents: 40000, spent_cents: 10000,
        remaining_cents: 30000, carried_in_cents: 5000, rollover: true },
    ],
    totals: { budgeted_cents: 40000, spent_cents: 10000,
              remaining_cents: 30000, carried_in_cents: 5000 },
  },
  bills: [
    { id: "b1", name: "Rent", category: "Housing", amount_cents: 150000,
      cadence: "monthly", next_due_date: "2026-11-20", active: true,
      source: "manual", due_state: "due_soon", days_until_due: 2 },
  ],
  goals: [
    { id: "g1", name: "Emergency fund", target_cents: 100000,
      saved_cents: 25000, progress_pct: 25.0, remaining_cents: 75000,
      auto_fund: true, funding_amount_cents: 10000,
      funding_cadence: "monthly", last_funded_date: "2026-11-01" },
  ],
  insights: [
    { kind: "savings_rate", title: "Savings rate",
      detail: "You kept 75.0% of money in this month.", tone: "good" },
  ],
};

/* Slice 3 fold-in surfaces load from their own endpoints. */
const FOLDIN = {
  envelopes: [
    { id: "e1", name: "Grocery pot", category: "Groceries",
      allocated_cents: 60000, effective_allocated_cents: 65000,
      carried_in_cents: 5000, spent_cents: 20000, remaining_cents: 45000,
      remaining_pct: 69.2, state: "green", cycle_start: "2026-11-01",
      cycle_end: "2026-12-01", cycle_start_day: 1, rollover: true,
      members: ["Sam"], visibility: "shared", active: true },
  ],
  safe: {
    as_of: "2026-11-18", next_payday_date: "2026-11-20",
    payday_estimated: false, safe_to_spend_cents: 273000, raw_cents: 273000,
    parts: { account_balance_cents: 500000,
             upcoming_bills_before_payday_cents: 102000,
             envelope_obligations_cents: 75000,
             vault_locks_cents: 50000, vault_balances_cents: 30000,
             locked_goals_cents: 20000 },
    plain_words: "Your balance, minus bills due before payday, minus money promised to envelopes, minus locked vault money. What is left is safe to spend.",
    accounts: [], bills_considered: [],
    envelopes: [{ envelope_id: "e1", name: "Grocery pot",
                  remaining_cents: 45000, state: "green" }],
    largest_envelope: { name: "Grocery pot", remaining_cents: 45000 },
  },
  methods: [
    { key: "day_of_month", name: "Day-of-Month Stack", cadence: "daily",
      description: "Save $1 on day 1, $2 on day 2.",
      enrolled: true, variant: "standard", config: {},
      saved_total_cents: 1500, actions_taken: 5, recent_events: [],
      cycle_goal_cents: 49600, saved_this_cycle_cents: 1500,
      expected_by_today_cents: 17100, pace: "behind",
      yearly_pace: { amount_cents: 595200, label: "EXACT",
                     basis: "$496 a month" },
      next_action: { action: "save_day", label: "Save $18 for day 18" },
      progress_pct: 3.0, complete: false },
    { key: "round_ups", name: "Round-Ups", cadence: "per purchase",
      description: "Spare change sweeps to savings.",
      enrolled: false, variant: "standard", config: {},
      saved_total_cents: 0, actions_taken: 0, recent_events: [],
      cycle_goal_cents: 0, yearly_pace: null, next_action: null,
      progress_pct: 0, complete: false },
  ],
  vaults: [
    { id: "v1", name: "Rainy day vault", balance_cents: 30000,
      withdrawal_delay_hours: 48, pending_withdrawal: null },
  ],
  age: { as_of: "2026-11-18", age_days: 40,
         oldest_unspent_income_date: "2026-10-09",
         unspent_income_cents: 120000 },
};

const EMPTY_FOLDIN = { envelopes: [], safe: null, methods: [], vaults: [],
                       age: null };

function mockGets(summary, foldin) {
  api.get.mockImplementation((url) => {
    if (url === "/personal/summary") return Promise.resolve(summary);
    if (url === "/personal/envelopes") return Promise.resolve(foldin.envelopes);
    if (url === "/personal/safe-to-spend") return Promise.resolve(foldin.safe);
    if (url === "/personal/savings-methods") return Promise.resolve(foldin.methods);
    if (url === "/personal/vaults") return Promise.resolve(foldin.vaults);
    if (url === "/personal/age-of-money") return Promise.resolve(foldin.age);
    return Promise.resolve({});
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  api.post.mockResolvedValue({});
  mockGets(SUMMARY, FOLDIN);
  api.delete.mockResolvedValue({});
  api.del.mockResolvedValue({});
});

test("business workspace sees the wall, not personal data", async () => {
  mockAuth.current = {
    user: { tenant: { kind: undefined, name: "Shop" }, role: "owner" },
    refresh: jest.fn(),
  };
  render(
    <MemoryRouter>
      <Household />
    </MemoryRouter>,
  );
  expect(await screen.findByTestId("household-not-household")).toBeTruthy();
  expect(screen.getByTestId("household-create")).toBeTruthy();
  expect(api.get).not.toHaveBeenCalledWith("/personal/summary");
});

test("brand-new household sees empty states, not bare cards", async () => {
  mockAuth.current = {
    user: { tenant: { kind: "household", name: "Home" }, role: "owner" },
    refresh: jest.fn(),
  };
  mockGets({
    ...SUMMARY,
    accounts: [],
    balance_history: { manual_items_held_flat: true, manual_net_cents: 0, points: [] },
    budgets: { period: "2026-11", rows: [],
               totals: { budgeted_cents: 0, spent_cents: 0,
                         remaining_cents: 0, carried_in_cents: 0 } },
    bills: [],
    goals: [],
    insights: [],
    net_worth: { ...SUMMARY.net_worth, manual_items: [] },
  }, EMPTY_FOLDIN);
  render(
    <MemoryRouter>
      <Household />
    </MemoryRouter>,
  );
  expect(await screen.findByTestId("household-page")).toBeTruthy();
  expect(screen.getByTestId("household-chart-empty")).toBeTruthy();
  expect(screen.getByText(/No budgets yet/)).toBeTruthy();
  expect(screen.getByText(/No bills tracked yet/)).toBeTruthy();
  expect(screen.getByText(/No savings goals yet/)).toBeTruthy();
  expect(screen.getByText(/No assets or debts added yet/)).toBeTruthy();
});

test("household workspace renders the home screen", async () => {
  mockAuth.current = {
    user: { tenant: { kind: "household", name: "Home" }, role: "owner" },
    refresh: jest.fn(),
  };
  render(
    <MemoryRouter>
      <Household />
    </MemoryRouter>,
  );
  expect(await screen.findByTestId("household-page")).toBeTruthy();
  expect(screen.getByTestId("household-net-worth").textContent).toContain("9,800");
  expect(screen.getAllByText("Checking").length).toBeGreaterThan(0);
  expect(screen.getAllByText("Groceries").length).toBeGreaterThan(0);
  expect(screen.getByText("Rent")).toBeTruthy();
  expect(screen.getByText("Emergency fund")).toBeTruthy();
  expect(screen.getByText("Savings rate")).toBeTruthy();
  await waitFor(() =>
    expect(api.post).toHaveBeenCalledWith("/personal/goals/fund-due", {}),
  );
});

test("fold-in sections render: safe-to-spend, envelopes, methods, vaults", async () => {
  mockAuth.current = {
    user: { tenant: { kind: "household", name: "Home" }, role: "owner" },
    refresh: jest.fn(),
  };
  render(
    <MemoryRouter>
      <Household />
    </MemoryRouter>,
  );
  expect(await screen.findByTestId("household-safe-to-spend")).toBeTruthy();
  expect(screen.getByTestId("household-safe-to-spend").textContent).toContain("2,730");
  expect(screen.getByText(/Age of money: 40 days/)).toBeTruthy();
  expect(screen.getByText("Grocery pot")).toBeTruthy();
  expect(screen.getByText("Healthy")).toBeTruthy();
  expect(screen.getByText("Day-of-Month Stack")).toBeTruthy();
  expect(screen.getAllByText("Rainy day vault").length).toBeGreaterThan(0);
  expect(api.get).toHaveBeenCalledWith("/personal/envelopes");
  expect(api.get).toHaveBeenCalledWith("/personal/safe-to-spend");
  expect(api.get).toHaveBeenCalledWith("/personal/savings-methods");
  expect(api.get).toHaveBeenCalledWith("/personal/vaults");
});
