/**
 * books.receipts adapter — transport parity + gate integrity.
 *
 * Parity target: pages/Receipts.jsx, whose upload is
 *   const fd = new FormData(); fd.append("file", f);
 *   await api.post("/receipts/upload", fd);
 * per file, and whose scan is api.post(`/receipts/${id}/scan`).
 * The adapter must send byte-identical requests through the same client.
 */
import fs from "fs";
import path from "path";
import { createReceiptsAdapter } from "./receiptsAdapter";

const makeClient = () => ({ post: jest.fn() });

describe("books.receipts adapter — transport parity with the Receipts page", () => {
  test("stage() sends exactly one multipart request, field 'file', to /receipts/upload", async () => {
    const client = makeClient();
    client.post.mockResolvedValue({ id: "r-123", status: "uploaded" });
    const adapter = createReceiptsAdapter(client);
    const file = new File(["receipt-bytes"], "receipt.jpg", { type: "image/jpeg" });

    const ref = await adapter.stage(file);

    expect(client.post).toHaveBeenCalledTimes(1);
    const [url, body] = client.post.mock.calls[0];
    expect(url).toBe("/receipts/upload");
    expect(body).toBeInstanceOf(FormData);
    expect([...body.keys()]).toEqual(["file"]);
    expect(body.get("file")).toBe(file);
    expect(ref).toEqual({ id: "r-123", status: "uploaded" });
  });

  test("scan() mirrors the page's scan call exactly (no body)", async () => {
    const client = makeClient();
    client.post.mockResolvedValue({ vendor: "Acme", amount: 12.5 });
    const adapter = createReceiptsAdapter(client);

    await adapter.scan("r-123");

    expect(client.post).toHaveBeenCalledTimes(1);
    expect(client.post.mock.calls[0]).toEqual(["/receipts/r-123/scan"]);
  });

  test("deepLink() points at the Receipts page with the receipt highlighted", () => {
    const adapter = createReceiptsAdapter(makeClient());
    expect(adapter.deepLink({ id: "r-123" })).toBe("/receipts?receipt=r-123");
    expect(adapter.deepLink({ id: null })).toBe("/receipts");
    expect(adapter.deepLink(null)).toBe("/receipts");
  });
});

describe("gate integrity — posting is unreachable via Pip", () => {
  test("the adapter exposes no post/expense method at all", () => {
    const adapter = createReceiptsAdapter(makeClient());
    expect(Object.keys(adapter).sort()).toEqual(["deepLink", "id", "scan", "stage"]);
    expect(adapter.post).toBeUndefined();
    expect(adapter.postExpense).toBeUndefined();
  });

  test("no file under src/pip (outside tests + vendored core) references the receipts post endpoint", () => {
    const pipDir = __dirname;
    const files = [];
    const walk = (d) => {
      for (const name of fs.readdirSync(d)) {
        const p = path.join(d, name);
        if (fs.statSync(p).isDirectory()) {
          if (!p.includes(`${path.sep}core`)) walk(p);
        } else if (/\.(js|jsx)$/.test(name) && !/\.test\.(js|jsx)$/.test(name)) {
          files.push(p);
        }
      }
    };
    walk(pipDir);
    expect(files.length).toBeGreaterThan(0);
    for (const f of files) {
      const src = fs.readFileSync(f, "utf8");
      // the expense-posting route shape: /receipts/${...}/post or /receipts/<id>/post
      expect(src).not.toMatch(/receipts\/(\$\{[^}]+\}|[\w-]+)\/post/);
    }
  });
});
