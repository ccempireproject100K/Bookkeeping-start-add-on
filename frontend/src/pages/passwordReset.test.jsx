/**
 * Password-reset pages: request flow, reset flow, error states, and the
 * compact LanguageSwitcher the owner's empire ruling requires on every
 * public surface. The API layer is mocked; the backend lifecycle is
 * covered in backend/tests/test_password_reset.py.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import "@/lib/i18n";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { PASSWORD_RESET, RESET_PASSWORD } from "@/constants/testIds";
import ForgotPassword from "./ForgotPassword";
import ResetPassword from "./ResetPassword";

jest.mock("@/lib/api", () => ({
  api: { post: jest.fn(), get: jest.fn() },
  errMsg: (e) => e?.response?.data?.detail || "Request failed",
}));

jest.mock("sonner", () => ({
  toast: { error: jest.fn(), success: jest.fn() },
}));

beforeEach(() => {
  jest.clearAllMocks();
});

const switcherPresent = () => {
  // The compact switcher is a select with the empire's four options.
  const el = screen.getByTestId("language-switcher");
  return el && el.querySelectorAll("option").length === 4;
};

test("forgot-password: form + switcher render; submit shows the sent state", async () => {
  api.post.mockResolvedValue({ data: { ok: true } });
  render(
    <MemoryRouter>
      <ForgotPassword />
    </MemoryRouter>
  );
  expect(switcherPresent()).toBe(true);
  fireEvent.change(screen.getByTestId(PASSWORD_RESET.emailInput), {
    target: { value: "owner@shop.example.com" },
  });
  fireEvent.click(screen.getByTestId(PASSWORD_RESET.submitButton));
  await waitFor(() =>
    expect(api.post).toHaveBeenCalledWith("/auth/password-reset/request", {
      email: "owner@shop.example.com",
    })
  );
  expect(
    await screen.findByTestId(PASSWORD_RESET.sentMessage)
  ).toBeTruthy();
});

test("forgot-password: empty email is a toast, not a request", async () => {
  render(
    <MemoryRouter>
      <ForgotPassword />
    </MemoryRouter>
  );
  fireEvent.click(screen.getByTestId(PASSWORD_RESET.submitButton));
  await waitFor(() => expect(toast.error).toHaveBeenCalled());
  expect(api.post).not.toHaveBeenCalled();
});

test("forgot-password: a 429 surfaces as an error toast, no sent state", async () => {
  api.post.mockRejectedValue({
    response: { status: 429, data: { detail: "Too many reset requests." } },
  });
  render(
    <MemoryRouter>
      <ForgotPassword />
    </MemoryRouter>
  );
  fireEvent.change(screen.getByTestId(PASSWORD_RESET.emailInput), {
    target: { value: "owner@shop.example.com" },
  });
  fireEvent.click(screen.getByTestId(PASSWORD_RESET.submitButton));
  await waitFor(() =>
    expect(toast.error).toHaveBeenCalledWith("Too many reset requests.")
  );
  expect(screen.queryByTestId(PASSWORD_RESET.sentMessage)).toBeNull();
});

test("reset-password: valid token shows the form; success shows done", async () => {
  api.get.mockResolvedValue({ data: { valid: true } });
  api.post.mockResolvedValue({ data: { ok: true } });
  render(
    <MemoryRouter initialEntries={["/reset-password?token=abc123"]}>
      <ResetPassword />
    </MemoryRouter>
  );
  expect(await screen.findByTestId(RESET_PASSWORD.passwordInput)).toBeTruthy();
  expect(switcherPresent()).toBe(true);
  expect(api.get).toHaveBeenCalledWith("/auth/password-reset/peek/abc123");
  fireEvent.change(screen.getByTestId(RESET_PASSWORD.passwordInput), {
    target: { value: "new-password-456" },
  });
  fireEvent.change(screen.getByTestId(RESET_PASSWORD.confirmInput), {
    target: { value: "new-password-456" },
  });
  fireEvent.click(screen.getByTestId(RESET_PASSWORD.submitButton));
  await waitFor(() =>
    expect(api.post).toHaveBeenCalledWith("/auth/password-reset/confirm", {
      token: "abc123",
      password: "new-password-456",
    })
  );
  expect(await screen.findByTestId(RESET_PASSWORD.doneMessage)).toBeTruthy();
});

test("reset-password: mismatched passwords never reach the API", async () => {
  api.get.mockResolvedValue({ data: { valid: true } });
  render(
    <MemoryRouter initialEntries={["/reset-password?token=abc123"]}>
      <ResetPassword />
    </MemoryRouter>
  );
  expect(await screen.findByTestId(RESET_PASSWORD.passwordInput)).toBeTruthy();
  fireEvent.change(screen.getByTestId(RESET_PASSWORD.passwordInput), {
    target: { value: "new-password-456" },
  });
  fireEvent.change(screen.getByTestId(RESET_PASSWORD.confirmInput), {
    target: { value: "different-password-1" },
  });
  fireEvent.click(screen.getByTestId(RESET_PASSWORD.submitButton));
  await waitFor(() => expect(toast.error).toHaveBeenCalled());
  expect(api.post).not.toHaveBeenCalled();
});

test("reset-password: dead token lands on the invalid state with a way back", async () => {
  api.get.mockRejectedValue({
    response: { status: 400, data: { detail: "invalid" } },
  });
  render(
    <MemoryRouter initialEntries={["/reset-password?token=spent"]}>
      <ResetPassword />
    </MemoryRouter>
  );
  expect(
    await screen.findByTestId(RESET_PASSWORD.invalidMessage)
  ).toBeTruthy();
  expect(screen.getByTestId(RESET_PASSWORD.requestLink)).toBeTruthy();
  expect(screen.queryByTestId(RESET_PASSWORD.passwordInput)).toBeNull();
});

test("reset-password: no token in the URL is invalid without a peek call", async () => {
  render(
    <MemoryRouter initialEntries={["/reset-password"]}>
      <ResetPassword />
    </MemoryRouter>
  );
  expect(
    await screen.findByTestId(RESET_PASSWORD.invalidMessage)
  ).toBeTruthy();
  expect(api.get).not.toHaveBeenCalled();
});
