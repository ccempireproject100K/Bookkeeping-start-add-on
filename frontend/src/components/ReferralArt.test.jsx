/**
 * ReferralArt: the referral page's artwork follows the workspace's NICHE
 * face. The art ships as base64 text modules under src/referral-art/ and is
 * decoded to a blob URL at render time (the repo's Git channel cannot carry
 * binaries). A generic face, an unknown niche, or a missing pack renders
 * nothing.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import "@/lib/i18n";
import ReferralArt from "@/components/ReferralArt";

beforeEach(() => {
  URL.createObjectURL = jest.fn(() => "blob:referral-art");
  URL.revokeObjectURL = jest.fn();
});

test("renders the niche's artwork for a niche face", async () => {
  render(<ReferralArt pack={{ niche_id: "hvac", trade: "HVAC" }} />);
  const img = await screen.findByAltText("Illustration of a HVAC professional at work");
  expect(img.getAttribute("src")).toBe("blob:referral-art");
  expect(img.getAttribute("width")).toBe("1200");
  expect(img.getAttribute("height")).toBe("800");
});

test("renders nothing for the generic face (no niche)", () => {
  const { container } = render(<ReferralArt pack={{ niche_id: null, trade: null }} />);
  expect(container.querySelector("img")).toBeNull();
});

test("renders nothing for an unknown niche slug", async () => {
  const { container } = render(<ReferralArt pack={{ niche_id: "not_a_niche", trade: "Nope" }} />);
  await new Promise((r) => setTimeout(r, 50));
  expect(container.querySelector("img")).toBeNull();
});

test("renders nothing without a pack", () => {
  const { container } = render(<ReferralArt pack={null} />);
  expect(container.querySelector("img")).toBeNull();
});
