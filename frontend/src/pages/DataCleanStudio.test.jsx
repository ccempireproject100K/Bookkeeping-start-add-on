/**
 * DataClean Studio: the signed-in surface over the key-authenticated
 * DataClean API. The API layer is mocked here; the API itself (auth,
 * clean, dedupe, metering) is covered by
 * backend/tests/test_dataclean_phase3.py. What these tests pin is the
 * page's contract: it connects with the customer's own mdck_ key in an
 * X-API-Key header, posts csv_text to the v1 endpoints, and renders
 * the cleaned records + changes the API returns.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import "@/lib/i18n";
import { api } from "@/lib/api";
import { toast } from "sonner";
import DataCleanStudio from "./DataCleanStudio";

jest.mock("@/lib/api", () => ({
  api: { post: jest.fn(), get: jest.fn() },
  errMsg: (e) => e?.response?.data?.detail?.message || "Request failed",
}));

jest.mock("sonner", () => ({
  toast: { error: jest.fn(), success: jest.fn() },
}));

const KEY = "mdck_test_key_1234567890";
const USAGE = {
  key: { key_stub: "mdck_test_ke...7890", label: "Studio" },
  calls_recorded: 3, by_status: { ok: 3 }, by_endpoint: {}, rows_this_month: 12,
};

beforeEach(() => {
  jest.clearAllMocks();
  api.get.mockResolvedValue(USAGE);
});

const renderPage = () => render(
  <MemoryRouter>
    <DataCleanStudio />
  </MemoryRouter>
);

const connect = async () => {
  fireEvent.change(screen.getByTestId("studio-key-input"), { target: { value: KEY } });
  fireEvent.click(screen.getByTestId("studio-connect"));
  await screen.findByTestId("studio-connected");
};

test("connect: validates the key against GET /usage with the X-API-Key header", async () => {
  renderPage();
  await connect();
  expect(api.get).toHaveBeenCalledWith("/v1/dataclean/usage", {
    headers: { "X-API-Key": KEY },
  });
  expect(screen.getByTestId("studio-connected").textContent).toContain("mdck_test_ke...7890");
});

test("connect: a key without the mdck_ shape never leaves the browser", async () => {
  renderPage();
  fireEvent.change(screen.getByTestId("studio-key-input"), { target: { value: "maek_wrong_surface" } });
  fireEvent.click(screen.getByTestId("studio-connect"));
  expect(api.get).not.toHaveBeenCalled();
  expect(toast.error).toHaveBeenCalled();
});

test("clean: posts csv_text to cleanup/clean and renders records + changes", async () => {
  api.post.mockResolvedValue({
    kind: "customers", rows: 1, shape: "dataclean.result/1", api_version: "v1",
    records: [{ name: "Acme Supply", email: "ops@acme.example", phone: "+15125550134", address: "9 Oak Ave, Dallas, TX 75001" }],
    changes: [{ row: 2, field: "phone", before: "(512) 555-0134", after: "+15125550134", rule: "phone:libphonenumber-e164", confidence: 0.99 }],
    changes_by_rule: { "phone:libphonenumber-e164": 1 },
  });
  renderPage();
  await connect();
  fireEvent.change(screen.getByTestId("studio-csv"), {
    target: { value: "name,email,phone,address\nAcme Supply,ops@acme.example,(512) 555-0134,\"9 Oak Ave, Dallas, TX 75001\"" },
  });
  fireEvent.click(screen.getByTestId("studio-clean"));
  await screen.findByTestId("studio-clean-result");
  expect(api.post).toHaveBeenCalledWith(
    "/v1/dataclean/cleanup/clean",
    expect.objectContaining({ kind: "customers", csv_text: expect.stringContaining("Acme Supply") }),
    { headers: { "X-API-Key": KEY } });
  const table = screen.getByTestId("studio-records-table");
  expect(table.textContent).toContain("+15125550134");
  expect(screen.getByTestId("studio-clean-changes").textContent)
    .toContain("phone:libphonenumber-e164");
  expect(screen.getByTestId("studio-download")).toBeTruthy();
});

test("scan: dedupe runs over the cleaned records and shows the pair", async () => {
  api.post.mockImplementation((path) => {
    if (path.endsWith("/cleanup/clean")) {
      return Promise.resolve({
        kind: "customers", rows: 2,
        records: [
          { name: "Acme Supply Inc", email: "a@acme.example", phone: "+15125550111", address: "", name_key: "acme supply inc" },
          { name: "ACME SUPPLY, INC.", email: "a@acme.example", phone: "+15125550111", address: "", name_key: "acme supply" },
        ],
        changes: [], changes_by_rule: {},
      });
    }
    if (path.endsWith("/dedupe/scan")) {
      return Promise.resolve({
        records: 2, engine: "deterministic", engine_note: null,
        pairs: [{
          a_id: "row-1", b_id: "row-2", score: 0.93,
          evidence: { name_similarity: 0.9, email_match: true, phone_match: true, address_similarity: 0, why: "same email" },
        }],
      });
    }
    return Promise.reject(new Error(`unexpected ${path}`));
  });
  renderPage();
  await connect();
  fireEvent.change(screen.getByTestId("studio-csv"), {
    target: { value: "name,email,phone\nAcme Supply Inc,a@acme.example,1\nACME SUPPLY INC,a@acme.example,1" },
  });
  fireEvent.click(screen.getByTestId("studio-clean"));
  await screen.findByTestId("studio-clean-result");
  fireEvent.click(screen.getByTestId("studio-scan"));
  await screen.findByTestId("studio-pairs-result");
  expect(screen.getByTestId("studio-pair-0").textContent).toContain("Acme Supply Inc");
  expect(screen.getByTestId("studio-pair-0").textContent).toContain("93%");
  // The scan request carries only the flat kind fields: clean_row's
  // engine helper keys (name_key, address_parts) are projected out.
  const scanCall = api.post.mock.calls.find(([p]) => p.endsWith("/dedupe/scan"));
  expect(scanCall[1].records).toEqual([
    { name: "Acme Supply Inc", email: "a@acme.example", phone: "+15125550111" },
    { name: "ACME SUPPLY, INC.", email: "a@acme.example", phone: "+15125550111" },
  ]);
});

test("normalize: posts typed values and renders the engine's verdicts", async () => {
  api.post.mockResolvedValue({
    type: "phone", shape: "dataclean.result/1", api_version: "v1",
    results: [
      { original: "(512) 555-0134", normalized: "+15125550134", ok: true, rule: "phone:libphonenumber-e164", confidence: 0.99, notes: [] },
      { original: "not a phone", normalized: null, ok: false, rule: "phone:libphonenumber-e164", confidence: 0, notes: [] },
    ],
  });
  renderPage();
  await connect();
  fireEvent.change(screen.getByTestId("studio-norm-values"), {
    target: { value: "(512) 555-0134\nnot a phone" },
  });
  fireEvent.click(screen.getByTestId("studio-normalize"));
  await screen.findByTestId("studio-norm-results");
  expect(api.post).toHaveBeenCalledWith(
    "/v1/dataclean/normalize",
    { type: "phone", values: ["(512) 555-0134", "not a phone"] },
    { headers: { "X-API-Key": KEY } });
  expect(screen.getByTestId("studio-norm-results").textContent).toContain("+15125550134");
});
