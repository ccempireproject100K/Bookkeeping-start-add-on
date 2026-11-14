import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import { Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { Receipt, Eye, X } from "lucide-react";

const IN = "bg-[#0F1218] border-[#222838]";
const money = (n) => `$${(n ?? 0).toFixed(2)}`;
const SOURCE_LABEL = {
  entry: "entry rate",
  job: "job's usual rate",
  tenant: "tenant default",
  override: "set in preview",
  mixed: "mixed rates",
};

/** "Bill this time" — approved billable time -> a draft invoice, gated by a
 *  line-by-line preview the user must confirm. Nothing converts without it. */
export default function BillTimePanel() {
  const [view, setView] = useState(null);
  const [defaultRate, setDefaultRate] = useState("");
  const [selected, setSelected] = useState(() => new Set());
  const [grouping, setGrouping] = useState("entry");
  const [preview, setPreview] = useState(null);
  const [overrides, setOverrides] = useState({});   // entry_id -> rate string
  const [drafts, setDrafts] = useState([]);
  const [rateInputs, setRateInputs] = useState({}); // entry_id -> rate string (needs-rate fix)
  const [busy, setBusy] = useState(false);

  const load = () => {
    api.get("/time-billing/unbilled").then((v) => {
      setView(v);
      setDefaultRate(v.default_billing_rate_cents ? String(v.default_billing_rate_cents / 100) : "");
    }).catch(() => setView({ groups: [], entries: [] }));
    api.get("/invoices").then((rows) =>
      setDrafts((rows || []).filter((i) => i.source === "time_billing" && i.status === "draft"))
    ).catch(() => setDrafts([]));
  };
  useEffect(load, []);

  const entryById = Object.fromEntries((view?.entries || []).map((e) => [e.id, e]));
  const selEntries = [...selected].map((id) => entryById[id]).filter(Boolean);
  const selClients = new Set(selEntries.map((e) => e.client_name));
  const multiClient = selClients.size > 1;

  const toggle = (id) => {
    setPreview(null);
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  };
  const toggleGroup = (entries) => {
    setPreview(null);
    const ids = entries.map((e) => e.id);
    const all = ids.every((id) => selected.has(id));
    setSelected((s) => {
      const n = new Set(s);
      ids.forEach((id) => (all ? n.delete(id) : n.add(id)));
      return n;
    });
  };

  const saveDefaultRate = async () => {
    try {
      const v = defaultRate === "" ? null : parseFloat(defaultRate);
      await api.put("/time-billing/settings", { default_billing_rate: v });
      toast.success(v ? `Default billing rate set to ${money(v)}/hr` : "Default billing rate cleared");
      setPreview(null);
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const setEntryRate = async (entry) => {
    const v = parseFloat(rateInputs[entry.id]);
    if (!v) return;
    try {
      await api.patch(`/time/entries/${entry.id}`, { billable_rate: v });
      toast.success(`Billing rate set to ${money(v)}/hr`);
      setPreview(null);
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const overrideFloats = () => {
    const out = {};
    Object.entries(overrides).forEach(([id, s]) => {
      const v = parseFloat(s);
      if (v > 0) out[id] = v;
    });
    return out;
  };

  const runPreview = async (ovr) => {
    setBusy(true);
    try {
      const p = await api.post("/time-billing/preview", {
        entry_ids: [...selected], grouping, rate_overrides: ovr ?? overrideFloats(),
      });
      setPreview(p);
    } catch (e) { toast.error(errMsg(e)); }
    setBusy(false);
  };

  const editLineRate = (line, value) => {
    const ovr = { ...overrides };
    line.time_entry_ids.forEach((id) => { ovr[id] = value; });
    setOverrides(ovr);
  };
  const refreshPreview = () => runPreview();

  const convert = async () => {
    if (!preview?.fingerprint) return;
    setBusy(true);
    try {
      const inv = await api.post("/time-billing/convert", {
        entry_ids: [...selected], grouping,
        rate_overrides: overrideFloats(), fingerprint: preview.fingerprint,
      });
      toast.success(`Draft invoice created for ${inv.client_name} — ${money(inv.total)} (see Invoices)`);
      setSelected(new Set());
      setPreview(null);
      setOverrides({});
      load();
    } catch (e) { toast.error(errMsg(e)); }
    setBusy(false);
  };

  const issueDraft = async (inv) => {
    try {
      await api.post(`/time-billing/invoices/${inv.id}/issue`, {});
      toast.success("Invoice issued — its time now stays billed");
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };
  const voidDraft = async (inv) => {
    try {
      const r = await api.post(`/time-billing/invoices/${inv.id}/void`, {});
      toast.success(`Draft voided — ${r.freed_entries} entr${r.freed_entries === 1 ? "y" : "ies"} freed back to unbilled`);
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  if (!view) return null;

  return (
    <Card className="mt-6" data-testid="bill-time-panel">
      <div className="flex items-center justify-between mb-1">
        <div className="text-xs uppercase tracking-wider text-gray-500 flex items-center gap-2">
          <Receipt size={14} /> Bill this time — unbilled approved hours
        </div>
        <div className="text-sm font-mono text-gray-300">
          {view.total_hours}h · {money((view.total_amount_cents || 0) / 100)}
          {view.needs_rate_count > 0 && <span className="text-rose-400"> · {view.needs_rate_count} need a rate</span>}
        </div>
      </div>
      <p className="text-xs text-gray-600 mb-4">
        Billing rate order: the entry's own rate → the job's remembered rate → the tenant default.
        The pay rate is a cost figure and is never billed. Nothing converts until you confirm the preview.
      </p>

      <div className="flex items-end gap-2 mb-4">
        <div className="w-52">
          <Label className="text-xs">Tenant default billing rate $/hr</Label>
          <Input type="number" step="0.01" min="0" value={defaultRate}
            onChange={(e) => setDefaultRate(e.target.value)} className={`${IN} font-mono`}
            placeholder="not set" />
        </div>
        <Button size="sm" variant="outline" onClick={saveDefaultRate}>Save default</Button>
      </div>

      {view.groups.length === 0 && <p className="text-sm text-gray-500 py-4 text-center">No approved billable time waiting to be billed.</p>}

      {view.groups.map((g) => (
        <div key={g.client_name || "none"} className="mb-5">
          <div className="flex items-center gap-3 mb-2">
            <input type="checkbox" className="accent-amber-500"
              checked={g.entries.every((e) => selected.has(e.id))}
              onChange={() => toggleGroup(g.entries)} />
            <h4 className="font-semibold text-gray-100">{g.client_name || "(no client)"}</h4>
            <span className="text-xs text-gray-500 font-mono">{g.hours}h · {money(g.amount_cents / 100)}</span>
            {g.needs_rate_count > 0 && <span className="text-xs text-rose-400">{g.needs_rate_count} need a rate</span>}
          </div>
          <div className="border border-[#222838] rounded-md overflow-hidden">
            <table className="w-full text-sm">
              <tbody>
                {g.entries.map((e) => (
                  <tr key={e.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-3 py-2 w-8">
                      <input type="checkbox" className="accent-amber-500"
                        checked={selected.has(e.id)} onChange={() => toggle(e.id)} />
                    </td>
                    <td className="px-3 py-2 text-gray-300 whitespace-nowrap">{fmtDate(e.date)}</td>
                    <td className="px-3 py-2 text-gray-400">{e.user_name}</td>
                    <td className="px-3 py-2 text-gray-400">{e.job_label || "—"}</td>
                    <td className="px-3 py-2 text-gray-500 text-xs max-w-[220px] truncate">{e.notes || "—"}</td>
                    <td className="px-3 py-2 text-right font-mono text-gray-200">{e.hours}h</td>
                    <td className="px-3 py-2 text-right whitespace-nowrap">
                      {e.needs_rate ? (
                        <span className="inline-flex items-center gap-1 justify-end">
                          <span className="text-rose-400 text-xs font-semibold">NEEDS RATE</span>
                          <Input type="number" step="0.01" min="0" placeholder="$/hr"
                            value={rateInputs[e.id] || ""}
                            onChange={(ev) => setRateInputs({ ...rateInputs, [e.id]: ev.target.value })}
                            className={`${IN} font-mono w-20 h-7 text-xs`} />
                          <Button size="sm" className="h-7" variant="outline" onClick={() => setEntryRate(e)}>Set</Button>
                        </span>
                      ) : (
                        <span className="font-mono text-gray-200">
                          {money(e.rate_cents / 100)}/hr
                          <span className="block text-[10px] text-gray-500 font-sans">{SOURCE_LABEL[e.rate_source] || e.rate_source}</span>
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-gray-100">{e.needs_rate ? "—" : money(e.amount_cents / 100)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      <div className="flex items-center gap-3 mt-2">
        <div>
          <Label className="text-xs">Invoice lines</Label>
          <select value={grouping} onChange={(e) => { setGrouping(e.target.value); setPreview(null); }}
            className={`${IN} w-full rounded-md px-2 py-2 text-sm text-gray-200`}>
            <option value="entry">One line per entry</option>
            <option value="day">Rolled up per day</option>
            <option value="job">Rolled up per job</option>
          </select>
        </div>
        <Button onClick={() => runPreview()} disabled={selected.size === 0 || busy}
          className="bg-sky-600 hover:bg-sky-500 text-black font-semibold mt-4">
          <Eye size={14} className="mr-1" /> Preview invoice ({selected.size} selected)
        </Button>
        {multiClient && <span className="text-xs text-rose-400 mt-4">Selection spans {selClients.size} clients — one client per invoice.</span>}
      </div>

      {preview && (
        <div className="mt-5 border border-sky-500/30 rounded-md p-4 bg-sky-500/5" data-testid="bill-time-preview">
          <div className="flex items-center justify-between mb-3">
            <h4 className="font-semibold text-gray-100">Preview — draft invoice for {preview.client_name || "—"}</h4>
            <button onClick={() => setPreview(null)} className="text-gray-500 hover:text-gray-300"><X size={16} /></button>
          </div>
          {preview.problems?.length > 0 && (
            <ul className="text-sm text-rose-400 mb-3 list-disc pl-5">
              {preview.problems.map((p, i) => <li key={i}>{p.problem}</li>)}
            </ul>
          )}
          <table className="w-full text-sm">
            <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
              <th className="py-2 text-left font-medium">Line</th>
              <th className="py-2 text-right font-medium">Hours</th>
              <th className="py-2 text-right font-medium">Rate $/hr</th>
              <th className="py-2 text-right font-medium">Amount</th>
            </tr></thead>
            <tbody>
              {preview.lines.map((l, i) => (
                <tr key={i} className="border-b border-[#171c26]">
                  <td className="py-2 text-gray-200">
                    {l.name}
                    <span className="block text-[11px] text-gray-500">
                      {l.entries.map((en) => `${en.date} · ${en.user_name} · ${en.hours}h @ ${money(en.rate_cents / 100)} (${SOURCE_LABEL[en.rate_source] || en.rate_source})`).join("  |  ")}
                    </span>
                  </td>
                  <td className="py-2 text-right font-mono text-gray-300">{l.hours}h</td>
                  <td className="py-2 text-right">
                    <Input type="number" step="0.01" min="0"
                      defaultValue={l.rate_cents ? l.rate_cents / 100 : ""}
                      placeholder={l.rate_cents ? "" : "mixed"}
                      onChange={(e) => editLineRate(l, e.target.value)}
                      className={`${IN} font-mono w-24 h-7 text-xs ml-auto`} />
                  </td>
                  <td className="py-2 text-right font-mono text-gray-100">{money(l.amount_cents / 100)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="flex items-center justify-between mt-3">
            <span className="text-xs text-gray-500">Editing a rate re-prices every entry on that line; the job remembers the rate you bill at.</span>
            <span className="font-mono text-gray-100">Total {money(preview.total)}</span>
          </div>
          <div className="flex gap-2 mt-3 justify-end">
            <Button size="sm" variant="outline" onClick={refreshPreview} disabled={busy}>Update preview</Button>
            <Button size="sm" onClick={convert} disabled={!preview.can_convert || busy}
              className="bg-emerald-600 hover:bg-emerald-500 text-black font-semibold">
              Confirm — create draft invoice
            </Button>
          </div>
        </div>
      )}

      {drafts.length > 0 && (
        <div className="mt-5">
          <div className="text-xs uppercase tracking-wider text-gray-500 mb-2">Time-billing drafts</div>
          {drafts.map((inv) => (
            <div key={inv.id} className="flex items-center gap-3 text-sm border border-[#222838] rounded-md px-3 py-2 mb-2">
              <span className="text-gray-200">{inv.client_name}</span>
              <span className="font-mono text-gray-400">{money(inv.total)}</span>
              <span className="text-xs text-gray-500">{(inv.time_entry_ids || []).length} entries · draft</span>
              <span className="flex-1" />
              <Button size="sm" variant="outline" className="h-7" onClick={() => voidDraft(inv)}>Void — free the time</Button>
              <Button size="sm" className="h-7 bg-emerald-600 hover:bg-emerald-500 text-black" onClick={() => issueDraft(inv)}>Issue invoice</Button>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
