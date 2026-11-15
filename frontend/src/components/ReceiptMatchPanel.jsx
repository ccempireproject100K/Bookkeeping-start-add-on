import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { Link2, Unlink, RefreshCw, Flag } from "lucide-react";

/**
 * Receipt-match review queue (walk item #8): receipts the matcher could
 * not link quietly — a weak match, an amount that disagrees, a date too
 * far away, two equal candidates, or no bank twin at all. Each flag
 * carries a plain-words reason. One tap links the right bank transaction
 * or marks the receipt as standing alone (cash purchase, personal card).
 */
export default function ReceiptMatchPanel({ canManage, onResolved }) {
  const [items, setItems] = useState(null);
  const [stats, setStats] = useState(null);
  const [picks, setPicks] = useState({});   // itemId -> chosen txn id
  const [scanning, setScanning] = useState(false);

  const load = () => {
    api.get("/receipt-match/queue").then(setItems).catch(() => setItems([]));
    api.get("/receipt-match/stats").then(setStats).catch(() => setStats(null));
  };
  useEffect(load, []);

  const runScan = async () => {
    setScanning(true);
    try {
      const r = await api.post("/receipt-match/scan");
      toast.success(`Checked ${r.receipts_checked} receipt${r.receipts_checked === 1 ? "" : "s"} — ` +
        `${r.auto_linked} linked automatically, ${r.flags_open} flagged for your eye`);
      load();
      if (onResolved) onResolved();
    } catch (e) { toast.error(errMsg(e)); }
    setScanning(false);
  };

  const resolve = async (item, action, txnId) => {
    try {
      await api.post(`/receipt-match/queue/${item.id}/resolve`, {
        action, txn_id: txnId || undefined,
      });
      toast.success(action === "no_match"
        ? "Marked as standing alone — it won't be flagged again"
        : "Receipt linked to the bank transaction");
      load();
      if (onResolved) onResolved();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const link = (item) => {
    const chosen = picks[item.id] || item.best_txn_id || "";
    if (!chosen) { toast.error("Pick a bank transaction first, or mark it as no match"); return; }
    resolve(item, chosen === item.best_txn_id ? "confirm_link" : "pick_txn", chosen);
  };

  return (
    <div data-testid="receipt-match-panel">
      <Card className="mb-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Flag size={16} className="text-amber-400" />
            <h3 className="text-sm font-semibold text-gray-100">Receipt matches needing your eye</h3>
            {stats && (
              <span className="text-xs text-gray-500">
                {stats.pending} waiting · {stats.auto_linked} linked automatically · {stats.resolved_total} resolved
              </span>
            )}
          </div>
          {canManage && (
            <Button size="sm" variant="outline" onClick={runScan} disabled={scanning}
                    data-testid="receipt-match-scan">
              <RefreshCw size={13} className="mr-1" />{scanning ? "Matching…" : "Run matching"}
            </Button>
          )}
        </div>

        <div className="mt-4 space-y-3">
          {items === null && <p className="text-sm text-gray-500">Loading…</p>}
          {items && items.length === 0 && (
            <p className="text-sm text-gray-500">
              Nothing waiting. Receipts that match the bank feed link themselves;
              anything unsure lands here with the reason.
            </p>
          )}
          {(items || []).map((item) => {
            const chosen = picks[item.id] || item.best_txn_id || "";
            return (
              <div key={item.id} className="bg-[#0F1218] border border-[#222838] rounded-lg px-4 py-3"
                   data-testid={`receipt-flag-${item.id}`}>
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="text-sm text-gray-100">
                    {item.vendor || "Receipt"} · {fmtDate(item.receipt_date)} ·{" "}
                    <span className="font-mono">{money(item.amount_cents)}</span>
                  </span>
                  {item.best_score != null && (
                    <span className="text-xs text-gray-500">
                      best match {Math.round(item.best_score * 100)}%
                    </span>
                  )}
                </div>
                <p className="mt-1 text-xs text-amber-400/90">{item.reason}</p>
                {canManage && (
                  <div className="mt-3 flex flex-wrap items-end gap-2">
                    <div className="min-w-[260px] flex-1">
                      <Label className="text-xs">Bank transaction</Label>
                      <Select value={chosen || "none"}
                              onValueChange={(v) => setPicks({ ...picks, [item.id]: v === "none" ? "" : v })}>
                        <SelectTrigger className="bg-[#0F1218] border-[#222838]">
                          <SelectValue placeholder="Pick a transaction" />
                        </SelectTrigger>
                        <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100 max-h-72">
                          <SelectItem value="none">Pick a transaction…</SelectItem>
                          {(item.candidates || []).map((c) => (
                            <SelectItem key={c.id} value={c.id}>
                              {fmtDate(c.txn_date)} — {money(c.amount_cents)} — {c.description.slice(0, 34)} ({Math.round(c.score * 100)}%)
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <Button size="sm" className="bg-emerald-600 hover:bg-emerald-500 text-black"
                            onClick={() => link(item)} data-testid={`link-${item.id}`}>
                      <Link2 size={13} className="mr-1" />Link
                    </Button>
                    <Button size="sm" variant="ghost" className="text-gray-400"
                            onClick={() => resolve(item, "no_match")}
                            data-testid={`no-match-${item.id}`}>
                      <Unlink size={13} className="mr-1" />No match — stands alone
                    </Button>
                  </div>
                )}
                {!!(item.candidates || []).length && (
                  <p className="mt-2 text-xs text-gray-600">{item.candidates[0].reason}</p>
                )}
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}
