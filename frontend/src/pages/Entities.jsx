import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import PlanGate from "@/components/PlanGate";
import { api, errMsg } from "@/lib/api";
import { money } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { toast } from "sonner";
import { Building2, ChevronDown, ChevronRight } from "lucide-react";

/** Multi-entity: every business you own, plus one consolidated view.
 * Intercompany eliminations are a tenant setting, OFF by default:
 * off = today's flagged view; on = gross -> eliminations -> consolidated
 * for the group total only. Individual books never change. */
function EntitiesInner() {
  const { t } = useTranslation();
  const [con, setCon] = useState(null);
  const [view, setView] = useState(null); // "net" | "gross" (server-resolved)
  const [busy, setBusy] = useState(false);
  const [showDetail, setShowDetail] = useState(false);

  const load = useCallback(async (v) => {
    try {
      const data = await api.get(`/entities/consolidated${v ? `?view=${v}` : ""}`);
      setCon(data);
      setView(data.view);
    } catch (e) {
      toast.error(errMsg(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (!con) return <Loading />;

  const enabled = !!con.eliminations_enabled;
  const isNet = view === "net" && enabled;
  const gross = con.gross || {
    total_revenue_cents: con.total_revenue_cents,
    total_expense_cents: con.total_expense_cents,
    net_cents: con.net_cents,
    combined_cash_cents: con.combined_cash_cents,
  };
  const elimTotals = con.eliminations_totals || { revenue_cents: 0, expense_cents: 0, balance_cents: 0 };

  const toggleSetting = async () => {
    setBusy(true);
    try {
      const r = await api.put("/entities/eliminations-setting", { enabled: !enabled });
      toast.success(r.enabled
        ? "Eliminations on — matched intercompany deals now net out of the group total."
        : "Eliminations off — intercompany deals are flagged for review, nothing is netted.");
      await load();
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const switchView = async (v) => {
    if (v === "net" && !enabled) {
      toast.info("Turn eliminations on first — the clean group total only exists when the setting is on.");
      return;
    }
    setView(v);
    await load(v);
  };

  const elimFor = (label, grossCents, netCents) =>
    isNet ? grossCents - netCents : 0;

  return (
    <div className="space-y-6">
      <PageHeader title={t("entities.entities")} subtitle={`One login, ${con?.entity_count || 0} ${con?.entity_count === 1 ? "business" : "businesses"} — combined view.`} />

      {/* Eliminations setting: tenant-level, default OFF */}
      <Card className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="max-w-2xl">
            <div className="text-sm font-medium">Intercompany eliminations</div>
            <p className="text-xs text-gray-500 mt-1">
              {enabled
                ? "ON — deals between your own businesses that match on both sides net out of the group total below. Every elimination is listed, and each business's own books never change — keep it on for a clean group total, turn it off when a lender or accountant needs to see the intercompany deals gross."
                : "OFF (the default) — the group view flags intercompany deals for review and changes nothing, exactly as it always has. Turn it on when you want a clean group total: matched intercompany amounts net out of the totals only, never out of a business's own books."}
            </p>
          </div>
          <button
            onClick={toggleSetting}
            disabled={busy}
            className={`relative inline-flex h-7 w-12 shrink-0 items-center rounded-full transition-colors ${enabled ? "bg-emerald-500" : "bg-[#2a3040]"} ${busy ? "opacity-50" : ""}`}
            aria-pressed={enabled}
            title={enabled ? "Turn eliminations off" : "Turn eliminations on"}
          >
            <span className={`inline-block h-5 w-5 transform rounded-full bg-white transition-transform ${enabled ? "translate-x-6" : "translate-x-1"}`} />
          </button>
        </div>
        {enabled && (
          <div className="flex gap-2 mt-4">
            <button onClick={() => switchView("net")} className={`px-3 py-1.5 rounded text-xs font-medium ${isNet ? "bg-emerald-500/20 text-emerald-300" : "bg-[#1a2030] text-gray-400"}`}>Consolidated (clean)</button>
            <button onClick={() => switchView("gross")} className={`px-3 py-1.5 rounded text-xs font-medium ${!isNet ? "bg-emerald-500/20 text-emerald-300" : "bg-[#1a2030] text-gray-400"}`}>Gross (everything in)</button>
          </div>
        )}
      </Card>

      {/* Totals: gross -> eliminations -> consolidated */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Card className="p-4"><div className="text-xs text-gray-500">Revenue{isNet ? " (consolidated)" : ""}</div>
          <div className="text-2xl font-semibold text-emerald-400">{money(con?.total_revenue_cents || 0)}</div>
          {isNet && <div className="text-[11px] text-gray-500 mt-1">Gross {money(gross.total_revenue_cents)} − eliminations {money(elimTotals.revenue_cents)}</div>}</Card>
        <Card className="p-4"><div className="text-xs text-gray-500">Expenses{isNet ? " (consolidated)" : ""}</div>
          <div className="text-2xl font-semibold text-rose-400">{money(con?.total_expense_cents || 0)}</div>
          {isNet && <div className="text-[11px] text-gray-500 mt-1">Gross {money(gross.total_expense_cents)} − eliminations {money(elimTotals.expense_cents)}</div>}</Card>
        <Card className="p-4"><div className="text-xs text-gray-500">Net{isNet ? " (consolidated)" : ""}</div>
          <div className="text-2xl font-semibold">{money(con?.net_cents || 0)}</div>
          {isNet && <div className="text-[11px] text-gray-500 mt-1">Gross net {money(gross.net_cents)}</div>}</Card>
        <Card className="p-4"><div className="text-xs text-gray-500">Combined cash</div>
          <div className="text-2xl font-semibold text-blue-400">{money(con?.combined_cash_cents || 0)}</div>
          {isNet && <div className="text-[11px] text-gray-500 mt-1">Cash between your own businesses stays in the group — eliminations $0.00</div>}</Card>
      </div>

      {/* Per-entity table (never altered by eliminations) */}
      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead><tr className="text-start text-gray-500 border-b border-[#222838]">
            <th className="px-4 py-3 font-medium">Business</th><th className="px-4 py-3 font-medium text-end">Revenue</th>
            <th className="px-4 py-3 font-medium text-end">Net</th><th className="px-4 py-3 font-medium text-end">Cash</th>
          </tr></thead>
          <tbody>
            {(con?.entities || []).map((e) => (
              <tr key={e.tenant_id} className="border-b border-[#1a2030]">
                <td className="px-4 py-3"><div className="flex items-center gap-2"><Building2 size={14} className="text-gray-500" /><span className="font-medium">{e.name}</span></div></td>
                <td className="px-4 py-3 text-end font-mono">{money(e.revenue_cents)}</td>
                <td className={`px-4 py-3 text-end font-mono ${e.net_cents >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{money(e.net_cents)}</td>
                <td className="px-4 py-3 text-end font-mono">{money(e.cash_cents)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      {/* P&L lines: three columns when net */}
      {(con?.lines || []).length > 0 && (
        <Card className="p-0 overflow-hidden">
          <div className="px-4 py-3 text-sm font-medium border-b border-[#222838]">Group profit &amp; loss</div>
          <table className="w-full text-sm">
            <thead><tr className="text-start text-gray-500 border-b border-[#222838]">
              <th className="px-4 py-2 font-medium">Account</th>
              {isNet && <th className="px-4 py-2 font-medium text-end">Entity total (gross)</th>}
              {isNet && <th className="px-4 py-2 font-medium text-end">Eliminations</th>}
              {isNet && <th className="px-4 py-2 font-medium text-end">Consolidated</th>}
              {!isNet && <th className="px-4 py-2 font-medium text-end">Amount</th>}
            </tr></thead>
            <tbody>
              {(con.lines || []).map((l) => (
                <tr key={l.code} className="border-b border-[#1a2030]">
                  <td className="px-4 py-2 text-gray-300">{l.code} · {l.name}</td>
                  {isNet && <td className="px-4 py-2 text-end font-mono">{money(l.gross_cents ?? l.amount_cents)}</td>}
                  {isNet && <td className="px-4 py-2 text-end font-mono text-amber-300">{l.eliminations_cents ? `−${money(l.eliminations_cents)}` : "—"}</td>}
                  {isNet && <td className="px-4 py-2 text-end font-mono font-medium">{money(l.consolidated_cents)}</td>}
                  {!isNet && <td className="px-4 py-2 text-end font-mono">{money(l.amount_cents)}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {/* Balance rows: same three columns (net only) */}
      {isNet && (con?.balances || []).length > 0 && (
        <Card className="p-0 overflow-hidden">
          <div className="px-4 py-3 text-sm font-medium border-b border-[#222838]">Group balances</div>
          <table className="w-full text-sm">
            <thead><tr className="text-start text-gray-500 border-b border-[#222838]">
              <th className="px-4 py-2 font-medium">Account</th>
              <th className="px-4 py-2 font-medium text-end">Entity total (gross)</th>
              <th className="px-4 py-2 font-medium text-end">Eliminations</th>
              <th className="px-4 py-2 font-medium text-end">Consolidated</th>
            </tr></thead>
            <tbody>
              {(con.balances || []).map((r) => (
                <tr key={r.code} className="border-b border-[#1a2030]">
                  <td className="px-4 py-2 text-gray-300">{r.code} · {r.name}</td>
                  <td className="px-4 py-2 text-end font-mono">{money(r.gross_cents)}</td>
                  <td className="px-4 py-2 text-end font-mono text-amber-300">{r.eliminations_cents ? `−${money(r.eliminations_cents)}` : "—"}</td>
                  <td className="px-4 py-2 text-end font-mono font-medium">{money(r.consolidated_cents)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="px-4 py-2 text-[11px] text-gray-600">{con.cash?.note}</p>
        </Card>
      )}

      {/* Eliminations detail: available in BOTH states */}
      {(con?.eliminations_detail || []).length > 0 && (
        <Card className="p-5">
          <button className="flex items-center gap-2 text-sm font-medium" onClick={() => setShowDetail(!showDetail)}>
            {showDetail ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            Eliminations detail ({con.eliminations_detail.length})
          </button>
          <p className="text-xs text-gray-500 mt-1">
            {isNet
              ? "Every amount taken out of the group total above, and why. Nothing here touches a business's own books."
              : enabled
                ? "You are looking at the gross view, so none of this is taken out of the totals above — it is exactly what the Consolidated view nets out."
                : "Eliminations are OFF, so none of this is taken out of the totals — this is what would net out if you turned the setting on."}
          </p>
          {showDetail && (
            <div className="mt-3 space-y-1">
              {con.eliminations_detail.map((d, i) => (
                <div key={i} className="text-xs py-1.5 border-b border-[#1a2030]">
                  <div className="flex justify-between gap-3">
                    <span className="text-gray-300">
                      {d.entity_a_name}: {d.account_a_code} · {d.account_a_name}
                      {d.account_b_code ? <> ↔ {d.entity_b_name}: {d.account_b_code} · {d.account_b_name}</> : <> → {d.entity_b_name}</>}
                    </span>
                    <span className="font-mono">{d.kind === "cash_transfer" ? money(d.amount_cents) : `−${money(d.eliminated_cents)}`}</span>
                  </div>
                  {(d.entries_a?.length > 0 || d.entries_b?.length > 0) && (
                    <div className="text-gray-600 mt-0.5">Entries: {[...(d.entries_a || []), ...(d.entries_b || [])].join(", ")}</div>
                  )}
                  {d.note && <div className="text-gray-500 mt-0.5">{d.note}</div>}
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

      {/* Unmatched intercompany: listed in BOTH states, never dropped */}
      {(con?.unmatched_intercompany || []).length > 0 && (
        <Card className="p-5 border-amber-500/30">
          <div className="text-sm font-medium mb-2 text-amber-400">Unmatched intercompany ({con.unmatched_intercompany.length})</div>
          <p className="text-xs text-gray-500 mb-2">Flagged as intercompany but the other side could not be paired — these are never force-matched and never eliminated. Usually the sister business hasn't recorded (or flagged) its side yet.</p>
          {con.unmatched_intercompany.map((u, i) => (
            <div key={i} className="text-xs py-1.5 border-b border-[#1a2030]">
              <div className="flex justify-between gap-3">
                <span className="text-gray-300">{u.entity_name}: {u.account_code} · {u.account_name}{u.counterparty_name ? ` → ${u.counterparty_name}` : ""}</span>
                <span className="font-mono">{money(u.amount_cents)}</span>
              </div>
              <div className="text-gray-500 mt-0.5">{u.reason}</div>
            </div>
          ))}
        </Card>
      )}

      {/* Legacy flagged-review card (the OFF-state view, as before) */}
      {!enabled && (con?.intercompany_review || []).length > 0 && (
        <Card className="p-5 border-amber-500/30">
          <div className="text-sm font-medium mb-2 text-amber-400">Intercompany review</div>
          <p className="text-xs text-gray-500 mb-2">These accounts look intercompany — review before reporting, not auto-eliminated.</p>
          {(con.intercompany_review || []).map((l) => (
            <div key={l.code} className="flex justify-between text-xs py-1 border-b border-[#1a2030]">
              <span className="text-gray-400">{l.code} · {l.name}</span>
              <span className="font-mono">{money(l.amount_cents)}</span>
            </div>
          ))}
        </Card>
      )}

      <p className="text-[11px] text-gray-600">{con.note} Consolidation is read-only — each entity's books stay separate. Only entities you own are included.</p>
    </div>
  );
}

export default function Entities() {
  return <PlanGate feature="multi_entity"><EntitiesInner /></PlanGate>;
}
