/**
 * DataCleanCheckout: the magical checkout page. Stripe itself is
 * mocked away (the embedded component and Stripe.js loader); what
 * these tests pin is the page's contract with OUR backend: it renders
 * the stamped catalog from /dataclean/billing/status with the
 * backend's sellable flags (Security + bundle visible but NOT
 * purchasable), shows the honest not-configured panel instead of a
 * fake form when billing is off, and treats the success return as
 * "Stripe confirmed — entitlement lands via webhook", polling the
 * status until the backend says the workspace is entitled.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import "@/lib/i18n";
import { api } from "@/lib/api";
import DataCleanCheckout from "./DataCleanCheckout";

jest.mock("@/lib/api", () => ({
  api: { post: jest.fn(), get: jest.fn() },
  errMsg: (e) => e?.response?.data?.detail?.message || "Request failed",
}));

jest.mock("@stripe/stripe-js", () => ({ loadStripe: jest.fn(async () => null) }));
jest.mock("@stripe/react-stripe-js", () => ({
  EmbeddedCheckoutProvider: ({ children }) => <div>{children}</div>,
  EmbeddedCheckout: () => <div data-testid="stripe-embedded" />,
}));
jest.mock("canvas-confetti", () => jest.fn());
jest.mock("lottie-web", () => ({
  loadAnimation: jest.fn(() => ({ destroy: jest.fn() })),
}));

const CATALOG = [
  { id: "dataclean", name: "Empire Digital Data Clean", amount_cents: 1299, display: "$12.99/mo", sellable: true, status: "live" },
  { id: "security", name: "Empire Security Suite", amount_cents: 1899, display: "$18.99/mo", sellable: false, status: "coming_soon" },
  { id: "bundle", name: "Empire Digital Data Clean + Empire Security Suite", amount_cents: 2799, display: "$27.99/mo", sellable: false, status: "coming_soon", parts_total_cents: 3198, savings_cents: 399 },
];

const status = (over = {}) => ({
  configured: true, billing_mode: "test", publishable_key: "",
  enforcement_on: false,
  billing: { entitled: false, status: null, product: null },
  catalog: CATALOG,
  ...over,
});

const renderPage = (entry = "/dataclean-checkout") => render(
  <MemoryRouter initialEntries={[entry]}>
    <DataCleanCheckout />
  </MemoryRouter>
);

beforeEach(() => { jest.clearAllMocks(); });

test("unconfigured billing shows the honest panel, never a fake form", async () => {
  api.get.mockResolvedValue(status({ configured: false }));
  renderPage();
  expect(await screen.findByTestId("checkout-not-configured")).toBeTruthy();
  expect(screen.queryByTestId("checkout-pay")).toBeNull();
  expect(screen.queryByTestId("checkout-plan-dataclean")).toBeNull();
});

test("plan cards render the stamped ladder; only DataClean is purchasable", async () => {
  api.get.mockResolvedValue(status());
  renderPage();
  expect(await screen.findByTestId("checkout-plan-dataclean")).toBeTruthy();
  expect(screen.getByTestId("checkout-plan-security")).toBeTruthy();
  const bundle = screen.getByTestId("checkout-plan-bundle");
  expect(bundle).toBeTruthy();
  // The animated savings ticker's settled facts ride along: parts
  // total struck through and the $3.99 saving badged.
  expect(bundle.textContent).toContain("$31.98");
  expect(bundle.textContent).toContain("Save $3.99");
  // Security and the bundle cannot be selected for purchase.
  expect(screen.getByTestId("checkout-select-security").disabled).toBe(true);
  expect(screen.getByTestId("checkout-select-bundle").disabled).toBe(true);
  expect(screen.getByTestId("checkout-select-dataclean").disabled).toBe(false);
  // No publishable key in this fixture -> the hosted fallback button.
  expect(screen.getByTestId("checkout-pay")).toBeTruthy();
});

test("an entitled workspace sees the already-on panel, not a buy form", async () => {
  api.get.mockResolvedValue(status({
    billing: { entitled: true, status: "active", product: "dataclean" },
  }));
  renderPage();
  expect(await screen.findByTestId("checkout-active")).toBeTruthy();
  expect(screen.queryByTestId("checkout-pay")).toBeNull();
});

test("success return celebrates and reports the live entitlement", async () => {
  api.get.mockResolvedValue(status({
    billing: { entitled: true, status: "active", product: "dataclean" },
  }));
  renderPage("/dataclean-checkout?checkout=success&session_id=cs_1");
  expect(await screen.findByTestId("checkout-success")).toBeTruthy();
  await waitFor(() => {
    expect(screen.getByTestId("checkout-success-active")).toBeTruthy();
  });
  // The self-built post-purchase step: the bundle teaser, honest copy.
  expect(screen.getByTestId("checkout-bundle-teaser").textContent).toContain("$3.99");
});

test("cancelled return is a gentle no-charge note", async () => {
  api.get.mockResolvedValue(status());
  renderPage("/dataclean-checkout?checkout=cancelled");
  expect(await screen.findByTestId("checkout-cancelled")).toBeTruthy();
});
