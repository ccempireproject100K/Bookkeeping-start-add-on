import { useEffect, useState, useCallback } from "react";
import client, { api, errMsg, downloadCsv } from "@/lib/api";
import { money } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { toast } from "sonner";
import { AlertTriangle, FileText, History, Plus, Sparkles } from "lucide-react";

const put = (p, b) => client.put(p, b).then((r) => r.data);
const LABEL = "AIA-style (G702/G703 format)";

function SummaryLines({ app }) {
  const row = (label, cents, bold) => (
    <div className={`flex justify-between py-0.5 ${bold ? "font-semibold text-gray-100" : "text-gray-300"}`}>
      <span>{label}</span><span className="font-mono">{money(cents)}</span>
    </div>
  );
  return (
    <div className="text-xs" data-testid="aia-summary">
      {row("1. Original contract sum", app.original_contract_cents)}
      {row("2. Net change by approved change orders", app.change_orders_cents)}
      {row("3. Contract sum to date", app.contract_sum_to_date_cents, true)}
      {row("4. Total completed & stored to date", app.total_completed_stored_cents)}
      {row("5. Retainage held", app.retainage_total_cents)}
      {row("6. Total earned less retainage", app.total_earned_less_retainage_cents, true)}
      {row("7. Less previous certificates", app.less_previous_certificates_cents)}
      {row("8. Current payment due", app.current_payment_due_cents, true)}
      {row("9. Balance to finish, incl. retainage", app.balance_to_finish_cents)}
    </div>
  );
}

function ContinuationTable({ app }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs" data-testid="aia-continuation">
        <thead><tr className="text-gray-500 text-left border-b border-[#222838]">
          <th className="py-1 pr-2">Item</th><th className="py-1 pr-2">Description</th>
          <th className="py-1 pr-2 text-right">Scheduled</th><th className="py-1 pr-2 text-right">Previous</th>
          <th className="py-1 pr-2 text-right">This period</th><th className="py-1 pr-2 text-right">Stored (new)</th>
          <th className="py-1 pr-2 text-right">Total</th><th className="py-1 pr-2 text-right">%</th>
          <th className="py-1 pr-2 text-right">Balance</th><th className="py-1 text-right">Retainage</th>
        </tr></thead>
        <tbody>
          {app.lines.map((r) => (
            <tr key={r.line_id} className="border-b border-[#171c26] text-gray-300">
              <td className="py-1 pr-2 font-mono">{r.item_no}</td>
              <td className="py-1 pr-2">{r.description}{r.phase_name ? <span className="text-gray-500"> • {r.phase_name}</span> : null}</td>
              <td className="py-1 pr-2 text-right font-mono">{money(r.scheduled_value_cents)}</td>
              <td className="py-1 pr-2 text-right font-mono">{money(r.previous_completed_cents)}</td>
              <td className="py-1 pr-2 text-right font-mono">{money(r.this_period_cents)}</td>
              <td className="py-1 pr-2 text-right font-mono">{money(r.stored_cents)}</td>
              <td className="py-1 pr-2 text-right font-mono">{money(r.total_completed_stored_cents)}</td>
              <td className="py-1 pr-2 text-right font-mono">{r.pct_complete}%</td>
              <td className="py-1 pr-2 text-right font-mono">{money(r.balance_to_finish_cents)}</td>
              <td className="py-1 text-right font-mono">{money(r.retainage_held_cents)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AiaBilling({ job, isOwner, onRefresh }) {
  const enabled = !!job.aia_enabled;
  const [sov, setSov] = useState(null);
  const [lines, setLines] = useState([]);
  const [periods, setPeriods] = useState([]);
  const [live, setLive] = useState(null);
  const [history, setHistory] = useState([]);
  const [entries, setEntries] = useState({});
  const [previewOpen, setPreviewOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!enabled) return;
    try {
      const [s, p, a, h] = await Promise.all([
        api.get(`/jobs/${job.id}/aia/sov`),
        api.get(`/jobs/${job.id}/aia/periods`),
        api.get(`/jobs/${job.id}/aia/application`),
        api.get(`/jobs/${job.id}/aia/history`),
      ]);
      setSov(s); setPeriods(p); setLive(a); setHistory(h);
      setLines((s.lines || []).map((l) => ({
        item_no: l.item_no, description: l.description,
        scheduled_value: (l.scheduled_value_cents / 100).toString(),
        phase_id: l.phase_id || "", retainage_pct: l.retainage_pct ?? "",
      })));
      const ent = {};
      (a.application?.lines || []).forEach((r) => {
        ent[r.line_id] = {
          this_period: r.this_period_cents ? (r.this_period_cents / 100).toString() : "",
          stored: r.stored_cents ? (r.stored_cents / 100).toString() : "",
        };
      });
      setEntries(ent);
    } catch (e) { /* panel stays quiet until enabled data exists */ }
  }, [enabled, job.id]);

  useEffect(() => { load(); }, [load]);

  const toggle = async (on) => {
    setBusy(true);
    try {
      const res = await put(`/jobs/${job.id}/aia/mode`, { enabled: on });
      toast.success(on ? (res.seeded ? "Progress billing on — schedule of values pre-filled from your estimate" : "Progress billing on") : "Progress billing off — history kept");
      onRefresh && onRefresh();
      setTimeout(load, 50);
    } catch (e) { toast.error(errMsg(e)); }
    setBusy(false);
  };

  const saveSov = async () => {
    try {
      const doc = await put(`/jobs/${job.id}/aia/sov`, {
        lines: lines.map((l) => ({
          item_no: l.item_no, description: l.description,
          scheduled_value: parseFloat(l.scheduled_value) || 0,
          phase_id: l.phase_id || null,
          retainage_pct: l.retainage_pct === "" ? null : parseFloat(l.retainage_pct),
        })),
      });
      setSov(doc);
      toast.success(doc.sum_check?.warning ? "Saved — note the sum warning below" : "Schedule of values saved");
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const seed = async () => {
    try {
      await api.post(`/jobs/${job.id}/aia/sov/seed`);
      toast.success("Schedule of values pre-filled from the estimate");
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const openPeriod = async () => {
    try {
      const p = await api.post(`/jobs/${job.id}/aia/periods`, {});
      toast.success(p.copied_from_application_no
        ? `Period ${p.period_no} opened — application #${p.copied_from_application_no} copied forward as the draft`
        : `Period ${p.period_no} opened`);
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const openP = periods.find((p) => p.status === "open");
  const app = live?.application;

  const saveEntries = async () => {
    if (!openP) return;
    try {
      const payload = {};
      Object.entries(entries).forEach(([lid, e]) => {
        payload[lid] = { this_period: parseFloat(e.this_period) || 0, stored: parseFloat(e.stored) || 0 };
      });
      await put(`/jobs/${job.id}/aia/periods/${openP.id}/entries`, { entries: payload });
      toast.success("Period figures saved — preview updated");
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const closePeriod = async () => {
    if (!openP) return;
    setBusy(true);
    try {
      const closed = await api.post(`/jobs/${job.id}/aia/periods/${openP.id}/close`);
      toast.success(closed.billing?.posted
        ? `Application #${closed.period_no} posted to the books as an invoice`
        : `Application #${closed.period_no} closed — nothing new to bill this period`);
      setPreviewOpen(false);
      load(); onRefresh && onRefresh();
    } catch (e) { toast.error(errMsg(e)); }
    setBusy(false);
  };

  const downloadPdf = (periodNo, periodId) =>
    downloadCsv(`/jobs/${job.id}/aia/periods/${periodId}/document`, `pay-app-${job.job_number}-${periodNo}.pdf`);

  if (!enabled) {
    return (
      <div className="mt-2 p-3 rounded-lg bg-[#0F1218] border border-[#222838] flex items-center justify-between" data-testid="aia-optin">
        <div className="text-xs text-gray-400">
          <span className="text-gray-200 font-medium">Progress billing — {LABEL}.</span>{" "}
          Optional, per job, off by default. Schedules of values and pay applications for commercial work.
        </div>
        {isOwner && <Button size="sm" disabled={busy} onClick={() => toggle(true)} data-testid="aia-enable" className="bg-amber-600 hover:bg-amber-500 text-black h-8 ml-3 shrink-0">Turn on</Button>}
      </div>
    );
  }

  return (
    <div className="mt-2 p-3 rounded-lg bg-[#0F1218] border border-[#222838]" data-testid="aia-panel">
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-xs uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
          <FileText size={13} /> Progress Billing — {LABEL}
        </h4>
        {isOwner && <Button size="sm" variant="ghost" disabled={busy} onClick={() => toggle(false)} data-testid="aia-disable" className="h-7 text-xs text-gray-500">Turn off</Button>}
      </div>
      <p className="text-[11px] text-gray-500 mb-3">Documents follow the G702/G703 structure and are labeled AIA-style — not official AIA forms. Closing a period records an invoice in the books; no payment is sent or moved.</p>

      {/* Schedule of values */}
      <div className="mb-1 flex items-center justify-between">
        <h5 className="text-[11px] uppercase tracking-wider text-gray-500">Schedule of Values</h5>
        {isOwner && lines.length === 0 && (
          <Button size="sm" variant="ghost" onClick={seed} data-testid="aia-seed" className="h-7 text-xs text-amber-400"><Sparkles size={12} className="mr-1" /> Pre-fill from estimate</Button>
        )}
      </div>
      {sov?.sum_check?.warning && (
        <div className="flex gap-2 items-start text-[11px] text-amber-300 bg-amber-500/10 border border-amber-500/20 rounded-md p-2 mb-2" data-testid="aia-sum-warning">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" /> {sov.sum_check.warning}
        </div>
      )}
      <table className="w-full text-xs mb-2" data-testid="aia-sov">
        <thead><tr className="text-gray-500 text-left">
          <th className="py-1 pr-2 w-16">Item</th><th className="py-1 pr-2">Description</th>
          <th className="py-1 pr-2 w-32 text-right">Scheduled value ($)</th>
          <th className="py-1 w-24 text-right">Retainage %</th>{isOwner && <th className="w-8" />}
        </tr></thead>
        <tbody>
          {lines.map((l, i) => (
            <tr key={i} className="border-b border-[#171c26]">
              <td className="py-1 pr-2"><Input disabled={!isOwner} value={l.item_no} onChange={(e) => setLines(lines.map((x, j) => j === i ? { ...x, item_no: e.target.value } : x))} className="bg-[#14171E] border-[#222838] h-7 text-xs font-mono" /></td>
              <td className="py-1 pr-2"><Input disabled={!isOwner} value={l.description} onChange={(e) => setLines(lines.map((x, j) => j === i ? { ...x, description: e.target.value } : x))} className="bg-[#14171E] border-[#222838] h-7 text-xs" /></td>
              <td className="py-1 pr-2"><Input disabled={!isOwner} value={l.scheduled_value} onChange={(e) => setLines(lines.map((x, j) => j === i ? { ...x, scheduled_value: e.target.value } : x))} className="bg-[#14171E] border-[#222838] h-7 text-xs font-mono text-right" /></td>
              <td className="py-1"><Input disabled={!isOwner} value={l.retainage_pct} placeholder="job" onChange={(e) => setLines(lines.map((x, j) => j === i ? { ...x, retainage_pct: e.target.value } : x))} className="bg-[#14171E] border-[#222838] h-7 text-xs font-mono text-right" /></td>
              {isOwner && <td><Button size="sm" variant="ghost" className="h-7 px-1 text-gray-500" onClick={() => setLines(lines.filter((_, j) => j !== i))}>×</Button></td>}
            </tr>
          ))}
        </tbody>
      </table>
      {isOwner && (
        <div className="flex gap-2 mb-3">
          <Button size="sm" variant="ghost" onClick={() => setLines([...lines, { item_no: `${lines.length + 1}.00`, description: "", scheduled_value: "", phase_id: "", retainage_pct: "" }])} className="h-7 text-xs text-gray-400"><Plus size={12} className="mr-1" /> Add line</Button>
          <Button size="sm" onClick={saveSov} data-testid="aia-sov-save" className="bg-amber-600 hover:bg-amber-500 text-black h-7 text-xs">Save schedule</Button>
        </div>
      )}

      {/* Open period working draft */}
      {app && (
        <div className="grid lg:grid-cols-[1fr_320px] gap-3">
          <div>
            <h5 className="text-[11px] uppercase tracking-wider text-gray-500 mb-1">
              {openP ? <>Application #{openP.period_no} — working draft{openP.copied_from_application_no ? <span className="normal-case text-gray-600"> (copied forward from #{openP.copied_from_application_no}; previous figures locked)</span> : null}</> : "Latest application"}
            </h5>
            {openP && isOwner ? (
              <table className="w-full text-xs" data-testid="aia-entries">
                <thead><tr className="text-gray-500 text-left">
                  <th className="py-1 pr-2">Line</th><th className="py-1 pr-2 text-right">Previous</th>
                  <th className="py-1 pr-2 w-28 text-right">This period ($)</th>
                  <th className="py-1 pr-2 w-28 text-right">Stored — new ($)</th>
                  <th className="py-1 text-right">Total / %</th>
                </tr></thead>
                <tbody>
                  {app.lines.map((r) => (
                    <tr key={r.line_id} className="border-b border-[#171c26] text-gray-300">
                      <td className="py-1 pr-2">{r.item_no} {r.description}</td>
                      <td className="py-1 pr-2 text-right font-mono">{money(r.previous_completed_cents)}</td>
                      <td className="py-1 pr-2"><Input value={entries[r.line_id]?.this_period ?? ""} onChange={(e) => setEntries({ ...entries, [r.line_id]: { ...entries[r.line_id], this_period: e.target.value } })} className="bg-[#14171E] border-[#222838] h-7 text-xs font-mono text-right" /></td>
                      <td className="py-1 pr-2"><Input value={entries[r.line_id]?.stored ?? ""} onChange={(e) => setEntries({ ...entries, [r.line_id]: { ...entries[r.line_id], stored: e.target.value } })} className="bg-[#14171E] border-[#222838] h-7 text-xs font-mono text-right" /></td>
                      <td className="py-1 text-right font-mono">{money(r.total_completed_stored_cents)} • {r.pct_complete}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <ContinuationTable app={app} />}
            {openP && isOwner && (
              <div className="flex gap-2 mt-2">
                <Button size="sm" onClick={saveEntries} className="bg-[#1B2030] hover:bg-[#232a3d] text-gray-200 h-8 text-xs">Save figures</Button>
                <Button size="sm" onClick={() => setPreviewOpen(true)} data-testid="aia-preview-open" className="bg-amber-600 hover:bg-amber-500 text-black h-8 text-xs">Preview application</Button>
              </div>
            )}
            {!openP && isOwner && lines.length > 0 && (
              <Button size="sm" onClick={openPeriod} data-testid="aia-open-period" className="bg-amber-600 hover:bg-amber-500 text-black h-8 text-xs mt-2">Open next billing period</Button>
            )}
          </div>
          <div className="p-2 rounded-md bg-[#0B0E13] border border-[#1B2030] h-fit">
            <h5 className="text-[11px] uppercase tracking-wider text-gray-500 mb-1">Application summary</h5>
            <SummaryLines app={app} />
          </div>
        </div>
      )}

      {/* History */}
      {history.length > 0 && (
        <div className="mt-3">
          <h5 className="text-[11px] uppercase tracking-wider text-gray-500 mb-1 flex items-center gap-1"><History size={12} /> Billing history</h5>
          {history.map((h) => (
            <details key={h.period_id} className="border-b border-[#171c26] py-1.5" data-testid="aia-history-row">
              <summary className="text-xs text-gray-300 cursor-pointer">
                Application #{h.period_no} — {h.label || "period"} • due {money(h.current_payment_due_cents)} • completed & stored {money(h.total_completed_stored_cents)}
                {h.invoice_id ? <span className="text-gray-500"> • posted as invoice</span> : null}
                <Button size="sm" variant="ghost" className="h-6 text-[11px] text-amber-400 ml-2" onClick={(e) => { e.preventDefault(); downloadPdf(h.period_no, h.period_id); }}>PDF</Button>
              </summary>
              <table className="w-full text-[11px] mt-1">
                <thead><tr className="text-gray-500 text-left">
                  <th className="py-0.5 pr-2">Line</th><th className="py-0.5 pr-2 text-right">Was</th>
                  <th className="py-0.5 pr-2 text-right">This period</th><th className="py-0.5 pr-2 text-right">Now</th>
                  <th className="py-0.5 text-right">Change</th>
                </tr></thead>
                <tbody>
                  {h.lines.map((l) => (
                    <tr key={l.line_id} className="text-gray-400 border-b border-[#12151d]">
                      <td className="py-0.5 pr-2">{l.item_no} {l.description}</td>
                      <td className="py-0.5 pr-2 text-right font-mono">{money(l.previous_total_cents)}</td>
                      <td className="py-0.5 pr-2 text-right font-mono">{money(l.this_period_cents)}</td>
                      <td className="py-0.5 pr-2 text-right font-mono">{money(l.total_cents)}</td>
                      <td className="py-0.5 text-right font-mono">{l.delta_total_cents > 0 ? "+" : ""}{money(l.delta_total_cents)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          ))}
        </div>
      )}

      {/* Preview before send */}
      <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
        <DialogContent className="bg-[#0F1218] border-[#222838] max-w-3xl max-h-[85vh] overflow-y-auto">
          <DialogHeader><DialogTitle className="text-gray-100 text-sm">Preview — Application #{openP?.period_no} ({LABEL})</DialogTitle></DialogHeader>
          {app && (
            <div className="space-y-3">
              <SummaryLines app={app} />
              <ContinuationTable app={app} />
              <p className="text-[11px] text-gray-500">This is exactly what posts: closing the period freezes these figures and records an invoice for the current payment due. Nothing is sent to the customer from here, and no payment moves.</p>
            </div>
          )}
          <DialogFooter>
            {openP && <Button size="sm" variant="ghost" onClick={() => downloadPdf(openP.period_no, openP.id)} className="text-gray-300">Download PDF</Button>}
            {isOwner && openP && <Button size="sm" disabled={busy} onClick={closePeriod} data-testid="aia-close-confirm" className="bg-emerald-600 hover:bg-emerald-500 text-black">Close period & post to books</Button>}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
