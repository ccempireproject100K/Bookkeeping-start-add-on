/**
 * PipDoor (Phase 1, Books receipts) — behaviour pins.
 *
 * Covered: sniff→card→stage→delivered flow through the REAL adapter
 * (against a recording mock client), explicit-scan gate, honesty states
 * (CSV signpost, backup danger, oversize), the one-question flow,
 * tenant isolation, button-mode parity, non-owner gate, failure+retry,
 * settings persistence, and the pose derivation (real states only).
 *
 * TextEncoder must exist before react-router-dom loads (the pristine
 * base's DataClean suites fail on exactly this) — hence require() for
 * the module under test after the polyfill.
 */
global.TextEncoder = require("util").TextEncoder;
global.TextDecoder = require("util").TextDecoder;

import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import i18n from "@/lib/i18n";
import { createTrayStore } from "./core/src/index.js";
import { createReceiptsAdapter } from "./receiptsAdapter";

const { PipDoor, pipPose, pipTrayStore } = require("./PipDoor");

const PNG_BYTES = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4]);
const PDF_BYTES = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x34, 10, 65, 66, 67]);

const pngFile = (name = "receipt.png") => new File([PNG_BYTES], name, { type: "image/png" });
const pdfFile = (name = "scan.pdf") => new File([PDF_BYTES], name, { type: "application/pdf" });

function setup({ tenantId = "tenant-a", canStage = true, postImpl } = {}) {
  const client = { post: jest.fn(postImpl || (async () => ({ id: "r-1", status: "uploaded" }))) };
  const adapter = createReceiptsAdapter(client);
  const store = createTrayStore();
  const navigate = jest.fn();
  const utils = render(
    <PipDoor tenantId={tenantId} canStage={canStage} navigate={navigate} adapter={adapter} store={store} />,
  );
  return { client, adapter, store, navigate, ...utils };
}

const dropFilesViaInput = (files) => {
  fireEvent.change(screen.getByTestId("pip-file-input"), { target: { files } });
};

const entryEl = (container, name) =>
  [...container.querySelectorAll("[data-testid^='pip-entry-']")].find((el) =>
    el.textContent.includes(name),
  );

beforeEach(async () => {
  window.localStorage.clear();
  await i18n.changeLanguage("en");
});

describe("receipt flow — sniff, card, stage, delivered, explicit scan", () => {
  test("a PNG becomes a ready receipt card; Send stages it; Scan fires ONLY on its button; nothing ever posts", async () => {
    const { client, container } = setup();
    dropFilesViaInput([pngFile()]);

    const entry = await waitFor(() => {
      const el = entryEl(container, "receipt.png");
      expect(el?.dataset.state).toBe("ready");
      return el;
    });
    expect(entry.textContent).toMatch(/Receipt Inbox/);
    // staging has NOT happened yet — the card waits for the human
    expect(client.post).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId(/pip-send-/));
    await waitFor(() => expect(entryEl(container, "receipt.png")?.dataset.state).toBe("delivered"));
    expect(client.post).toHaveBeenCalledTimes(1);
    expect(client.post.mock.calls[0][0]).toBe("/receipts/upload");
    expect(client.post.mock.calls[0][1]).toBeInstanceOf(FormData);

    // delivered is not scanned: no scan call until the explicit button
    expect(client.post).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId(/pip-scan-/));
    await waitFor(() => expect(client.post).toHaveBeenCalledTimes(2));
    expect(client.post.mock.calls[1]).toEqual(["/receipts/r-1/scan"]);

    // and under no circumstances did anything call the post endpoint
    for (const call of client.post.mock.calls) {
      expect(call[0]).not.toMatch(/\/post$/);
    }
  });

  test("the tray deep link navigates to the highlighted receipt", async () => {
    const { navigate, container } = setup();
    dropFilesViaInput([pngFile()]);
    await waitFor(() => expect(entryEl(container, "receipt.png")?.dataset.state).toBe("ready"));
    fireEvent.click(screen.getByTestId(/pip-send-/));
    await waitFor(() => expect(entryEl(container, "receipt.png")?.dataset.state).toBe("delivered"));
    fireEvent.click(screen.getByTestId(/pip-open-/));
    expect(navigate).toHaveBeenCalledWith("/receipts?receipt=r-1");
  });

  test("two receipts get a Send-all that stages each file separately, failures isolated", async () => {
    let n = 0;
    const { client, container } = setup({
      postImpl: async () => {
        n += 1;
        if (n === 1) throw new Error("network down");
        return { id: `r-${n}`, status: "uploaded" };
      },
    });
    dropFilesViaInput([pngFile("a.png"), pngFile("b.png")]);
    await waitFor(() => expect(screen.getByTestId("pip-send-all")).toBeTruthy());
    fireEvent.click(screen.getByTestId("pip-send-all"));
    await waitFor(() => expect(entryEl(container, "b.png")?.dataset.state).toBe("delivered"));
    expect(entryEl(container, "a.png")?.dataset.state).toBe("failed");
    const uploads = client.post.mock.calls.filter((c) => c[0] === "/receipts/upload");
    expect(uploads).toHaveLength(2); // one request per file, like the page
  });

  test("a failed upload can be retried from the tray and then delivers", async () => {
    let fail = true;
    const { container } = setup({
      postImpl: async () => {
        if (fail) throw new Error("boom");
        return { id: "r-9", status: "uploaded" };
      },
    });
    dropFilesViaInput([pngFile()]);
    await waitFor(() => expect(entryEl(container, "receipt.png")?.dataset.state).toBe("ready"));
    fireEvent.click(screen.getByTestId(/pip-send-/));
    await waitFor(() => expect(entryEl(container, "receipt.png")?.dataset.state).toBe("failed"));
    fail = false;
    fireEvent.click(screen.getByTestId(/pip-retry-/));
    await waitFor(() => expect(entryEl(container, "receipt.png")?.dataset.state).toBe("delivered"));
  });
});

describe("honesty states (§4.7) — nothing silently misroutes", () => {
  test("a CSV gets the signpost card to the Cleanup Console and is never staged", async () => {
    const { client, navigate, container } = setup();
    const csv = new File(["name,email\nAda,ada@example.com\n"], "customers.csv", { type: "text/csv" });
    dropFilesViaInput([csv]);
    const entry = await waitFor(() => {
      const el = entryEl(container, "customers.csv");
      expect(el?.dataset.state).toBe("unsupported");
      return el;
    });
    expect(client.post).not.toHaveBeenCalled();
    fireEvent.click(within(entry).getByTestId(/pip-signpost-/));
    expect(navigate).toHaveBeenCalledWith("/cleanup-console");
  });

  test("a backup JSON gets the danger wording and no handling", async () => {
    const { client, container } = setup();
    const backup = new File(
      [JSON.stringify({ format: "mae-backup", ciphertext: "x", iv: "y", salt: "z" })],
      "backup.json",
      { type: "application/json" },
    );
    dropFilesViaInput([backup]);
    const entry = await waitFor(() => {
      const el = entryEl(container, "backup.json");
      expect(el?.dataset.state).toBe("unsupported");
      return el;
    });
    expect(entry.textContent).toMatch(/admin-only/);
    expect(client.post).not.toHaveBeenCalled();
  });

  test("an oversize receipt is terminal with the destination's real limit quoted", async () => {
    const { container } = setup();
    const big = pngFile("huge.png");
    Object.defineProperty(big, "size", { value: 11 * 1024 * 1024 });
    dropFilesViaInput([big]);
    const entry = await waitFor(() => {
      const el = entryEl(container, "huge.png");
      expect(el?.dataset.state).toBe("unsupported");
      return el;
    });
    expect(entry.textContent).toMatch(/10\.0 MB/);
  });
});

describe("the one question (§3.2/§4.1)", () => {
  test("an ambiguous PDF asks once; answering 'A receipt' makes it ready", async () => {
    const { container } = setup();
    dropFilesViaInput([pdfFile()]);
    const entry = await waitFor(() => {
      const el = entryEl(container, "scan.pdf");
      expect(el?.dataset.state).toBe("needs-answer");
      return el;
    });
    fireEvent.click(within(entry).getByRole("button", { name: "A receipt" }));
    await waitFor(() => expect(entryEl(container, "scan.pdf")?.dataset.state).toBe("ready"));
  });

  test("answering 'Neither of these' closes as the honesty card — no retry, no staging", async () => {
    const { client, container } = setup();
    dropFilesViaInput([pdfFile()]);
    const entry = await waitFor(() => {
      const el = entryEl(container, "scan.pdf");
      expect(el?.dataset.state).toBe("needs-answer");
      return el;
    });
    fireEvent.click(within(entry).getByRole("button", { name: "Neither of these" }));
    await waitFor(() => {
      const el = entryEl(container, "scan.pdf");
      expect(el.textContent).toMatch(/Can't route this one/);
    });
    expect(screen.queryByTestId(/pip-retry-/)).toBeNull();
    expect(client.post).not.toHaveBeenCalled();
  });
});

describe("gates and modes", () => {
  test("a non-owner sees the owner-only note and no Send button", async () => {
    const { client, container } = setup({ canStage: false });
    dropFilesViaInput([pngFile()]);
    const entry = await waitFor(() => {
      const el = entryEl(container, "receipt.png");
      expect(el?.dataset.state).toBe("ready");
      return el;
    });
    expect(entry.textContent).toMatch(/owner-only/);
    expect(screen.queryByTestId(/pip-send-/)).toBeNull();
    expect(client.post).not.toHaveBeenCalled();
  });

  test("button mode keeps 100% of the flow (no walking strip)", async () => {
    window.localStorage.setItem("pip-button-mode", "yes");
    const { container } = setup();
    expect(screen.queryByTestId("pip-strip")).toBeNull();
    expect(screen.getByTestId("pip-add-files-btn")).toBeTruthy();
    dropFilesViaInput([pngFile()]);
    await waitFor(() => expect(entryEl(container, "receipt.png")?.dataset.state).toBe("ready"));
    fireEvent.click(screen.getByTestId(/pip-send-/));
    await waitFor(() => expect(entryEl(container, "receipt.png")?.dataset.state).toBe("delivered"));
  });

  test("speed + pause choices persist per user", async () => {
    setup();
    fireEvent.click(screen.getByTestId("pip-tray-toggle"));
    fireEvent.click(await screen.findByTestId("pip-speed-zoom"));
    expect(window.localStorage.getItem("pip-speed")).toBe("zoom");
    fireEvent.click(screen.getByTestId("pip-pause-toggle"));
    expect(window.localStorage.getItem("pip-paused")).toBe("yes");
  });

  test("reduced motion: Pip stands idle instead of walking", async () => {
    window.matchMedia = (q) => ({
      matches: q.includes("prefers-reduced-motion"),
      media: q,
      addEventListener: jest.fn(),
      removeEventListener: jest.fn(),
    });
    const { container } = setup();
    await waitFor(() => {
      expect(container.querySelector(".pip-char")?.getAttribute("data-state")).toBe("idle");
    });
    delete window.matchMedia;
  });
});

describe("tenant isolation (§5.6)", () => {
  test("tenant B sees none of tenant A's tray; A's entries stay A's", async () => {
    const shared = createTrayStore();
    const client = { post: jest.fn(async () => ({ id: "r-1", status: "uploaded" })) };
    const adapter = createReceiptsAdapter(client);
    const first = render(
      <PipDoor tenantId="tenant-a" canStage navigate={jest.fn()} adapter={adapter} store={shared} />,
    );
    dropFilesViaInput([pngFile()]);
    await waitFor(() => expect(entryEl(first.container, "receipt.png")?.dataset.state).toBe("ready"));
    first.unmount();

    const second = render(
      <PipDoor tenantId="tenant-b" canStage navigate={jest.fn()} adapter={adapter} store={shared} />,
    );
    fireEvent.click(screen.getByTestId("pip-tray-toggle"));
    expect(entryEl(second.container, "receipt.png")).toBeUndefined();
    expect(shared.list("tenant-a")).toHaveLength(1);
    expect(shared.list("tenant-b")).toHaveLength(0);
    // and the persisted histories are keyed per tenant
    expect(window.localStorage.getItem("pip-tray:tenant-b")).toBeNull();
  });
});

describe("pipPose — poses follow real states only", () => {
  const base = { dragOver: false, celebrating: false, anyStaging: false, anyQuestion: false, lastOutcome: null, sleeping: false, walking: true };
  test("priority order", () => {
    expect(pipPose(base)).toBe("walk");
    expect(pipPose({ ...base, walking: false })).toBe("idle");
    expect(pipPose({ ...base, sleeping: true, walking: false })).toBe("sleep");
    expect(pipPose({ ...base, lastOutcome: "failed" })).toBe("slumped");
    expect(pipPose({ ...base, anyQuestion: true })).toBe("thinking");
    expect(pipPose({ ...base, anyStaging: true })).toBe("carrying");
    expect(pipPose({ ...base, celebrating: true })).toBe("celebrate");
    expect(pipPose({ ...base, dragOver: true, celebrating: true })).toBe("excited");
  });
  test("the shell store singleton is the vendored tray store shape", () => {
    expect(typeof pipTrayStore.add).toBe("function");
    expect(typeof pipTrayStore.dispatch).toBe("function");
    expect(typeof pipTrayStore.wipe).toBe("function");
  });
});
