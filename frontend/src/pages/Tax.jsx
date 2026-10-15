import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, dollars, fmtDate, today } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { toast } from "sonner";
import { Plus, Trash2, Send, FileCheck2, AlertTriangle } from "lucide-react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import SalesTaxTab from "@/components/SalesTaxTab";
import { useTranslation } from "react-i18next";
const ENTITY_LABEL = {
  individual: "Individual",
  sole_prop: "Sole proprietor",
  llc: "LLC",
  c_corp: "C corp",
  s_corp: "S corp",
  partnership: "Partnership",
  trust: "Trust"
};
const PILL_OK = "bg-emerald-500/10 text-emerald-400 border-emerald-500/20";
const PILL_WARN = "bg-amber-500/10 text-amber-400 border-amber-500/20";
const PILL_INFO = "bg-sky-500/10 text-sky-400 border-sky-500/20";
const blankForm = {
  name: "",
  business_name: "",
  email: "",
  phone: "",
  entity_type: "individual",
  tax_id: "",
  street: "",
  city: "",
  state: "",
  zip: ""
};
const INPUT_CLS = "bg-[#0F1218] border-[#222838]";
export default function Tax() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const canWrite = user?.role === "owner";
  const canRevealPii = user?.role === "owner" || user?.role === "accountant";
  const curYear = new Date().getFullYear();
  const [year, setYear] = useState(curYear);
  const [summary, setSummary] = useState(null);
  const [tb, setTb] = useState(null);
  const [showNew, setShowNew] = useState(false);
  const [f, setF] = useState(blankForm);
  const [payFor, setPayFor] = useState(null);
  const [pay, setPay] = useState({
    date: today(),
    amount: "",
    description: ""
  });
  const [preview, setPreview] = useState(null);
  const [revealedTin, setRevealedTin] = useState({});
  const [historyFor, setHistoryFor] = useState(null);
  const [history, setHistory] = useState([]);
  const [efileFor, setEfileFor] = useState(null);
  const [efileDetail, setEfileDetail] = useState(null);
  const [efileBusy, setEfileBusy] = useState(false);
  const revealTin = async cid => {
    try {
      const r = await api.get(`/tax/contractors/${cid}/tax-id`);
      setRevealedTin(m => ({
        ...m,
        [cid]: r.tax_id
      }));
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const [busy, setBusy] = useState(false);
  const load = async () => {
    try {
      setSummary(await api.get(`/tax/summary?year=${year}`));
    } catch {
      setSummary({
        contractors: [],
        required_count: 0,
        reportable_total_cents: 0,
        missing_w9: [],
        threshold: 600
      });
    }
  };
  const loadTb = () => api.get("/tax/taxbandits/status").then(setTb).catch(() => setTb({
    configured: false
  }));
  useEffect(() => {
    load();
    loadTb();
  }, [year]);
  const saveContractor = async () => {
    if (!f.name.trim()) {
      toast.error(t("tax.nameIsRequired"));
      return;
    }
    setBusy(true);
    try {
      await api.post("/tax/contractors", {
        name: f.name.trim(),
        business_name: f.business_name.trim(),
        email: f.email.trim(),
        phone: f.phone.trim(),
        entity_type: f.entity_type,
        tax_id: f.tax_id.trim(),
        address: {
          street: f.street.trim(),
          city: f.city.trim(),
          state: f.state.trim(),
          zip: f.zip.trim()
        }
      });
      toast.success(t("tax.contractorAdded"));
      setF(blankForm);
      setShowNew(false);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
    setBusy(false);
  };
  const toggleW9 = async c => {
    try {
      await api.post(`/tax/contractors/${c.id}/w9`, {
        received: !c.w9_received
      });
      toast.success(t("tax.w9StatusUpdated"));
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const removeContractor = async c => {
    if (!window.confirm(`Remove ${c.name}? Payment history for this contractor stays in the database.`)) return;
    try {
      await api.del(`/tax/contractors/${c.id}`);
      toast.success(t("tax.contractorRemoved"));
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const recordPayment = async () => {
    if (!pay.amount || parseFloat(pay.amount) <= 0) {
      toast.error(t("tax.enterAPositiveAmount"));
      return;
    }
    try {
      await api.post(`/tax/contractors/${payFor.id}/payments`, {
        date: pay.date,
        amount: parseFloat(pay.amount),
        description: pay.description.trim()
      });
      toast.success(t("tax.paymentRecorded"));
      setPayFor(null);
      setPay({
        date: today(),
        amount: "",
        description: ""
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const efile = async c => {
    if (!window.confirm(`Create a 1099-NEC draft in TaxBandits for ${c.name} (${money(c.year_total_cents)})? This does NOT transmit — you review and file inside TaxBandits.`)) return;
    try {
      const r = await api.post(`/tax/efile-1099/${c.id}?year=${year}`, {});
      toast.success(r.note || "Draft created in TaxBandits");
      load();
      loadTb();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const loadPreview = async () => {
    try {
      setPreview(await api.get(`/tax/1099-preview?year=${year}`));
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const openHistory = async c => {
    setHistoryFor(c);
    setHistory([]);
    try {
      setHistory(await api.get(`/tax/contractors/${c.id}/payments?year=${year}`));
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const deletePayment = async p => {
    if (!window.confirm(`Delete this ${money(p.amount_cents)} payment dated ${p.date}? The year total will update.`)) return;
    try {
      await api.del(`/tax/payments/${p.id}`);
      toast.success(t("tax.paymentDeleted"));
      openHistory(historyFor);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const openEfile = c => {
    setEfileFor(c);
    setEfileDetail(c.efile || {});
  };
  const refreshEfile = async () => {
    if (!efileFor) return;
    setEfileBusy(true);
    try {
      const r = await api.get(`/tax/efile-1099/${efileFor.id}/status`);
      setEfileDetail(r.efile || {});
      load();
      toast.success(t("tax.statusRefreshed"));
    } catch (e) {
      toast.error(errMsg(e));
    }
    setEfileBusy(false);
  };
  if (summary === null) return <Loading />;
  const rows = summary.contractors || [];
  return <div className="animate-rise">
      <PageHeader title={`Tax & 1099s — ${year}`} subtitle="Contractor tracking, W-9 status, and 1099-NEC e-filing via TaxBandits." testid="tax-page">
        <select value={year} onChange={e => setYear(parseInt(e.target.value, 10))} className={`${INPUT_CLS} rounded-md px-2 py-1.5 text-sm text-gray-200`} data-testid="tax-year-select">
          {[curYear, curYear - 1, curYear - 2].map(y => <option key={y} value={y}>{y}</option>)}
        </select>
        {canWrite && <Button data-testid="tax-new-contractor" onClick={() => setShowNew(x => !x)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" /> {t("tax.addContractor")}</Button>}
      </PageHeader>

      <Tabs defaultValue="contractors">
        <TabsList className="bg-[#14171E] border border-[#222838] mb-4">
          <TabsTrigger value="contractors" data-testid="tab-contractors">{t("tax.contractors1099s")}</TabsTrigger>
          <TabsTrigger value="sales" data-testid="tab-sales-tax">{t("tax.salesTax")}</TabsTrigger>
        </TabsList>
        <TabsContent value="contractors">
      {tb && !tb.configured && <div className="mb-4 flex gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-gray-300" data-testid="tax-tb-banner">
          <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-400" />
          <p><strong className="text-amber-300">{t("tax.taxbanditsNotConnected")}</strong> Tracking and year summaries work now; e-filing lights up once API keys are added. See <code className="text-amber-200">{t("tax.taxbanditsSETUPMd")}</code> {t("tax.inTheRepoNoIRSFiling")}</p>
        </div>}
      {tb?.configured && tb.sandbox && <div className="mb-4 rounded-lg border border-sky-500/30 bg-sky-500/10 p-3 text-sm text-gray-300">{t("tax.taxbanditsConnectedIn")} <strong className="text-sky-300">{t("tax.sandbox")}</strong> {t("tax.modeNoRealFilingsFlip")} <code>{t("tax.taxbanditsSANDBOXFalse")}</code> {t("tax.toGoLive")}</div>}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 mb-4">
        {[{
            label: "Need 1099-NEC",
            value: String(summary.required_count),
            sub: `≥ ${dollars(summary.threshold)}`,
            cls: "text-gray-100"
          }, {
            label: "Reportable total",
            value: money(summary.reportable_total_cents),
            sub: "Box 1 nonemployee comp",
            cls: "text-amber-400 font-mono"
          }, {
            label: "Missing W-9",
            value: String((summary.missing_w9 || []).length),
            sub: (summary.missing_w9 || []).slice(0, 2).join(", ") || "—",
            cls: "text-gray-100"
          }, {
            label: "Contractors",
            value: String(rows.length),
            sub: `${year} tracking`,
            cls: "text-gray-100"
          }].map(s => <Card key={s.label} className="p-4">
            <div className="text-xs uppercase tracking-wider text-gray-500">{s.label}</div>
            <div className={`text-2xl font-semibold mt-1 ${s.cls}`}>{s.value}</div>
            <div className="text-xs text-gray-500 mt-0.5 truncate">{s.sub}</div>
          </Card>)}
      </div>

      {showNew && canWrite && <Card className="p-5 mb-4" data-testid="tax-new-form">
          <div className="text-xs uppercase tracking-wider text-gray-500 mb-3">{t("tax.newContractor")}</div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <div><Label className="text-xs">{t("tax.name")}</Label><Input value={f.name} onChange={e => setF({
                ...f,
                name: e.target.value
              })} className={INPUT_CLS} data-testid="tax-f-name" /></div>
            <div><Label className="text-xs">{t("tax.businessName")}</Label><Input value={f.business_name} onChange={e => setF({
                ...f,
                business_name: e.target.value
              })} className={INPUT_CLS} data-testid="tax-f-business" /></div>
            <div><Label className="text-xs">{t("tax.email")}</Label><Input value={f.email} onChange={e => setF({
                ...f,
                email: e.target.value
              })} className={INPUT_CLS} /></div>
            <div><Label className="text-xs">{t("tax.phone")}</Label><Input value={f.phone} onChange={e => setF({
                ...f,
                phone: e.target.value
              })} className={INPUT_CLS} /></div>
            <div><Label className="text-xs">{t("tax.entityType")}</Label>
              <select value={f.entity_type} onChange={e => setF({
                ...f,
                entity_type: e.target.value
              })} className={`${INPUT_CLS} w-full rounded-md px-2 py-1.5 text-sm text-gray-200`} data-testid="tax-f-entity">
                {Object.entries(ENTITY_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </div>
            <div><Label className="text-xs">{t("tax.ssnEIN")}</Label><Input value={f.tax_id} onChange={e => setF({
                ...f,
                tax_id: e.target.value
              })} placeholder={t("tax.storedSecurely")} className={INPUT_CLS} data-testid="tax-f-taxid" /></div>
            <div><Label className="text-xs">{t("tax.street")}</Label><Input value={f.street} onChange={e => setF({
                ...f,
                street: e.target.value
              })} className={INPUT_CLS} /></div>
            <div className="grid grid-cols-3 gap-2">
              <div><Label className="text-xs">{t("tax.city")}</Label><Input value={f.city} onChange={e => setF({
                  ...f,
                  city: e.target.value
                })} className={INPUT_CLS} /></div>
              <div><Label className="text-xs">{t("tax.state")}</Label><Input value={f.state} onChange={e => setF({
                  ...f,
                  state: e.target.value
                })} className={INPUT_CLS} /></div>
              <div><Label className="text-xs">{t("tax.zip")}</Label><Input value={f.zip} onChange={e => setF({
                  ...f,
                  zip: e.target.value
                })} className={INPUT_CLS} /></div>
            </div>
          </div>
          <div className="flex gap-2 mt-4">
            <Button onClick={saveContractor} disabled={busy} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="tax-f-save">{t("tax.addContractor")}</Button>
            <Button variant="ghost" onClick={() => setShowNew(false)}>{t("tax.cancel")}</Button>
          </div>
        </Card>}

      <Card className="p-0 overflow-hidden mb-4">
        <table className="w-full text-sm" data-testid="tax-table">
          <thead>
            <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
              <th className="px-5 py-3 text-left font-medium">{t("tax.contractor")}</th>
              <th className="px-5 py-3 text-right font-medium">{t("tax.paid")} {year}</th>
              <th className="px-5 py-3 text-left font-medium">{t("tax.w9")}</th>
              <th className="px-5 py-3 text-left font-medium">{t("tax.NEC")}</th>
              <th className="px-5 py-3 text-left font-medium">{t("tax.eFile")}</th>
              {canWrite && <th className="px-5 py-3 text-right font-medium">{t("tax.actions")}</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map(c => <tr key={c.id} className="border-b border-[#171c26] hover:bg-[#161b25]" data-testid={`tax-row-${c.id}`}>
                <td className="px-5 py-3">
                  <div className="text-gray-100 font-medium">{c.name}</div>
                  <div className="text-xs text-gray-500">{ENTITY_LABEL[c.entity_type]}{c.business_name ? ` · ${c.business_name}` : ""}{c.tax_id_last4 ? ` · •••${c.tax_id_last4}` : ""}</div>
                </td>
                <td className="px-5 py-3 text-right">
                  <button onClick={() => openHistory(c)} title={t("tax.viewPaymentHistory")} className="font-mono text-gray-200 hover:text-amber-300 hover:underline" data-testid={`tax-history-${c.id}`}>{money(c.year_total_cents)}</button>
                </td>
                <td className="px-5 py-3">
                  {canWrite ? <button onClick={() => toggleW9(c)} title={t("tax.toggleW9Received")} data-testid={`tax-w9-${c.id}`} className={`px-2 py-0.5 rounded-full text-xs border ${c.w9_received ? PILL_OK : PILL_WARN}`}>{c.w9_received ? "Received" : "Missing"}</button> : <span className={`px-2 py-0.5 rounded-full text-xs border ${c.w9_received ? PILL_OK : PILL_WARN}`}>{c.w9_received ? "Received" : "Missing"}</span>}
                </td>
                <td className="px-5 py-3">{c.needs_1099 ? <span className={`px-2 py-0.5 rounded-full text-xs border ${PILL_INFO}`}>{t("tax.required")}</span> : <span className="text-xs text-gray-600">—</span>}</td>
                <td className="px-5 py-3 text-xs text-gray-500">
                  {c.efile?.submission_id ? <button onClick={() => openEfile(c)} title={t("tax.viewFilingStatus")} className="hover:underline text-gray-300" data-testid={`tax-efile-status-${c.id}`}>
                        <span className={`px-2 py-0.5 rounded-full border ${/accept|transmit|filed/i.test(c.efile.status || "") ? PILL_OK : /reject|fail|error/i.test(c.efile.status || "") ? "bg-rose-500/10 text-rose-400 border-rose-500/20" : PILL_INFO}`}>
                          {c.efile.status || "Filed"}{c.efile.sandbox ? " · sandbox" : ""}
                        </span>
                      </button> : <span className="text-gray-600">{t("tax.notFiled")}</span>}
                </td>
                {canWrite && <td className="px-5 py-3 text-right">
                    <div className="flex justify-end gap-1">
                      <Button size="sm" variant="outline" className="h-7" onClick={() => {
                      setPayFor(c);
                      setPay({
                        date: today(),
                        amount: "",
                        description: ""
                      });
                    }} data-testid={`tax-pay-${c.id}`}>{t("tax.recordPayment")}</Button>
                      {c.needs_1099 && !c.efile?.submission_id && <Button size="sm" className="h-7 bg-amber-600 hover:bg-amber-500 text-black" onClick={() => efile(c)} disabled={!tb?.configured} title={tb?.configured ? "Create 1099-NEC draft in TaxBandits" : "Connect TaxBandits first"} data-testid={`tax-efile-${c.id}`}>
                          <Send size={12} className="mr-1" /> {t("tax.eFile")}</Button>}
                      <button onClick={() => removeContractor(c)} className="text-gray-600 hover:text-rose-400 px-1" data-testid={`tax-del-${c.id}`}><Trash2 size={14} /></button>
                    </div>
                  </td>}
              </tr>)}
            {rows.length === 0 && <tr><td colSpan={canWrite ? 6 : 5} className="px-5 py-12 text-center text-gray-500">{t("tax.noContractorsYet")}</td></tr>}
          </tbody>
        </table>
      </Card>

      {canWrite && <Card className="p-5 mb-4" data-testid="tax-preview-panel">
          <div className="flex flex-wrap items-center justify-between gap-3 mb-2">
            <div>
              <div className="font-medium text-gray-100 flex items-center gap-2"><FileCheck2 size={16} className="text-amber-400" /> {t("tax.NECDataPreview")}</div>
              <p className="text-xs text-gray-500 mt-1">{t("tax.recipientBox1DataShapedFor")}</p>
            </div>
            <Button size="sm" variant="outline" onClick={loadPreview} data-testid="tax-preview-btn">{t("tax.loadPreview")}</Button>
          </div>
          {preview && <table className="w-full text-sm mt-3">
              <thead>
                <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="py-2 text-left font-medium">{t("tax.recipient")}</th>
                  <th className="py-2 text-left font-medium">{t("tax.tin")}</th>
                  <th className="py-2 text-left font-medium">{t("tax.address")}</th>
                  <th className="py-2 text-right font-medium">{t("tax.box1")}</th>
                  <th className="py-2 text-left font-medium">{t("tax.w9")}</th>
                </tr>
              </thead>
              <tbody>
                {preview.recipients.map(r => <tr key={r.contractor_id} className="border-b border-[#171c26]">
                    <td className="py-2 text-gray-100">{r.name}{r.business_name ? <span className="block text-xs text-gray-500">{r.business_name} · {ENTITY_LABEL[r.entity_type]}</span> : null}</td>
                    <td className="py-2 font-mono text-gray-300">
                      {revealedTin[r.contractor_id] ? <span>{revealedTin[r.contractor_id]}</span> : r.tin_last4 ? <span>•••{r.tin_last4}</span> : <span className="text-amber-400">{t("tax.missing")}</span>}
                      {!revealedTin[r.contractor_id] && r.tin_last4 && canRevealPii && <button onClick={() => revealTin(r.contractor_id)} className="ml-2 text-xs text-sky-400 hover:underline" data-testid={`tax-reveal-${r.contractor_id}`}>{t("tax.reveal")}</button>}
                    </td>
                    <td className="py-2 text-gray-400 text-xs">{[r.address?.street, r.address?.city, r.address?.state, r.address?.zip].filter(Boolean).join(", ") || "—"}</td>
                    <td className="py-2 text-right font-mono text-gray-200">{money(r.box1_cents)}</td>
                    <td className="py-2"><span className={`px-2 py-0.5 rounded-full text-xs border ${r.w9_received ? PILL_OK : PILL_WARN}`}>{r.w9_received ? "Received" : "Missing"}</span></td>
                  </tr>)}
                {preview.recipients.length === 0 && <tr><td colSpan={5} className="py-8 text-center text-gray-500">{t("tax.noContractorsMeetThe")} {dollars(summary.threshold)} {t("tax.reportingThresholdFor")} {year}.</td></tr>}
              </tbody>
            </table>}
        </Card>}

      <Dialog open={!!payFor} onOpenChange={o => !o && setPayFor(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838]" data-testid="tax-pay-form">
          <DialogHeader><DialogTitle>{t("tax.recordPayment2")} {payFor?.name}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("tax.date")}</Label><Input type="date" value={pay.date} onChange={e => setPay({
                  ...pay,
                  date: e.target.value
                })} className={INPUT_CLS} /></div>
            <div><Label className="text-xs">{t("tax.amount")}</Label><Input type="number" step="0.01" min={0.01} value={pay.amount} onChange={e => setPay({
                  ...pay,
                  amount: e.target.value
                })} className={`${INPUT_CLS} font-mono`} data-testid="tax-pay-amount" /></div>
          </div>
          <div><Label className="text-xs">{t("tax.description")}</Label><Input value={pay.description} onChange={e => setPay({
                ...pay,
                description: e.target.value
              })} placeholder={t("tax.eGMarchBoothRentPayout")} className={INPUT_CLS} /></div>
          <DialogFooter><Button onClick={recordPayment} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="tax-pay-save">{t("tax.record")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!historyFor} onOpenChange={o => !o && setHistoryFor(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg" data-testid="tax-history-dialog">
          <DialogHeader><DialogTitle>{t("tax.paymentHistory")} {historyFor?.name} ({year})</DialogTitle></DialogHeader>
          {history.length === 0 ? <p className="text-sm text-gray-500 py-6 text-center">{t("tax.noPaymentsRecordedFor")} {year}.</p> : <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                    <th className="py-2 text-left font-medium">{t("tax.date")}</th>
                    <th className="py-2 text-left font-medium">{t("tax.description")}</th>
                    <th className="py-2 text-right font-medium">{t("tax.amount2")}</th>
                    {canWrite && <th className="py-2 text-right font-medium"> </th>}
                  </tr>
                </thead>
                <tbody>
                  {history.map(p => <tr key={p.id} className="border-b border-[#171c26]" data-testid={`tax-payment-${p.id}`}>
                      <td className="py-2 text-gray-300">{fmtDate(p.date)}</td>
                      <td className="py-2 text-gray-400">{p.description || <span className="text-gray-600">—</span>}</td>
                      <td className="py-2 text-right font-mono text-gray-100">{money(p.amount_cents)}</td>
                      {canWrite && <td className="py-2 text-right">
                          <button onClick={() => deletePayment(p)} className="text-gray-600 hover:text-rose-400" title={t("tax.deletePayment")} data-testid={`tax-payment-del-${p.id}`}><Trash2 size={14} /></button>
                        </td>}
                    </tr>)}
                </tbody>
              </table>}
          <DialogFooter className="flex justify-between">
            <span className="text-sm text-gray-400 mr-auto">{t("tax.yearTotal")} <strong className="font-mono text-gray-100">{money((history || []).reduce((s, p) => s + (p.amount_cents || 0), 0))}</strong></span>
            {canWrite && <Button onClick={() => {
                setPayFor(historyFor);
                setHistoryFor(null);
                setPay({
                  date: today(),
                  amount: "",
                  description: ""
                });
              }} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="tax-history-record"><Plus size={14} className="mr-1" /> {t("tax.recordPayment")}</Button>}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!efileFor} onOpenChange={o => !o && setEfileFor(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-md" data-testid="tax-efile-dialog">
          <DialogHeader><DialogTitle>{t("tax.NECFiling")} {efileFor?.name}</DialogTitle></DialogHeader>
          {efileDetail && <div className="space-y-2 text-sm">
              {[["Status", efileDetail.status || "—"], ["Submission ID", <span key="s" className="font-mono">{efileDetail.submission_id || "—"}</span>], ["Record ID", <span key="r" className="font-mono">{efileDetail.record_id || "—"}</span>], ["Tax year", efileDetail.year || "—"], ["Amount (Box 1)", money(efileDetail.amount_cents)], ["Environment", efileDetail.sandbox ? "Sandbox (test)" : "Production"], ["Last updated", efileDetail.updated_at ? fmtDate(efileDetail.updated_at) : "—"]].map(([k, v]) => <div key={k} className="flex justify-between gap-4 py-1.5 border-b border-[#171c26]">
                  <span className="text-gray-500">{k}</span>
                  <span className="text-gray-200 text-right">{v}</span>
                </div>)}
              <p className="text-xs text-gray-600 pt-1">{t("tax.thisDraftLivesInTaxBanditsReview")}</p>
            </div>}
          <DialogFooter>
            <Button variant="outline" onClick={refreshEfile} disabled={efileBusy} data-testid="tax-efile-refresh">
              {efileBusy ? "Refreshing…" : "Refresh status"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <p className="text-xs text-gray-600 mt-2">{t("tax.NECIsRequiredForNon")} {dollars(summary.threshold)} {t("tax.inTheYearEFilingCreates")}</p>
        </TabsContent>
        <TabsContent value="sales">
          <SalesTaxTab canWrite={canWrite} />
        </TabsContent>
      </Tabs>
    </div>;
}
