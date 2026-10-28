import { useEffect, useState } from "react";
import client, { api, errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";

// Empire referral payouts — the operator surface for the shared
// referral ledger (books-host B3). Three rails: the monthly Stripe
// Connect batch, manual recording (backup), and the Melio queue for
// bank-transfer (ACH) and paper-check members. Paper checks carry a
// $2.00 fee, deducted from the payout and shown on every number here.
const INPUT_CLS = "bg-[#0F1218] border-[#222838]";
const money = (c) => `$${((c || 0) / 100).toFixed(2)}`;
const PRODUCTS = ["trade_circle", "books", "momo"];
const NEXT_STATES = {
  queued: ["sent_to_melio", "failed"],
  sent_to_melio: ["awaiting_claim", "claimed", "completed", "failed"],
  awaiting_claim: ["claimed", "completed", "failed"],
  claimed: ["completed", "failed"],
};

export default function ReferralPayouts() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [balances, setBalances] = useState([]);
  const [queue, setQueue] = useState([]);
  const [loading, setLoading] = useState(true);
  const [period, setPeriod] = useState(() =>
    new Date().toISOString().slice(0, 7));
  const [note, setNote] = useState("");
  const [batchResult, setBatchResult] = useState(null);
  const [selected, setSelected] = useState({});
  const [lastBatchId, setLastBatchId] = useState(
    () => localStorage.getItem("melioBatchId") || "");
  const [busy, setBusy] = useState(false);

  const loadBalances = async () => {
    // The endpoint paginates (limit/offset over a global sort);
    // walk the pages so the table always shows every referrer.
    const PAGE = 200;
    const all = [];
    for (let offset = 0; ; offset += PAGE) {
      const page = await api.get(
        `/referrals/admin/balances?limit=${PAGE}&offset=${offset}`);
      all.push(...(page || []));
      if (!page || page.length < PAGE) break;
    }
    return all;
  };

  const load = async () => {
    setLoading(true);
    try {
      const [b, q] = await Promise.all([
        loadBalances(),
        api.get("/referrals/admin/queue"),
      ]);
      setBalances(b || []);
      setQueue(q || []);
    } catch (e) {
      toast.error(errMsg(e));
    }
    setLoading(false);
  };
  useEffect(() => { load(); }, []);

  const act = async (fn, okMsg) => {
    setBusy(true);
    try {
      await fn();
      if (okMsg) toast.success(okMsg);
      await load();
    } catch (e) {
      toast.error(errMsg(e));
    }
    setBusy(false);
  };

  const runStripeBatch = () =>
    act(async () => {
      const r = await api.post("/referrals/admin/payouts/batch", { period });
      setBatchResult(r);
    }, "Stripe batch finished");

  const recordManual = (row, product) =>
    act(async () => {
      if (!note.trim()) throw new Error("Add a note saying how it was paid.");
      await api.post("/referrals/admin/payouts/manual", {
        referrer_id: row.empire_id, product, note: note.trim(),
      });
    }, "Manual payout recorded");

  const queueForMelio = (row, product) =>
    act(async () => {
      await api.post("/referrals/admin/queue", {
        referrer_id: row.empire_id, product,
      });
    }, "Queued for Melio");

  const toggleSelect = (row, product) => {
    const key = `${row.empire_id}|${product}`;
    setSelected((s) => ({ ...s, [key]: !s[key] }));
  };

  const exportMelio = () =>
    act(async () => {
      const items = Object.keys(selected)
        .filter((k) => selected[k])
        .map((k) => {
          const [referrer_id, product] = k.split("|");
          return { referrer_id, product };
        });
      if (!items.length) throw new Error("Select at least one Melio payout.");
      const res = await client.post(
        "/referrals/admin/payouts/export", { period, items },
        { responseType: "blob" });
      const batchId = res.headers["x-pay-batch-id"];
      if (batchId) {
        setLastBatchId(batchId);
        localStorage.setItem("melioBatchId", batchId);
      }
      const url = window.URL.createObjectURL(new Blob([res.data]));
      const a = document.createElement("a");
      a.href = url;
      a.download = `melio-payouts-${period}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    }, "Melio batch file downloaded");

  const markBatchDone = () =>
    act(async () => {
      if (!lastBatchId) throw new Error("No exported batch to mark done.");
      const r = await api.post(
        `/referrals/admin/payouts/batches/${lastBatchId}/done`, {});
      toast.info(
        `Recorded ${r.recorded.length}, already recorded ` +
        `${r.already_recorded.length}, refused ${r.refused.length}`);
    }, "Batch marked done");

  const advance = (item, status) =>
    act(async () => {
      await api.post(`/referrals/admin/queue/${item.id}/advance`, { status });
    }, `Moved to ${status}`);

  if (!isOwner) {
    return (
      <div className="p-6">
        <PageHeader title="Referral Payouts" />
        <Card><p className="text-sm text-slate-400">
          Referral payouts are run by the workspace owner.
        </p></Card>
      </div>
    );
  }
  if (loading) return <Loading />;

  return (
    <div className="p-6 space-y-6">
      <PageHeader title="Referral Payouts"
        subtitle="One ledger for the whole Empire. Pots are per site and unlock at 3 paying referrals on that site." />

      <Card>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <Label>Batch period</Label>
            <Input className={INPUT_CLS} value={period}
              onChange={(e) => setPeriod(e.target.value)}
              placeholder="YYYY-MM" />
          </div>
          <div className="flex-1 min-w-[220px]">
            <Label>Manual payout note (how it was paid)</Label>
            <Input className={INPUT_CLS} value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="e.g. Zelle 10/24" />
          </div>
          <Button disabled={busy} onClick={runStripeBatch}>
            Run Stripe batch
          </Button>
          <Button disabled={busy} variant="outline" onClick={exportMelio}>
            Export Melio file
          </Button>
          <Button disabled={busy || !lastBatchId} variant="outline"
            onClick={markBatchDone}>
            Mark Melio batch done
          </Button>
        </div>
        {batchResult && (
          <p className="mt-3 text-sm text-slate-300">
            Stripe batch {batchResult.period}: paid {batchResult.paid.length},
            skipped {batchResult.skipped.length},
            failed {batchResult.failed.length}.
            {batchResult.skipped.map((s, i) => (
              <span key={i} className="block text-slate-400">
                Skipped {s.product}: {s.reason}
              </span>
            ))}
          </p>
        )}
      </Card>

      <Card>
        <h3 className="font-semibold mb-3">Balances by referrer</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-start text-slate-400">
                <th className="py-2">Referrer</th><th>Preference</th>
                <th>Site</th><th>Paying</th><th>Payable</th>
                <th>Locked</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {balances.flatMap((row) =>
                PRODUCTS.filter((p) => row.sites[p]).map((p) => {
                  const s = row.sites[p];
                  const melio = row.payout_preference !== "stripe";
                  const key = `${row.empire_id}|${p}`;
                  return (
                    <tr key={key} className="border-t border-[#222838]">
                      <td className="py-2">
                        {row.name || row.email || row.empire_id}
                        <span className="block text-xs text-slate-500">
                          {row.code}{row.frozen ? " · FROZEN" : ""}
                          {row.under_review ? " · UNDER REVIEW" : ""}
                        </span>
                      </td>
                      <td>{row.payout_preference}</td>
                      <td>{p}</td>
                      <td>{s.paying_referrals} / {s.unlock_at}</td>
                      <td>{money(s.payable)}</td>
                      <td>{s.locked ? money(s.locked_cents) : "—"}</td>
                      <td className="space-x-2 rtl:space-x-reverse whitespace-nowrap">
                        <Button size="sm" variant="outline" disabled={busy}
                          onClick={() => recordManual(row, p)}>
                          Record manual
                        </Button>
                        {melio && (
                          <>
                            <Button size="sm" variant="outline" disabled={busy}
                              onClick={() => queueForMelio(row, p)}>
                              Queue for Melio
                            </Button>
                            <label className="inline-flex items-center gap-1 text-xs">
                              <input type="checkbox" checked={!!selected[key]}
                                onChange={() => toggleSelect(row, p)} />
                              Export
                            </label>
                          </>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <Card>
        <h3 className="font-semibold mb-3">
          Melio queue — bank transfers (ACH) &amp; paper checks ($2.00 fee deducted)
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-start text-slate-400">
                <th className="py-2">Referrer</th><th>Site</th><th>Method</th>
                <th>Gross</th><th>Fee</th><th>Net</th><th>State</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {queue.map((item) => (
                <tr key={item.id} className="border-t border-[#222838]">
                  <td className="py-2">
                    {item.referrer_email || item.referrer_id}
                    <span className="block text-xs text-slate-500">{item.referrer_code}</span>
                  </td>
                  <td>{item.product}</td>
                  <td>{item.preference === "paper_check" ? "Paper check" : "ACH"}</td>
                  <td>{money(item.gross_cents)}</td>
                  <td>{item.fee_cents ? money(item.fee_cents) : "—"}</td>
                  <td>{money(item.net_cents)}</td>
                  <td>{item.status}</td>
                  <td className="space-x-2 rtl:space-x-reverse whitespace-nowrap">
                    {(NEXT_STATES[item.status] || []).map((st) => (
                      <Button key={st} size="sm" variant="outline" disabled={busy}
                        onClick={() => advance(item, st)}>
                        {st}
                      </Button>
                    ))}
                  </td>
                </tr>
              ))}
              {!queue.length && (
                <tr><td colSpan={8} className="py-3 text-slate-400">
                  Nothing in the Melio queue.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
