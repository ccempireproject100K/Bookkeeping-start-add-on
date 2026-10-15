import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, dollars, fmtDate, today } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { toast } from "sonner";
import { Plus, Trash2, Send, FileCheck2, AlertTriangle } from "lucide-react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import SalesTaxTab from "@/components/SalesTaxTab";

const ENTITY_LABEL = {
  individual: "Individual", sole_prop: "Sole proprietor", llc: "LLC",
  c_corp: "C corp", s_corp: "S corp", partnership: "Partnership", trust: "Trust",
};

const PILL_OK = "bg-emerald-500/10 text-emerald-400 border-emerald-500/20";
const PILL_WARN = "bg-amber-500/10 text-amber-400 border-amber-500/20";
const PILL_INFO = "bg-sky-500/10 text-sky-400 border-sky-500/20";

const blankForm = { name: "", business_name: "", email: "", phone: "", entity_type: "individual", tax_id: "", street: "", city: "", state: "", zip: "" };
const INPUT_CLS = "bg-[#0F1218] border-[#222838]";

export default function Tax() {
  const { user } = useAuth();
  const canWrite = user?.role === "owner";
  const canRevealPii = user?.role === "owner" || user?.role === "accountant";
  const curYear = new Date().getFullYear();
  const [year, setYear] = useState(curYear);
  const [summary, setSummary] = useState(null);
  const [tb, setTb] = useState(null);
  const [showNew, setShowNew] = useState(false);
  const [f, setF] = useState(blankForm);
  const [payFor, setPayFor] = useState(null);
  const [pay, setPay] = useState({ date: today(), amount: "", description: "" });
  const [preview, setPreview] = useState(null);
  const [revealedTin, setRevealedTin] = useState({});

  const revealTin = async (cid) => {
    try {
      const r = await api.get(`/tax/contractors/${cid}/tax-id`);
      setRevealedTin((m) => ({ ...m, [cid]: r.tax_id }));
    } catch (e) { toast.error(errMsg(e)); }
  };
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try { setSummary(await api.get(`/tax/summary?year=${year}`)); }
    catch { setSummary({ contractors: [], required_count: 0, reportable_total_cents: 0, missing_w9: [], threshold: 600 }); }
  };
  const loadTb = () => api.get("/tax/taxbandits/status").then(setTb).catch(() => setTb({ configured: false }));
  useEffect(() => { load(); loadTb(); }, [year]);

  const saveContractor = async () => {
    if (!f.name.trim()) { toast.error("Name is required"); return; }
    setBusy(true);
    try {
      await api.post("/tax/contractors", {
        name: f.name.trim(), business_name: f.business_name.trim(), email: f.email.trim(),
        phone: f.phone.trim(), entity_type: f.entity_type, tax_id: f.tax_id.trim(),
        address: { street: f.street.trim(), city: f.city.trim(), state: f.state.trim(), zip: f.zip.trim() },
      });
      toast.success("Contractor added"); setF(blankForm); setShowNew(false); load();
    } catch (e) { toast.error(errMsg(e)); }
    setBusy(false);
  };

  const toggleW9 = async (c) => {
    try { await api.post(`/tax/contractors/${c.id}/w9`, { received: !c.w9_received }); toast.success("W-9 status updated"); load(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const removeContractor = async (c) => {
    if (!window.confirm(`Remove ${c.name}? Payment history for this contractor stays in the database.`)) return;
    try { await api.del(`/tax/contractors/${c.id}`); toast.success("Contractor removed"); load(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const recordPayment = async () => {
    if (!pay.amount || parseFloat(pay.amount) <= 0) { toast.error("Enter a positive amount"); return; }
    try {
      await api.post(`/tax/contractors/${payFor.id}/payments`, { date: pay.date, amount: parseFloat(pay.amount), description: pay.description.trim() });
      toast.success("Payment recorded"); setPayFor(null); setPay({ date: today(), amount: "", description: "" }); load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const efile = async (c) => {
    if (!window.confirm(`Create a 1099-NEC draft in TaxBandits for ${c.name} (${money(c.year_total_cents)})? This does NOT transmit — you review and file inside TaxBandits.`)) return;
    try {
      const r = await api.post(`/tax/efile-1099/${c.id}?year=${year}`, {});
      toast.success(r.note || "Draft created in TaxBandits"); load(); loadTb();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const loadPreview = async () => {
    try { setPreview(await api.get(`/tax/1099-preview?year=${year}`)); }
    catch (e) { toast.error(errMsg(e)); }
  };

  if (summary === null) return <Loading />;
  const rows = summary.contractors || [];

  return (
    <div className="animate-rise">
      <PageHeader title={`Tax & 1099s — ${year}`} subtitle="Contractor tracking, W-9 status, and 1099-NEC e-filing via TaxBandits." testid="tax-page">
        <select value={year} onChange={(e) => setYear(parseInt(e.target.value, 10))} className={`${INPUT_CLS} rounded-md px-2 py-1.5 text-sm text-gray-200`} data-testid="tax-year-select">
          {[curYear, curYear - 1, curYear - 2].map((y) => <option key={y} value={y}>{y}</option>)}
        </select>
        {canWrite && <Button data-testid="tax-new-contractor" onClick={() => setShowNew((x) => !x)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" /> Add contractor</Button>}
      </PageHeader>

      <Tabs defaultValue="contractors">
        <TabsList className="bg-[#14171E] border border-[#222838] mb-4">
          <TabsTrigger value="contractors" data-testid="tab-contractors">Contractors & 1099s</TabsTrigger>
          <TabsTrigger value="sales" data-testid="tab-sales-tax">Sales Tax</TabsTrigger>
        </TabsList>
        <TabsContent value="contractors">
      {tb && !tb.configured && (
        <div className="mb-4 flex gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-gray-300" data-testid="tax-tb-banner">
          <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-400" />
          <p><strong className="text-amber-300">TaxBandits not connected.</strong> Tracking and year summaries work now; e-filing lights up once API keys are added. See <code className="text-amber-200">TAXBANDITS_SETUP.md</code> in the repo. No IRS filing is ever done in-house.</p>
        </div>
      )}
      {tb?.configured && tb.sandbox && (
        <div className="mb-4 rounded-lg border border-sky-500/30 bg-sky-500/10 p-3 text-sm text-gray-300">
          TaxBandits connected in <strong className="text-sky-300">sandbox</strong> mode — no real filings. Flip <code>TAXBANDITS_SANDBOX=false</code> to go live.
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 mb-4">
        {[
          { label: "Need 1099-NEC", value: String(summary.required_count), sub: `≥ ${dollars(summary.threshold)}`, cls: "text-gray-100" },
          { label: "Reportable total", value: money(summary.reportable_total_cents), sub: "Box 1 nonemployee comp", cls: "text-amber-400 font-mono" },
          { label: "Missing W-9", value: String((summary.missing_w9 || []).length), sub: (summary.missing_w9 || []).slice(0, 2).join(", ") || "—", cls: "text-gray-100" },
          { label: "Contractors", value: String(rows.length), sub: `${year} tracking`, cls: "text-gray-100" },
        ].map((s) => (
          <Card key={s.label} className="p-4">
            <div className="text-xs uppercase tracking-wider text-gray-500">{s.label}</div>
            <div className={`text-2xl font-semibold mt-1 ${s.cls}`}>{s.value}</div>
            <div className="text-xs text-gray-500 mt-0.5 truncate">{s.sub}</div>
          </Card>
        ))}
      </div>

      {showNew && canWrite && (
        <Card className="p-5 mb-4" data-testid="tax-new-form">
          <div className="text-xs uppercase tracking-wider text-gray-500 mb-3">New contractor</div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <div><Label className="text-xs">Name *</Label><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} className={INPUT_CLS} data-testid="tax-f-name" /></div>
            <div><Label className="text-xs">Business name</Label><Input value={f.business_name} onChange={(e) => setF({ ...f, business_name: e.target.value })} className={INPUT_CLS} data-testid="tax-f-business" /></div>
            <div><Label className="text-xs">Email</Label><Input value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} className={INPUT_CLS} /></div>
            <div><Label className="text-xs">Phone</Label><Input value={f.phone} onChange={(e) => setF({ ...f, phone: e.target.value })} className={INPUT_CLS} /></div>
            <div><Label className="text-xs">Entity type</Label>
              <select value={f.entity_type} onChange={(e) => setF({ ...f, entity_type: e.target.value })} className={`${INPUT_CLS} w-full rounded-md px-2 py-1.5 text-sm text-gray-200`} data-testid="tax-f-entity">
                {Object.entries(ENTITY_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </div>
            <div><Label className="text-xs">SSN / EIN</Label><Input value={f.tax_id} onChange={(e) => setF({ ...f, tax_id: e.target.value })} placeholder="Stored securely" className={INPUT_CLS} data-testid="tax-f-taxid" /></div>
            <div><Label className="text-xs">Street</Label><Input value={f.street} onChange={(e) => setF({ ...f, street: e.target.value })} className={INPUT_CLS} /></div>
            <div className="grid grid-cols-3 gap-2">
              <div><Label className="text-xs">City</Label><Input value={f.city} onChange={(e) => setF({ ...f, city: e.target.value })} className={INPUT_CLS} /></div>
              <div><Label className="text-xs">State</Label><Input value={f.state} onChange={(e) => setF({ ...f, state: e.target.value })} className={INPUT_CLS} /></div>
              <div><Label className="text-xs">ZIP</Label><Input value={f.zip} onChange={(e) => setF({ ...f, zip: e.target.value })} className={INPUT_CLS} /></div>
            </div>
          </div>
          <div className="flex gap-2 mt-4">
            <Button onClick={saveContractor} disabled={busy} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="tax-f-save">Add contractor</Button>
            <Button variant="ghost" onClick={() => setShowNew(false)}>Cancel</Button>
          </div>
        </Card>
      )}

      <Card className="p-0 overflow-hidden mb-4">
        <table className="w-full text-sm" data-testid="tax-table">
          <thead>
            <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
              <th className="px-5 py-3 text-left font-medium">Contractor</th>
              <th className="px-5 py-3 text-right font-medium">Paid {year}</th>
              <th className="px-5 py-3 text-left font-medium">W-9</th>
              <th className="px-5 py-3 text-left font-medium">1099-NEC</th>
              <th className="px-5 py-3 text-left font-medium">E-file</th>
              {canWrite && <th className="px-5 py-3 text-right font-medium">Actions</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.id} className="border-b border-[#171c26] hover:bg-[#161b25]" data-testid={`tax-row-${c.id}`}>
                <td className="px-5 py-3">
                  <div className="text-gray-100 font-medium">{c.name}</div>
                  <div className="text-xs text-gray-500">{ENTITY_LABEL[c.entity_type]}{c.business_name ? ` · ${c.business_name}` : ""}{c.tax_id_last4 ? ` · •••${c.tax_id_last4}` : ""}</div>
                </td>
                <td className="px-5 py-3 text-right font-mono text-gray-200">{money(c.year_total_cents)}</td>
                <td className="px-5 py-3">
                  {canWrite
                    ? <button onClick={() => toggleW9(c)} title="Toggle W-9 received" data-testid={`tax-w9-${c.id}`} className={`px-2 py-0.5 rounded-full text-xs border ${c.w9_received ? PILL_OK : PILL_WARN}`}>{c.w9_received ? "Received" : "Missing"}</button>
                    : <span className={`px-2 py-0.5 rounded-full text-xs border ${c.w9_received ? PILL_OK : PILL_WARN}`}>{c.w9_received ? "Received" : "Missing"}</span>}
                </td>
                <td className="px-5 py-3">{c.needs_1099 ? <span className={`px-2 py-0.5 rounded-full text-xs border ${PILL_INFO}`}>Required</span> : <span className="text-xs text-gray-600">—</span>}</td>
                <td className="px-5 py-3 text-xs text-gray-500">
                  {c.efile?.submission_id ? <span>{c.efile.status}{c.efile.sandbox ? " (sandbox)" : ""}</span> : <span className="text-gray-600">Not filed</span>}
                </td>
                {canWrite && (
                  <td className="px-5 py-3 text-right">
                    <div className="flex justify-end gap-1">
                      <Button size="sm" variant="outline" className="h-7" onClick={() => { setPayFor(c); setPay({ date: today(), amount: "", description: "" }); }} data-testid={`tax-pay-${c.id}`}>Record payment</Button>
                      {c.needs_1099 && !c.efile?.submission_id && (
                        <Button size="sm" className="h-7 bg-amber-600 hover:bg-amber-500 text-black" onClick={() => efile(c)} disabled={!tb?.configured} title={tb?.configured ? "Create 1099-NEC draft in TaxBandits" : "Connect TaxBandits first"} data-testid={`tax-efile-${c.id}`}>
                          <Send size={12} className="mr-1" /> E-file
                        </Button>
                      )}
                      <button onClick={() => removeContractor(c)} className="text-gray-600 hover:text-rose-400 px-1" data-testid={`tax-del-${c.id}`}><Trash2 size={14} /></button>
                    </div>
                  </td>
                )}
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={canWrite ? 6 : 5} className="px-5 py-12 text-center text-gray-500">No contractors yet.</td></tr>}
          </tbody>
        </table>
      </Card>

      {canWrite && (
        <Card className="p-5 mb-4" data-testid="tax-preview-panel">
          <div className="flex flex-wrap items-center justify-between gap-3 mb-2">
            <div>
              <div className="font-medium text-gray-100 flex items-center gap-2"><FileCheck2 size={16} className="text-amber-400" /> 1099-NEC data preview</div>
              <p className="text-xs text-gray-500 mt-1">Recipient + Box 1 data shaped for e-filing. Tax IDs stay masked — reveal needs PII access. Draft data only, nothing is filed in-house.</p>
            </div>
            <Button size="sm" variant="outline" onClick={loadPreview} data-testid="tax-preview-btn">Load preview</Button>
          </div>
          {preview && (
            <table className="w-full text-sm mt-3">
              <thead>
                <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="py-2 text-left font-medium">Recipient</th>
                  <th className="py-2 text-left font-medium">TIN</th>
                  <th className="py-2 text-left font-medium">Address</th>
                  <th className="py-2 text-right font-medium">Box 1</th>
                  <th className="py-2 text-left font-medium">W-9</th>
                </tr>
              </thead>
              <tbody>
                {preview.recipients.map((r) => (
                  <tr key={r.contractor_id} className="border-b border-[#171c26]">
                    <td className="py-2 text-gray-100">{r.name}{r.business_name ? <span className="block text-xs text-gray-500">{r.business_name} · {ENTITY_LABEL[r.entity_type]}</span> : null}</td>
                    <td className="py-2 font-mono text-gray-300">
                      {revealedTin[r.contractor_id] ? (
                        <span>{revealedTin[r.contractor_id]}</span>
                      ) : r.tin_last4 ? (
                        <span>•••{r.tin_last4}</span>
                      ) : (
                        <span className="text-amber-400">missing</span>
                      )}
                      {!revealedTin[r.contractor_id] && r.tin_last4 && canRevealPii && (
                        <button onClick={() => revealTin(r.contractor_id)}
                          className="ml-2 text-xs text-sky-400 hover:underline"
                          data-testid={`tax-reveal-${r.contractor_id}`}>
                          reveal
                        </button>
                      )}
                    </td>
                    <td className="py-2 text-gray-400 text-xs">{[r.address?.street, r.address?.city, r.address?.state, r.address?.zip].filter(Boolean).join(", ") || "—"}</td>
                    <td className="py-2 text-right font-mono text-gray-200">{money(r.box1_cents)}</td>
                    <td className="py-2"><span className={`px-2 py-0.5 rounded-full text-xs border ${r.w9_received ? PILL_OK : PILL_WARN}`}>{r.w9_received ? "Received" : "Missing"}</span></td>
                  </tr>
                ))}
                {preview.recipients.length === 0 && <tr><td colSpan={5} className="py-8 text-center text-gray-500">No contractors meet the {dollars(summary.threshold)} reporting threshold for {year}.</td></tr>}
              </tbody>
            </table>
          )}
        </Card>
      )}

      <Dialog open={!!payFor} onOpenChange={(o) => !o && setPayFor(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838]" data-testid="tax-pay-form">
          <DialogHeader><DialogTitle>Record payment — {payFor?.name}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">Date</Label><Input type="date" value={pay.date} onChange={(e) => setPay({ ...pay, date: e.target.value })} className={INPUT_CLS} /></div>
            <div><Label className="text-xs">Amount $</Label><Input type="number" step="0.01" min={0.01} value={pay.amount} onChange={(e) => setPay({ ...pay, amount: e.target.value })} className={`${INPUT_CLS} font-mono`} data-testid="tax-pay-amount" /></div>
          </div>
          <div><Label className="text-xs">Description</Label><Input value={pay.description} onChange={(e) => setPay({ ...pay, description: e.target.value })} placeholder="e.g. March booth rent payout" className={INPUT_CLS} /></div>
          <DialogFooter><Button onClick={recordPayment} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="tax-pay-save">Record</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <p className="text-xs text-gray-600 mt-2">
        1099-NEC is required for non-corporate contractors paid ≥ {dollars(summary.threshold)} in the year. E-filing creates a draft in TaxBandits — you review and transmit there. This app never files with the IRS directly.
      </p>
        </TabsContent>
        <TabsContent value="sales">
          <SalesTaxTab canWrite={canWrite} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
