/**
 * MarketLocationSection — the "Where are you located?" section
 * (owner ruling 2026-11-01). Pins: the owner's heading wording, a
 * stated reason for every face (choice / detected / US default /
 * explicit no-market — never a silent non-US face), and that picking
 * a market calls the parent's handler (which stores the choice).
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { api } from "@/lib/api";
import MarketLocationSection from "./MarketLocationSection";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
  errMsg: (e) => String(e),
}));

const MARKETS = [
  { code: "US", name: "United States", flag: "🇺🇸", colors: { accent: "#3c3b6e" }, languages: ["en"], currency: "USD" },
  { code: "GB", name: "United Kingdom", flag: "🇬🇧", colors: { accent: "#012169" }, languages: ["en"], currency: "GBP" },
  { code: "DE", name: "Germany", flag: "🇩🇪", colors: { accent: "#ffcc00" }, languages: ["de"], currency: "EUR" },
];

beforeEach(() => {
  api.get.mockReset();
  api.get.mockImplementation(() => Promise.resolve(MARKETS));
});

test("renders the owner's heading and the switcher", async () => {
  render(<MarketLocationSection code="US" source="default" onPick={() => {}} />);
  expect(screen.getByTestId("market-location-section")).toBeTruthy();
  expect(screen.getByText("Where are you located?")).toBeTruthy();
  const select = await screen.findByTestId("market-location-select");
  await waitFor(() => expect(select.options.length).toBe(4));
  expect([...select.options].map((o) => o.value)).toEqual(["", "US", "GB", "DE"]);
});

test("the US default is stated, not worn silently", () => {
  render(<MarketLocationSection code="US" source="default" onPick={() => {}} />);
  const note = screen.getByTestId("market-location-note");
  expect(note.textContent).toContain("United States edition");
  expect(note.textContent).toContain("default");
});

test("a detected non-US face says it was detected", async () => {
  render(<MarketLocationSection code="GB" source="detected" onPick={() => {}} />);
  const note = screen.getByTestId("market-location-note");
  await waitFor(() => expect(note.textContent).toContain("United Kingdom"));
  expect(note.textContent).toContain("detected your location");
  expect(note.textContent).toContain("United Kingdom");
});

test("a saved choice is named as the visitor's own", async () => {
  render(<MarketLocationSection code="DE" source="choice" onPick={() => {}} />);
  const note = screen.getByTestId("market-location-note");
  await waitFor(() => expect(note.textContent).toContain("Germany"));
  expect(note.textContent).toContain("Germany");
  expect(note.textContent).toContain("your saved choice");
});

test("an explicit no-market pick is explained too", () => {
  render(<MarketLocationSection code="" source="none" onPick={() => {}} />);
  const note = screen.getByTestId("market-location-note");
  expect(note.textContent).toContain("trade-only edition");
  expect(note.textContent).toContain("your saved choice");
});

test("picking a market calls onPick with its code", async () => {
  const onPick = jest.fn();
  render(<MarketLocationSection code="US" source="default" onPick={onPick} />);
  const select = await screen.findByTestId("market-location-select");
  fireEvent.change(select, { target: { value: "GB" } });
  expect(onPick).toHaveBeenCalledWith("GB");
});
