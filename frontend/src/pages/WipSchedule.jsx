import { Fragment, useCallback, useEffect, useState } from "react";
import client, { api, errMsg } from "@/lib/api";
import { dollars } from "@/lib/format";
import { PageHeader, Card, Loading, Empty } from "@/components/Page";
import { AlertTriangle, Camera, Download, FileText, Scale } from "lucide-react";
import { toast } from "sonner";

const pct = (p) => (p === null || p === undefined ? "—" : `${p}%`);
const money = (v) => (v === null || v === undefined ? "—" : dollars(v));

export default function WipSchedule() {
  const [sched, setSched] = useState(null);
  const [snapshots, setSnapshots] = useState([]);
  const [viewId, setViewId] = useState("live");       // live | snapshot id
  const [compareBase, setCompareBase] = useState(""); // snapshot id
  const [compare, setCompare] = useState(null);
  const [period, setPeriod] = useState(
    () => new Date().toISOString().slice(0, 7));

  const loadSnapshots = useCallback(() => {
    api.get("/jobs/wip/snapshots").then(setSnapshots).catch(() => {});
  }, []);

  useEffect(() => {
    api.get("/jobs/wip").then(setSched).catch((e) => toast.error(errMsg(e)));
    loadSnapshots();
  }, [loadSnapshots]);

  useEffect(() => {
    if (viewId === "live") {
      api.get("/jobs/wip").then(setSched).catch(() => {});
    } else {
      api.get(`/jobs/wip/snapshots/${viewId}`).then(setSched)
        .catch((e) => toast.error(errMsg(e)));
    }
  }, [viewId]);

  useEffect(() => {
    if (!compareBase) { setCompare(null); return; }
    api.get(`/jobs/wip/compare?base=${compareBase}&against=${viewId}`)
      .then(setCompare).catch((e) => toast.error(errMsg(e)));
  }, [compareBase, viewId]);

  const takeSnapshot = async () => {
    try {
      await api.post("/jobs/wip/snapshots", { period });
      toast.success(`Snapshot saved for ${period}`);
      loadSnapshots();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const exportAs = async (format) => {
    const qs = viewId === "live" ? "" : `&snapshot_id=${viewId}`;
    try {
      const res = await client.get(`/jobs/wip/export?format=${format}${qs}`,
        { responseType: "blob" });
      const url = window.URL.createObjectURL(new Blob([res.data]));
      const a = document.createElement("a");
      a.href = url;
      a.download = `wip-${viewId === "live" ? "live" : period}.${format}`;
      document.body.appendChild(a); a.click(); a.remove();
      window.URL.revokeObjectURL(url);
    } catch (e) { toast.error(errMsg(e)); }
  };

  if (!sched) return <Loading />;
  const t = sched.totals || {};
  const viewing = viewId === "live" ? null
    : snapshots.find((s) => s.id === viewId);

  return (
    <div className="animate-rise">
      <PageHeader title="WIP Schedule" testid="wip-schedule-page"
        subtitle="Work in progress — percent complete, earned revenue, over/under billing. Live from your jobs, no month-end rebuild." />

      <div className="flex flex-wrap items-center gap-2 mb-5" data-testid="wip-actions">
        <select value={viewId} onChange={(e) => setViewId(e.target.value)}
          className="bg-[#12181f] border border-[#232c38] rounded-lg px-3 py-2 text-sm"
          data-testid="wip-view-select">
          <option value="live">Live schedule</option>
          {snapshots.map((s) => (
            <option key={s.id} value={s.id}>
              Snapshot — {s.label} ({new Date(s.taken_at).toLocaleDateString()})
            </option>
          ))}
        </select>
        <input value={period} onChange={(e) => setPeriod(e.target.value)}
          placeholder="YYYY-MM"
          className="bg-[#12181f] border border-[#232c38] rounded-lg px-3 py-2 text-sm w-28"
          data-testid="wip-period-input" />
        <button onClick={takeSnapshot}
          className="flex items-center gap-2 bg-amber-500/90 hover:bg-amber-500 text-black text-sm font-medium rounded-lg px-3 py-2"
          data-testid="wip-take-snapshot">
          <Camera size={15} /> Save month-end snapshot
        </button>
        <button onClick={() => exportAs("csv")}
          className="flex items-center gap-2 border border-[#232c38] hover:border-[#34404f] text-sm rounded-lg px-3 py-2"
          data-testid="wip-export-csv">
          <Download size={15} /> CSV
        </button>
        <button onClick={() => exportAs("pdf")}
          className="flex items-center gap-2 border border-[#232c38] hover:border-[#34404f] text-sm rounded-lg px-3 py-2"
          data-testid="wip-export-pdf">
          <FileText size={15} /> PDF
        </button>
        <select value={compareBase} onChange={(e) => setCompareBase(e.target.value)}
          className="bg-[#12181f] border border-[#232c38] rounded-lg px-3 py-2 text-sm"
          data-testid="wip-compare-select">
          <option value="">Compare against snapshot…</option>
          {snapshots.filter((s) => s.id !== viewId).map((s) => (
            <option key={s.id} value={s.id}>vs {s.label}</option>
          ))}
        </select>
      </div>

      {viewing && (
        <div className="text-xs text-gray-500 mb-3" data-testid="wip-frozen-note">
          Frozen snapshot — taken {new Date(viewing.taken_at).toLocaleString()}; later costs and invoices do not move it.
        </div>
      )}

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-5" data-testid="wip-kpis">
        {[
          { l: "Contract Value", v: money(t.contract) },
          { l: "Earned Revenue", v: money(t.earned_revenue) },
          { l: "Billed To Date", v: money(t.billed_to_date) },
          { l: "Over / (Under) Billed", v: money(t.over_under),
            c: t.over_under < 0 ? "text-amber-400" : "text-emerald-400" },
        ].map((k) => (
          <Card key={k.l}>
            <span className="text-xs uppercase tracking-wider text-gray-500">{k.l}</span>
            <div className={`mt-2 font-mono text-2xl font-semibold ${k.c || ""}`}>{k.v}</div>
          </Card>
        ))}
      </div>

      {sched.rows.length === 0 ? (
        <Empty>No open jobs yet — the WIP schedule fills in as jobs are created.</Empty>
      ) : (
        <Card className="overflow-x-auto" >
          <table className="w-full text-sm" data-testid="wip-table">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-gray-500 border-b border-[#232c38]">
                <th className="py-2 pr-3">Job</th>
                <th className="py-2 pr-3 text-right">Contract</th>
                <th className="py-2 pr-3 text-right">Costs</th>
                <th className="py-2 pr-3 text-right">% Done</th>
                <th className="py-2 pr-3 text-right">Earned</th>
                <th className="py-2 pr-3 text-right">Billed</th>
                <th className="py-2 text-right">Over/Under</th>
              </tr>
            </thead>
            <tbody>
              {sched.rows.map((r) => (
                <Fragment key={r.job_id}>
                  <tr
                    className={`border-b border-[#1b222c] ${r.billing_behind ? "bg-amber-500/5" : ""}`}
                    data-testid={`wip-row-${r.job_number}`}>
                    <td className="py-2 pr-3">
                      <span className="font-mono text-amber-400">{r.job_number}</span>{" "}
                      <span className="text-gray-300">{r.customer_name}</span>
                      {r.billing_behind && (
                        <span className="ml-2 inline-flex items-center gap-1 text-[11px] text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded px-1.5 py-0.5"
                          data-testid={`wip-flag-${r.job_number}`}>
                          <AlertTriangle size={11} /> work ahead of billing
                        </span>
                      )}
                      <div className="text-xs text-gray-500 mt-0.5 max-w-xl">{r.note}</div>
                    </td>
                    <td className="py-2 pr-3 text-right font-mono">{money(r.contract)}</td>
                    <td className="py-2 pr-3 text-right font-mono">{money(r.costs_to_date)}</td>
                    <td className="py-2 pr-3 text-right font-mono">{pct(r.pct_complete)}</td>
                    <td className="py-2 pr-3 text-right font-mono">{money(r.earned_revenue)}</td>
                    <td className="py-2 pr-3 text-right font-mono">{money(r.billed_to_date)}</td>
                    <td className={`py-2 text-right font-mono ${r.over_under < 0 ? "text-amber-400" : "text-emerald-400"}`}>
                      {money(r.over_under)}
                    </td>
                  </tr>
                  {(r.phases || []).map((p) => (
                    <tr key={`${r.job_id}-${p.phase_id}`}
                      className="border-b border-[#141a21] text-gray-400"
                      data-testid={`wip-phase-${r.job_number}-${p.name}`}>
                      <td className="py-1.5 pr-3 pl-8 text-xs">phase · {p.name}</td>
                      <td className="py-1.5 pr-3 text-right font-mono text-xs">{money(p.contract_share)}</td>
                      <td className="py-1.5 pr-3 text-right font-mono text-xs">{money(p.costs_to_date)}</td>
                      <td className="py-1.5 pr-3 text-right font-mono text-xs">{pct(p.pct_complete)}</td>
                      <td className="py-1.5 pr-3 text-right font-mono text-xs">{money(p.earned_revenue)}</td>
                      <td className="py-1.5 pr-3 text-right font-mono text-xs">—</td>
                      <td className="py-1.5 text-right font-mono text-xs">—</td>
                    </tr>
                  ))}
                </Fragment>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {compare && (
        <Card className="mt-5" >
          <h3 className="font-display font-semibold mb-3 flex items-center gap-2">
            <Scale size={16} /> Month over month — {viewId === "live" ? "live" : "snapshot"} vs {snapshots.find((s) => s.id === compareBase)?.label || "snapshot"}
          </h3>
          <div className="space-y-2" data-testid="wip-compare-rows">
            {compare.rows.map((r) => (
              <div key={r.job_id} className="flex flex-wrap items-center justify-between gap-2 py-2 px-3 rounded-lg bg-[#12181f] border border-[#232c38] text-sm">
                <span>
                  <span className="font-mono text-amber-400">{r.job_number}</span>{" "}
                  <span className="text-gray-300">{r.customer_name}</span>
                  {r.presence !== "both" && (
                    <span className="ml-2 text-[11px] text-gray-500">{r.presence === "new" ? "new since snapshot" : "no longer open"}</span>
                  )}
                  {r.billing_behind && (
                    <span className="ml-2 inline-flex items-center gap-1 text-[11px] text-amber-300">
                      <AlertTriangle size={11} /> work ahead of billing
                    </span>
                  )}
                </span>
                {r.presence === "both" && (
                  <span className="font-mono text-xs text-gray-400">
                    % done {r.pct_complete_delta === null || r.pct_complete_delta === undefined
                      ? "—" : `${r.pct_complete_delta >= 0 ? "+" : ""}${r.pct_complete_delta} pts`} ·
                    earned {r.earned_delta_cents >= 0 ? "+" : ""}{dollars((r.earned_delta_cents || 0) / 100)} ·
                    billed {r.billed_delta_cents >= 0 ? "+" : ""}{dollars((r.billed_delta_cents || 0) / 100)}
                  </span>
                )}
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
