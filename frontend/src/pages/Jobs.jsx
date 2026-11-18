import { useEffect, useState, useCallback } from "react";
import { api, errMsg } from "@/lib/api";
import AiaBilling from "@/components/AiaBilling";
import { money, dollars, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Plus, AlertTriangle, ArrowRight, Briefcase, BellRing, Settings2, Trash2, Zap } from "lucide-react";
import { useTranslation } from "react-i18next";
const CATS = ["material", "labor", "equipment", "subcontractor", "other"];
const STATUS_CLS = {
  estimate: "bg-sky-500/10 text-sky-400 border-sky-500/20",
  active: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  completed: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  closed: "bg-gray-500/10 text-gray-400 border-gray-500/20"
};
const NEXT = {
  estimate: "active",
  active: "completed",
  completed: "closed"
};
const NEXT_LABEL = {
  estimate: "Convert to Job",
  active: "Mark Completed",
  completed: "Close Job"
};
const CO_CLS = {
  pending: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  approved: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  rejected: "bg-rose-500/10 text-rose-400 border-rose-500/20",
};
const NEXT = { estimate: "active", active: "completed", completed: "closed" };
const NEXT_LABEL = { estimate: "Convert to Job", active: "Mark Completed", completed: "Close Job" };

export default function Jobs() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const isOwner = user?.role === "owner";
  const [rows, setRows] = useState(null);
  const [customers, setCustomers] = useState([]);
  const [team, setTeam] = useState([]);
  const [supplies, setSupplies] = useState([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [detail, setDetail] = useState(null);
  const [jc, setJc] = useState(null);               // job-costing config + templates
  const [suggestion, setSuggestion] = useState(null);
  const [allTeam, setAllTeam] = useState([]);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const [form, setForm] = useState({ customer_name: "", job_type: "installation", description: "", estimated_revenue: "", material: "", labor: "", equipment: "", subcontractor: "", other: "", retainage_pct: "", retainage_payable_pct: "" });
  const [cost, setCost] = useState({ category: "material", description: "", amount: "", hours: "", rate: "", technician_id: "", paid: false, product_id: "", qty: "1", commitment_id: "", phase_id: "" });
  const [co, setCo] = useState({ description: "", reason: "", revenue_delta: "", cost_delta: "", delay_days: "", material: "", labor: "", equipment: "", subcontractor: "", other: "" });
  const [notifs, setNotifs] = useState([]);
  const [alertCfg, setAlertCfg] = useState(null);
  const [cfgForm, setCfgForm] = useState({
    warning_pct: "90",
    overrun_pct: "100",
    enabled: true
  });
  const [cfgOpen, setCfgOpen] = useState(false);
  const [retRollup, setRetRollup] = useState(null);
  const [invAmt, setInvAmt] = useState("");
  const load = () => api.get("/jobs").then(setRows).catch(() => setRows([]));
  const loadJc = useCallback(() => {
    api.get("/job-costing/config").then(setJc).catch(() => {});
    api.get("/job-costing/burden-suggestion").then(setSuggestion).catch(() => {});
  }, []);
  useEffect(() => {
    load();
    loadJc();
    api.get("/retainage").then(setRetRollup).catch(() => {});
    api.get("/customers").then(setCustomers);
    api.get("/products").then(setSupplies).catch(() => {});
    api.get("/auth/team").then((t) => { setAllTeam(t); setTeam(t.filter((u) => u.role === "barber")); });
  }, [loadJc]);

  const saveJc = async (patch) => {
    try { const cfg = await api.patch("/job-costing/config", patch); setJc(cfg); toast.success("Job costing settings saved"); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const openDetail = useCallback((id) => api.get(`/jobs/${id}`).then(setDetail), []);

  const loadNotifs = useCallback(() => {
    api.get("/jobs/notifications").then((r) => setNotifs(r.notifications || [])).catch(() => {});
    api.get("/jobs/alert-settings").then((cfg) => {
      setAlertCfg(cfg);
      setCfgForm({ warning_pct: String(cfg.warning_pct), overrun_pct: String(cfg.overrun_pct), enabled: cfg.enabled });
    }).catch(() => {});
  }, []);
  useEffect(() => {
    load();
    loadNotifs();
    api.get("/customers").then(setCustomers);
    api.get("/products").then(setSupplies).catch(() => {});
    api.get("/auth/team").then(t => setTeam(t.filter(u => u.role === "barber")));
  }, [loadNotifs]);
  const markNotifRead = async id => {
    try {
      await api.post(`/jobs/notifications/${id}/read`);
      loadNotifs();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const saveAlertCfg = async () => {
    try {
      await api.put("/jobs/alert-settings", {
        warning_pct: parseFloat(cfgForm.warning_pct),
        overrun_pct: parseFloat(cfgForm.overrun_pct),
        enabled: cfgForm.enabled
      });
      toast.success("Budget alert settings saved");
      setCfgOpen(false);
      loadNotifs();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const createJob = async () => {
    try {
      await api.post("/jobs", {
        customer_name: form.customer_name,
        job_type: form.job_type,
        description: form.description,
        estimated_revenue: parseFloat(form.estimated_revenue) || 0,
        estimated_costs: Object.fromEntries(CATS.map(c => [c, parseFloat(form[c]) || 0])),
        retainage_pct: parseFloat(form.retainage_pct) || 0,
        retainage_payable_pct: form.retainage_payable_pct === "" ? null : parseFloat(form.retainage_payable_pct)
      });
      toast.success(t("jobs.estimateCreated"));
      setCreateOpen(false);
      setForm({
        customer_name: "",
        job_type: "installation",
        description: "",
        estimated_revenue: "",
        material: "",
        labor: "",
        equipment: "",
        subcontractor: "",
        other: ""
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const advance = async job => {
    const next = NEXT[job.status];
    if (!next) return;
    try {
      await api.patch(`/jobs/${job.id}`, {
        status: next
      });
      toast.success(`Status → ${next}`);
      openDetail(job.id);
      load();
      loadNotifs();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const addCost = async () => {
    try {
      const useStock = cost.category === "material" && cost.product_id;
      await api.post(`/jobs/${detail.id}/costs`, {
        category: cost.category,
        description: cost.description,
        amount: useStock ? 0 : parseFloat(cost.amount) || 0,
        hours: parseFloat(cost.hours) || 0,
        rate: parseFloat(cost.rate) || 0,
        technician_id: cost.technician_id || null,
        paid: cost.paid,
        product_id: useStock ? cost.product_id : null,
        qty: useStock ? parseInt(cost.qty) || 1 : 1,
        commitment_id: cost.category === "subcontractor" && cost.commitment_id ? cost.commitment_id : null,
        phase_id: cost.phase_id || null
      });
      toast.success(useStock ? "Material pulled from stock & posted to GL" : "Cost logged & posted to GL");
      setCost({
        category: "material",
        description: "",
        amount: "",
        hours: "",
        rate: "",
        technician_id: "",
        paid: false,
        product_id: "",
        qty: "1",
        commitment_id: "",
        phase_id: ""
      });
      openDetail(detail.id);
      load();
      loadNotifs();
      api.get("/products").then(setSupplies);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const addCo = async () => {
    try {
      await api.post(`/jobs/${detail.id}/change-orders`, {
        description: co.description, reason: co.reason,
        revenue_delta: parseFloat(co.revenue_delta) || 0,
        delay_days: parseInt(co.delay_days) || 0,
        cost_impact: Object.fromEntries(CATS.map((c) => [c, parseFloat(co[c]) || 0])),
      });
      toast.success("Change order submitted — pending approval (budget unchanged until approved)");
      setCo({ description: "", reason: "", revenue_delta: "", delay_days: "", material: "", labor: "", equipment: "", subcontractor: "", other: "" });
      openDetail(detail.id); load(); loadNotifs();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const decideCo = async (coId, action) => {
    try {
      await api.post(`/jobs/${detail.id}/change-orders/${coId}/${action}`);
      toast.success(action === "approve" ? "Change order approved — budget revised" : "Change order rejected");
      openDetail(detail.id); load(); loadNotifs();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const invoice = async () => {
    try {
      await api.post(`/jobs/${detail.id}/invoice`, {
        amount: parseFloat(invAmt) || 0
      });
      toast.success(t("jobs.jobInvoicedRevenueRecognized"));
      setInvAmt("");
      openDetail(detail.id);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!rows) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("jobs.jobs")} subtitle="Estimate, track costs, and see profit on every job" testid="jobs-page">
        {isOwner && <Button data-testid="btn-job-costing-settings" variant="outline" onClick={() => setSettingsOpen(true)} className="border-[#222838] text-gray-300 me-2"><Settings2 size={16} className="me-1" />Job Costing</Button>}
        {isOwner && <Button data-testid="btn-alert-settings" variant="outline" onClick={() => setCfgOpen(true)} className="border-[#222838] text-gray-300 me-2"><Settings2 size={16} className="me-1" />Budget Alerts{alertCfg ? ` (${alertCfg.warning_pct}% / ${alertCfg.overrun_pct}%)` : ""}</Button>}
        {isOwner && <Button data-testid="btn-new-job" onClick={() => setCreateOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="me-1" />{t("jobs.newJob")}</Button>}
      </PageHeader>

      {/* Job costing settings */}
      <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-xl max-h-[90vh] overflow-y-auto">
          <DialogHeader><DialogTitle>Job Costing Settings</DialogTitle></DialogHeader>
          {jc && <JobCostingSettings jc={jc} saveJc={saveJc} suggestion={suggestion} allTeam={allTeam} reloadJc={loadJc} />}


      {/* Budget alert notifications */}
      {notifs.length > 0 && (
        <Card className="mb-4 p-0 overflow-hidden" >
          <div data-testid="job-alert-notifications">
            {notifs.slice(0, 5).map((n) => (
              <div key={n.id} data-testid={`job-alert-${n.level}`}
                className={`flex items-center gap-3 px-4 py-2.5 border-b border-[#171c26] text-sm ${n.read ? "opacity-60" : ""} ${n.level === "overrun" ? "bg-rose-500/10" : "bg-amber-500/10"}`}>
                <BellRing size={15} className={n.level === "overrun" ? "text-rose-400 shrink-0" : "text-amber-400 shrink-0"} />
                <div className="flex-1 min-w-0">
                  <span className={`font-semibold ${n.level === "overrun" ? "text-rose-300" : "text-amber-300"}`}>{n.title}</span>
                  <span className="text-gray-400"> — {n.body}</span>
                </div>
                {!n.read && (
                  <Button size="sm" variant="ghost" data-testid="job-alert-mark-read" onClick={(e) => { e.stopPropagation(); markNotifRead(n.id); }} className="text-xs text-gray-400 shrink-0">Mark read</Button>
                )}
              </div>
            ))}
      {retRollup && (retRollup.receivable.outstanding_cents > 0 || retRollup.payable.outstanding_cents > 0) && (
        <Card className="mb-4 px-5 py-3" data-testid="retainage-rollup">
          <div className="flex flex-wrap gap-x-8 gap-y-1 text-sm">
            <span className="text-gray-400">Retainage held from us: <span className="font-mono text-amber-400">{money(retRollup.receivable.outstanding_cents)}</span> outstanding</span>
            <span className="text-gray-400">Retainage we hold from subs: <span className="font-mono text-amber-400">{money(retRollup.payable.outstanding_cents)}</span> outstanding</span>
            <span className="text-[11px] text-gray-500 self-center">Tracked here only — Bookkeeping does not hold, collect, or pay out retainage.</span>
          </div>
        </Card>
      )}

      {/* Alert settings dialog */}
      <Dialog open={cfgOpen} onOpenChange={setCfgOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-sm">
          <DialogHeader><DialogTitle>Budget Alert Settings</DialogTitle></DialogHeader>
          <div className="grid gap-3 text-sm">
            <p className="text-xs text-gray-500">Alerts fire once when a job's costs cross a threshold of its estimate. Warning fires while the job is active; overrun also fires on completed jobs.</p>
            <div><Label className="text-xs">Warning at (% of estimate)</Label>
              <Input data-testid="alert-warning-pct" type="number" value={cfgForm.warning_pct} onChange={(e) => setCfgForm({ ...cfgForm, warning_pct: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">Overrun at (% of estimate)</Label>
              <Input data-testid="alert-overrun-pct" type="number" value={cfgForm.overrun_pct} onChange={(e) => setCfgForm({ ...cfgForm, overrun_pct: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <label className="flex items-center gap-2 text-gray-300 text-xs">
              <input data-testid="alert-enabled" type="checkbox" checked={cfgForm.enabled} onChange={(e) => setCfgForm({ ...cfgForm, enabled: e.target.checked })} />
              Budget alert notifications on
            </label>
          </div>
          <DialogFooter><Button data-testid="alert-settings-save" onClick={saveAlertCfg} className="bg-amber-600 hover:bg-amber-500 text-black">Save</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Card className="p-0 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="table-jobs">
            <thead>
              <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-5 py-3 text-start font-medium">{t("jobs.job")}</th>
                <th className="px-5 py-3 text-start font-medium">{t("jobs.customer")}</th>
                <th className="px-5 py-3 text-start font-medium">{t("jobs.type")}</th>
                <th className="px-5 py-3 text-start font-medium">{t("jobs.status")}</th>
                <th className="px-5 py-3 text-end font-medium">{t("jobs.revenue")}</th>
                <th className="px-5 py-3 text-end font-medium">{t("jobs.cost")}</th>
                <th className="px-5 py-3 text-end font-medium">{t("jobs.grossProfit")}</th>
                <th className="px-5 py-3 text-end font-medium">{t("jobs.margin")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(j => <tr key={j.id} data-testid={`job-row-${j.job_number}`} className={`border-b border-[#171c26] hover:bg-[#161b25] cursor-pointer ${j.profit.is_unprofitable ? "bg-rose-500/5" : ""}`} onClick={() => openDetail(j.id)}>
                  <td className="px-5 py-3 font-mono text-amber-400">{j.job_number}</td>
                  <td className="px-5 py-3 text-gray-100">{j.customer_name}</td>
                  <td className="px-5 py-3 capitalize text-gray-300">{j.job_type}</td>
                  <td className="px-5 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[j.status]}`}>{j.status}</span>{j.co_summary?.pending_count > 0 && <span data-testid={`co-pending-chip-${j.job_number}`} className="ms-2 px-2 py-0.5 rounded-full text-xs border bg-amber-500/10 text-amber-400 border-amber-500/20">{j.co_summary.pending_count} CO waiting</span>}</td>
                  <td className="px-5 py-3 text-end font-mono text-gray-300">{dollars(j.profit.revenue)}</td>
<td className="px-5 py-3 text-end font-mono">
                    <span className={j.budget?.level === "overrun" ? "text-rose-400 font-semibold" : j.budget?.level === "warning" ? "text-amber-400 font-semibold" : "text-gray-400"}>{dollars(j.profit.cost)}</span>
                    {j.budget?.has_estimate && j.budget?.level && <span data-testid={`job-budget-badge-${j.job_number}`} className={`ms-2 inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] border ${j.budget.level === "overrun" ? "bg-rose-500/10 text-rose-400 border-rose-500/30" : "bg-amber-500/10 text-amber-400 border-amber-500/30"}`}><AlertTriangle size={10} />{j.budget.pct_used}% of estimate</span>}
                    {j.budget?.has_estimate && !j.budget?.level && <span className="ms-2 text-[10px] text-gray-600">{j.budget.pct_used}% of est.</span>}
                  </td>
                  <td className={`px-5 py-3 text-end font-mono ${j.profit.gross_profit < 0 ? "text-rose-400" : "text-emerald-400"}`}>{dollars(j.profit.gross_profit)}</td>
                  <td className="px-5 py-3 text-end font-mono flex items-center justify-end gap-1">
                    {j.profit.is_unprofitable && <AlertTriangle size={13} className="text-rose-400" />}
                    <span className={j.profit.gross_margin_pct < 0 ? "text-rose-400" : j.profit.gross_margin_pct < 20 ? "text-amber-400" : "text-emerald-400"}>{j.profit.gross_margin_pct}%</span>
                  </td>
                </tr>)}
              {rows.length === 0 && <tr><td colSpan={8} className="px-5 py-12 text-center text-gray-500">{t("jobs.noJobsYetCreateAnEstimate")}</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Create modal */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg">
          <DialogHeader><DialogTitle>{t("jobs.newJobEstimate")}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2"><Label className="text-xs">{t("jobs.customer")}</Label>
              <Input data-testid="job-customer" list="cust-list" value={form.customer_name} onChange={e => setForm({
              ...form,
              customer_name: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" placeholder={t("jobs.customerName")} />
              <datalist id="cust-list">{customers.map(c => <option key={c.id} value={c.name} />)}</datalist>
            </div>
            <div><Label className="text-xs">{t("jobs.type")}</Label>
              <Select value={form.job_type} onValueChange={v => setForm({
              ...form,
              job_type: v
            })}>
                <SelectTrigger data-testid="job-type" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                <SelectContent className="bg-[#14171E] border-[#222838]">
                  {["installation", "maintenance", "repair"].map(t => <SelectItem key={t} value={t} className="capitalize">{t}</SelectItem>)}
                </SelectContent>
              </Select></div>
            <div><Label className="text-xs">{t("jobs.estimatedRevenue")}</Label><Input data-testid="job-est-revenue" value={form.estimated_revenue} onChange={e => setForm({
              ...form,
              estimated_revenue: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div className="col-span-2"><Label className="text-xs">{t("jobs.description")}</Label><Input data-testid="job-description" value={form.description} onChange={e => setForm({
              ...form,
              description: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div className="col-span-2"><Label className="text-xs">Description</Label><Input data-testid="job-description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">Retainage held from us (%)</Label><Input data-testid="job-retainage-pct" value={form.retainage_pct} onChange={(e) => setForm({ ...form, retainage_pct: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" placeholder="0" /></div>
            <div><Label className="text-xs">Retainage we hold from subs (%)</Label><Input data-testid="job-retainage-payable-pct" value={form.retainage_payable_pct} onChange={(e) => setForm({ ...form, retainage_payable_pct: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" placeholder="Same as left" /></div>
            <div className="col-span-2 grid grid-cols-5 gap-2">
              {CATS.map(c => <div key={c}><Label className="text-[10px] capitalize text-gray-500">{c}</Label><Input data-testid={`est-${c}`} value={form[c]} onChange={e => setForm({
                ...form,
                [c]: e.target.value
              })} className="bg-[#0F1218] border-[#222838] font-mono text-xs h-8" placeholder="0" /></div>)}
            </div>
          </div>
          <DialogFooter><Button data-testid="job-save" onClick={createJob} className="bg-amber-600 hover:bg-amber-500 text-black">{t("jobs.createEstimate")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Detail modal */}
      <Dialog open={!!detail} onOpenChange={o => !o && setDetail(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-3xl max-h-[90vh] overflow-y-auto">
          {detail && <JobDetail detail={detail} isOwner={isOwner} team={team} advance={advance} refreshDetail={() => {
        openDetail(detail.id);
        load();
        loadNotifs();
      }} jc={jc} saveJc={saveJc} reloadDetail={() => openDetail(detail.id)} cost={cost} setCost={setCost} addCost={addCost} co={co} setCo={setCo} addCo={addCo} invAmt={invAmt} setInvAmt={setInvAmt} invoice={invoice} />}
        </DialogContent>
      </Dialog>
    </div>;
}
function JobDetail({
  detail,
  isOwner,
  team,
  advance,
  refreshDetail,
  jc,
  saveJc,
  reloadDetail,
  cost,
  setCost,
  addCost,
  co,
  setCo,
  addCo,
  invAmt,
  setInvAmt,
  invoice
}) {
  const {
    t
  } = useTranslation();
  const p = detail.profit;
  const b = detail.budget || {};
  const estCents = detail.estimated_costs_cents || {};
  const [ovr, setOvr] = useState({
    warning: detail.alert_warning_pct != null ? String(detail.alert_warning_pct) : "",
    overrun: detail.alert_overrun_pct != null ? String(detail.alert_overrun_pct) : "",
  });

  const saveOverride = async () => {
    try {
      await api.patch(`/jobs/${detail.id}/alert-thresholds`, {
        alert_warning_pct: ovr.warning === "" ? null : parseFloat(ovr.warning),
        alert_overrun_pct: ovr.overrun === "" ? null : parseFloat(ovr.overrun),
      });
      toast.success("Job alert thresholds saved");
      refreshDetail && refreshDetail();
    } catch (e) { toast.error(errMsg(e)); }
  };
    const view = jc?.labor_view || "base";
  const budget = detail.budget;
  const [preview, setPreview] = useState(null);
  const [cf, setCf] = useState({ vendor_name: "", description: "", amount: "" });

  // Live burdened-cost preview for the labor line, before it posts.
  useEffect(() => {
    if (cost.category !== "labor" || !(parseFloat(cost.hours) > 0 && parseFloat(cost.rate) > 0)) { setPreview(null); return; }
    const t = setTimeout(() => {
      api.post(`/jobs/${detail.id}/costs/preview`, {
        category: "labor", hours: parseFloat(cost.hours) || 0, rate: parseFloat(cost.rate) || 0,
        technician_id: cost.technician_id || null,
      }).then(setPreview).catch(() => setPreview(null));
    }, 350);
    return () => clearTimeout(t);
  }, [cost.category, cost.hours, cost.rate, cost.technician_id, detail.id]);

  const addCommitment = async () => {
    try {
      await api.post(`/job-costing/jobs/${detail.id}/commitments`, {
        vendor_name: cf.vendor_name, description: cf.description, amount: parseFloat(cf.amount) || 0,
      });
      toast.success("Commitment recorded — it counts against this job's budget");
      setCf({ vendor_name: "", description: "", amount: "" });
      reloadDetail();
    } catch (e) { toast.error(errMsg(e)); }
  };
  const cancelCommitment = async (cid) => {
    try { await api.post(`/job-costing/commitments/${cid}/cancel`, {}); toast.success("Commitment cancelled"); reloadDetail(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  // Labor figures per the tenant's view option (base / burdened / both).
  const laborActual = (cat) => {
    const base = p.actual_by_category_cents[cat] || 0;
    if (cat !== "labor") return money(base);
    const burdened = (p.burdened_by_category_cents || {})[cat] ?? base;
    if (view === "burdened") return money(burdened);
    if (view === "both") return `${money(base)} / ${money(burdened)}`;
    return money(base);
  };
    return <>
      <DialogHeader>
        <DialogTitle className="flex items-center gap-3 font-display">
          <Briefcase size={18} className="text-amber-400" />
          <span className="font-mono text-amber-400">{detail.job_number}</span>
          <span className="text-gray-200">{detail.customer_name}</span>
          <span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[detail.status]}`}>{detail.status}</span>
        </DialogTitle>
      </DialogHeader>
      <div className="text-sm text-gray-500 -mt-2 mb-2 capitalize">{detail.job_type} • {detail.service_location} • {detail.description}</div>

      {/* Profit summary */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[["Revenue", dollars(p.revenue), "text-gray-100"], ["Total Cost", dollars(p.cost), "text-gray-300"], ["Gross Profit", dollars(p.gross_profit), p.gross_profit < 0 ? "text-rose-400" : "text-emerald-400"], ["Margin", `${p.gross_margin_pct}%`, p.gross_margin_pct < 0 ? "text-rose-400" : p.gross_margin_pct < 20 ? "text-amber-400" : "text-emerald-400"]].map(([l, v, c]) => <div key={l} className="rounded-lg bg-[#0F1218] border border-[#222838] px-3 py-2" data-testid={`job-profit-${l.toLowerCase().replace(" ", "-")}`}>
            <div className="text-[10px] uppercase tracking-wider text-gray-500">{l}</div>
            <div className={`font-mono text-lg mt-0.5 ${c}`}>{v}</div>
          </div>)}
      </div>

      {isOwner && NEXT[detail.status] && <Button size="sm" onClick={() => advance(detail)} data-testid="btn-advance-status" className="w-fit bg-amber-600 hover:bg-amber-500 text-black mt-1">
          {NEXT_LABEL[detail.status]} <ArrowRight size={14} className="ms-1 rtl:-scale-x-100" />
        </Button>}

      {/* Budget watch banner — only when an estimate exists */}
      {b.has_estimate && b.level && (
        <div data-testid={`job-budget-banner-${b.level}`}
          className={`mt-2 flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${b.level === "overrun" ? "bg-rose-500/10 border-rose-500/30 text-rose-300" : "bg-amber-500/10 border-amber-500/30 text-amber-300"}`}>
          <AlertTriangle size={15} className="shrink-0" />
          <span>
            {b.level === "overrun"
              ? <>Over budget: costs are at <b>{b.pct_used}%</b> of the {money(b.estimated_cost_cents)} estimate ({money(b.actual_cost_cents)} spent).</>
              : <>Closing in on the estimate: costs are at <b>{b.pct_used}%</b> of the {money(b.estimated_cost_cents)} estimate ({money(b.actual_cost_cents)} spent).</>}
          </span>
        </div>
      )}
      {b.has_estimate && !b.level && (
        <div className="mt-2 text-xs text-gray-500" data-testid="job-budget-ok">
          Budget watch: costs at {b.pct_used}% of the {money(b.estimated_cost_cents)} estimate — alert at {b.warning_pct}%, overrun at {b.overrun_pct}%.
        </div>
      )}

      {/* Per-job threshold override */}
      {isOwner && (
        <div className="mt-2 flex items-end gap-2 text-xs text-gray-400" data-testid="job-threshold-override">
          <span className="pb-1 text-gray-500">Alert thresholds for this job (blank = use shop defaults):</span>
          <div><Label className="text-[10px]">Warn %</Label>
            <Input data-testid="job-alert-warning-pct" type="number" value={ovr.warning} onChange={(e) => setOvr({ ...ovr, warning: e.target.value })} placeholder={String(b.warning_pct ?? 90)} className="bg-[#0F1218] border-[#222838] h-8 w-20 font-mono text-xs" /></div>
          <div><Label className="text-[10px]">Overrun %</Label>
            <Input data-testid="job-alert-overrun-pct" type="number" value={ovr.overrun} onChange={(e) => setOvr({ ...ovr, overrun: e.target.value })} placeholder={String(b.overrun_pct ?? 100)} className="bg-[#0F1218] border-[#222838] h-8 w-20 font-mono text-xs" /></div>
          <Button size="sm" data-testid="job-alert-save" onClick={saveOverride} className="bg-[#222838] hover:bg-[#2b3247] text-gray-200 h-8">Save</Button>
        </div>
      )}

      {/* Labor view option — display only, never changes stored numbers */}
      {jc && (
        <div className="flex items-center gap-2 mt-1" data-testid="labor-view-toggle">
          <span className="text-[10px] uppercase tracking-wider text-gray-500">Labor view</span>
          {["base", "burdened", "both"].map((v) => (
            <button key={v} disabled={!isOwner} data-testid={`labor-view-${v}`}
              onClick={() => saveJc({ labor_view: v })}
              className={`px-2 py-0.5 rounded-full text-xs border capitalize ${view === v ? "bg-amber-500/15 text-amber-400 border-amber-500/30" : "text-gray-500 border-[#222838]"} ${isOwner ? "cursor-pointer" : "cursor-default"}`}>
              {v === "base" ? "Base wage" : v === "burdened" ? "Burdened" : "Both"}
            </button>
          ))}
          {view !== "base" && p.labor_burden_cents > 0 && (
            <span className="text-xs text-gray-500">burden on this job: {money(p.labor_burden_cents)}</span>
          )}
        </div>
      )}

      {/* Budget view — committed costs (optional feature) */}
      {budget?.enabled && (
        <div className="mt-2" data-testid="job-budget-view">
          <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Budget — Estimated / Committed / Actual / Remaining</h4>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {[["Estimated", money(budget.estimated_cost_cents), "text-gray-100"],
              ["Committed", money(budget.committed_cents), "text-sky-400"],
              ["Actual", money(budget.actual_total_cents), "text-gray-300"],
              ["Remaining", money(budget.remaining_cents), budget.remaining_cents < 0 ? "text-rose-400" : "text-emerald-400"]].map(([l, v, c]) => (
              <div key={l} className="rounded-lg bg-[#0F1218] border border-[#222838] px-3 py-2" data-testid={`budget-${l.toLowerCase()}`}>
                <div className="text-[10px] uppercase tracking-wider text-gray-500">{l}</div>
                <div className={`font-mono text-lg mt-0.5 ${c}`}>{v}</div>
              </div>
            ))}
          </div>
          <div className="text-xs text-gray-500 mt-2 space-y-0.5">
            <div>Committed: purchase orders {money(budget.committed_po_cents)} · subcontracts {money(budget.committed_subcontract_cents)}</div>
            <div>Actual: posted job costs {money(budget.actual_cost_cents)}{budget.billed_commitment_cents > 0 ? ` · billed POs ${money(budget.billed_commitment_cents)}` : ""}</div>
            {budget.purchase_orders?.map((po) => (
              <div key={po.po_id} className="flex justify-between max-w-md">
                <span>{po.number} — {po.vendor} <span className="text-gray-600 capitalize">({po.status.replace("_", " ")})</span></span>
                <span className="font-mono">{po.bucket === "committed" ? "committed " : "actual "}{money(po.amount_cents)}</span>
              </div>
            ))}
          </div>
          {/* Subcontract commitments */}
          <div className="mt-3 p-3 rounded-lg bg-[#0F1218] border border-[#222838]">
            <h5 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Subcontract Commitments</h5>
            {budget.commitments?.filter((c) => c.status !== "cancelled").map((c) => (
              <div key={c.id} className="flex items-center justify-between text-sm py-1 border-b border-[#171c26]">
                <span className="text-gray-300">{c.vendor_name}{c.description ? ` — ${c.description}` : ""}</span>
                <span className="font-mono text-gray-400 text-xs">
                  {money(c.amount_cents)} · fulfilled {money(c.fulfilled_cents || 0)} · open {money(c.amount_cents - (c.fulfilled_cents || 0))}
                  <span className="text-gray-600 capitalize"> · {c.status}</span>
                </span>
                {isOwner && c.status === "approved" && (
                  <Button size="sm" variant="ghost" className="h-6 text-xs" onClick={() => cancelCommitment(c.id)}>Cancel</Button>
                )}
              </div>
            ))}
            {isOwner && (
              <div className="flex gap-2 mt-2">
                <Input data-testid="commitment-vendor" value={cf.vendor_name} onChange={(e) => setCf({ ...cf, vendor_name: e.target.value })} placeholder="Subcontractor" className="bg-[#14171E] border-[#222838] h-8 text-xs" />
                <Input data-testid="commitment-description" value={cf.description} onChange={(e) => setCf({ ...cf, description: e.target.value })} placeholder="Scope (optional)" className="bg-[#14171E] border-[#222838] h-8 text-xs" />
                <Input data-testid="commitment-amount" value={cf.amount} onChange={(e) => setCf({ ...cf, amount: e.target.value })} placeholder="Amount ($)" className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono w-32" />
                <Button size="sm" data-testid="commitment-save" onClick={addCommitment} disabled={!cf.vendor_name || !cf.amount} className="bg-sky-600 hover:bg-sky-500 text-black h-8">Commit</Button>
              </div>
            )}
          </div>
        </div>
      )}

      {detail.field_capture && (
        <p className="text-xs text-gray-500 flex items-center gap-1.5 mt-1" data-testid="job-field-freshness">
          <Zap size={12} className="text-emerald-400 shrink-0" />
          Includes field entries through {new Date(detail.field_capture.last_field_cost_at).toLocaleString()}
          {detail.field_capture.includes_today ? " (today)" : ""} — job costs update the moment time is approved or a receipt posts, no office re-keying.
        </p>
      )}

      {/* Estimate vs actual */}
      <div className="mt-2">
        <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">{t("jobs.estimateVsActual")}</h4>
        <table className="w-full text-sm">
          <thead><tr className="text-[10px] uppercase text-gray-500 border-b border-[#222838]"><th className="py-1.5 text-start">{t("jobs.category")}</th><th className="py-1.5 text-end">{t("jobs.estimated")}</th><th className="py-1.5 text-end">{t("jobs.actual")}</th><th className="py-1.5 text-end">{t("jobs.variance")}</th></tr></thead>
          <tbody>
            {CATS.map(c => {
            const est = estCents[c] || 0;
            const act = p.actual_by_category_cents[c] || 0;
            const actShown = c === "labor" && view !== "base" ? ((p.burdened_by_category_cents || {})[c] ?? act) : act;
            const v = actShown - est;
            return <tr key={c} className="border-b border-[#171c26]">
                  <td className="py-1.5 capitalize text-gray-300">{c}{c === "labor" && view === "both" ? " (base / burdened)" : ""}</td>
                  <td className="py-1.5 text-end font-mono text-gray-500">{money(est)}</td>
                  <td className="py-1.5 text-end font-mono text-gray-200">{laborActual(c)}</td>
                  <td className={`py-1.5 text-end font-mono ${v > 0 ? "text-rose-400" : v < 0 ? "text-emerald-400" : "text-gray-500"}`}>{v > 0 ? "+" : ""}{money(v)}</td>
                </tr>;
          })}
          </tbody>
        </table>
      </div>

      {/* Phases / cost codes */}
      <PhaseSection detail={detail} isOwner={isOwner} refresh={refresh} />
          <RetainagePanel detail={detail} isOwner={isOwner} refreshDetail={refreshDetail} />

      {/* Add cost */}
      {isOwner && detail.status !== "estimate" && <div className="mt-2 p-3 rounded-lg bg-[#0F1218] border border-[#222838]">
          <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">{t("jobs.logACost")}</h4>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 items-end">
            <div><Label className="text-[10px]">{t("jobs.category")}</Label>
              <Select value={cost.category} onValueChange={v => setCost({
            ...cost,
            category: v
          })}>
                <SelectTrigger data-testid="cost-category" className="bg-[#14171E] border-[#222838] h-8 text-xs capitalize"><SelectValue /></SelectTrigger>
                <SelectContent className="bg-[#14171E] border-[#222838]">{CATS.map(c => <SelectItem key={c} value={c} className="capitalize">{c}</SelectItem>)}</SelectContent>
              </Select></div>
            <div className="sm:col-span-2"><Label className="text-[10px]">{t("jobs.description")}</Label><Input data-testid="cost-description" value={cost.description} onChange={e => setCost({
            ...cost,
            description: e.target.value
          })} className="bg-[#14171E] border-[#222838] h-8 text-xs" /></div>
            {cost.category === "labor" ? <div className="grid grid-cols-2 gap-1">
                <div><Label className="text-[10px]">{t("jobs.hrs")}</Label><Input data-testid="cost-hours" value={cost.hours} onChange={e => setCost({
              ...cost,
              hours: e.target.value
            })} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" /></div>
                <div><Label className="text-[10px]">{t("jobs.rate")}</Label><Input data-testid="cost-rate" value={cost.rate} onChange={e => setCost({
              ...cost,
              rate: e.target.value
            })} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" /></div>
              </div> : <div><Label className="text-[10px]">{t("jobs.amount")}</Label><Input data-testid="cost-amount" value={cost.amount} onChange={e => setCost({
            ...cost,
            amount: e.target.value
          })} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" /></div>}
            {cost.category === "labor" && <div className="sm:col-span-2"><Label className="text-[10px]">{t("jobs.technician")}</Label>
                <Select value={cost.technician_id} onValueChange={v => setCost({
            ...cost,
            technician_id: v
          })}>
                  <SelectTrigger data-testid="cost-technician" className="bg-[#14171E] border-[#222838] h-8 text-xs"><SelectValue placeholder={t("jobs.assign")} /></SelectTrigger>
                  <SelectContent className="bg-[#14171E] border-[#222838]">{team.map(t => <SelectItem key={t.id} value={t.id}>{t.name}</SelectItem>)}</SelectContent>
                </Select></div>}
            {cost.category === "subcontractor" && budget?.enabled && <div className="sm:col-span-2"><Label className="text-[10px]">Fulfils commitment (optional)</Label>
                <Select value={cost.commitment_id || "none"} onValueChange={v => setCost({
            ...cost,
            commitment_id: v === "none" ? "" : v
          })}>
                  <SelectTrigger data-testid="cost-commitment" className="bg-[#14171E] border-[#222838] h-8 text-xs"><SelectValue placeholder="None" /></SelectTrigger>
                  <SelectContent className="bg-[#14171E] border-[#222838]">
                    <SelectItem value="none">None</SelectItem>
                    {budget.commitments?.filter(c => c.status === "approved").map(c => <SelectItem key={c.id} value={c.id}>{c.vendor_name} — {money(c.amount_cents - (c.fulfilled_cents || 0))} open</SelectItem>)}
                  </SelectContent>
                </Select></div>}
            {(detail.phases || []).length > 0 && <div><Label className="text-[10px]">Phase (optional)</Label>
                <Select value={cost.phase_id || "none"} onValueChange={v => setCost({
            ...cost,
            phase_id: v === "none" ? "" : v
          })}>
                  <SelectTrigger data-testid="cost-phase" className="bg-[#14171E] border-[#222838] h-8 text-xs"><SelectValue placeholder="No phase" /></SelectTrigger>
                  <SelectContent className="bg-[#14171E] border-[#222838]">
                    <SelectItem value="none">No phase</SelectItem>
                    {(detail.phases || []).map(ph => <SelectItem key={ph.id} value={ph.id}>{ph.name}</SelectItem>)}
                  </SelectContent>
                </Select></div>}
            <Button size="sm" data-testid="cost-save" onClick={addCost} className="bg-emerald-600 hover:bg-emerald-500 text-black h-8">{t("jobs.logCost")}</Button>
          </div>
                  {cost.category === "labor" && preview && <div className="mt-2 text-xs" data-testid="labor-cost-preview">
              <span className="text-gray-400">Base wage {money(preview.base_cents)}</span>
              {preview.burden_cents > 0 ? <span className="text-gray-400"> · burden +{preview.burden_pct}%{preview.burden_template_name ? ` (${preview.burden_template_name})` : ""} {money(preview.burden_cents)} · <span className="text-amber-400 font-mono">true cost {money(preview.burdened_cents)}</span></span> : <span className="text-gray-600"> · no burden rate configured</span>}
              {preview.burden_would_tip_over && <span className="text-rose-400 flex items-center gap-1 mt-1"><AlertTriangle size={12} /> Burdened, this line pushes the job over its estimate — you'll be flagged when it posts.</span>}
            </div>}
        </div>}

      {/* Costs list */}
      {detail.costs?.length > 0 && <div className="mt-2">
          <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">{t("jobs.loggedCosts")}</h4>
          <table className="w-full text-sm" data-testid="table-job-costs">
            <tbody>
              {detail.costs.map(c => <tr key={c.id} className="border-b border-[#171c26]">
                  <td className="py-1.5 capitalize text-gray-400 w-28">{c.category}</td>
                  <td className="py-1.5 text-gray-300">{c.description || "—"}{c.phase_id && <span className="ms-2 px-1.5 py-0.5 rounded text-[10px] bg-sky-500/10 text-sky-400 border border-sky-500/20">{(detail.phases || []).find(ph => ph.id === c.phase_id)?.name || "phase"}</span>}
                    {c.category === "labor" && c.burden_cents > 0 && (
                      <span className="block text-[11px] text-gray-500">
                        {view === "base" && <>burden +{c.burden_pct}% = {money(c.burdened_cents)} true cost</>}
                        {view === "burdened" && <>base wage {money(c.amount_cents)} · burden {money(c.burden_cents)}</>}
                        {view === "both" && <>base {money(c.amount_cents)} + burden {money(c.burden_cents)} = {money(c.burdened_cents)}</>}
                      </span>
                    )}
                    {c.source === "time_entry" && <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300">field · time</span>}
                    {c.source === "receipt" && <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded-full bg-violet-500/15 text-violet-300">field · receipt</span>}
                    {c.burdened_amount_cents != null && <span className="ml-2 text-[10px] text-gray-500">burdened {money(c.burdened_amount_cents)}</span>}
                  </td>
                  <td className="py-1.5 font-mono text-amber-400 w-32">{c.entry_number}</td>
                  <td className="py-1.5 text-end font-mono text-gray-200">{c.category === "labor" && view === "burdened" && c.burdened_cents != null ? money(c.burdened_cents) : money(c.amount_cents)}</td>
                </tr>)}
            </tbody>
          </table>
        </div>}

      {/* Pending change orders — visible block */}
      {pendingCos.length > 0 && (
        <div className="mt-2 p-3 rounded-lg bg-amber-500/5 border border-amber-500/30" data-testid="co-pending-block">
          <h4 className="text-xs uppercase tracking-wider text-amber-400 mb-2">
            {pendingCos.length} change order{pendingCos.length > 1 ? "s" : ""} waiting — {money(sum.pending_revenue_cents || 0)} pending revenue · {money(sum.pending_cost_cents || 0)} pending cost
          </h4>
          <div className="space-y-2">
            {pendingCos.map((c) => (
              <div key={c.id} className="flex items-center justify-between gap-2 text-sm">
                <div className="text-gray-200">{c.description}
                  <span className="text-gray-500 text-xs"> · rev {money(c.revenue_delta_cents)} · cost {money(c.cost_delta_cents)}{c.reason ? ` · ${c.reason}` : ""}</span>
                </div>
                {isOwner && detail.status !== "estimate" && (
                  <div className="flex gap-1 shrink-0">
                    <Button size="sm" data-testid={`co-approve-${c.id}`} onClick={() => decideCo(c.id, "approve")} className="bg-emerald-600 hover:bg-emerald-500 text-black h-7 text-xs">Approve</Button>
                    <Button size="sm" data-testid={`co-reject-${c.id}`} onClick={() => decideCo(c.id, "reject")} className="bg-rose-600/80 hover:bg-rose-500 text-black h-7 text-xs">Reject</Button>
                  </div>
                )}
              </div>
            ))}
          </div>
          <div className="text-[11px] text-gray-500 mt-1">Pending change orders do not change the job budget. Approving revises the estimate the same day.</div>
        </div>
      )}

      {/* Change order history */}
      {cos.length > 0 && (
        <div className="mt-2" data-testid="co-history">
          <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Change Order History</h4>
          <div className="space-y-2">
            {cos.map((c) => (
              <div key={c.id} className="p-2.5 rounded-lg bg-[#0F1218] border border-[#222838] text-sm">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-gray-200">{c.description}</span>
                  <span className={`px-2 py-0.5 rounded-full text-[11px] border capitalize ${CO_CLS[c.state] || CO_CLS.pending}`}>{c.state}</span>
                </div>
                <div className="text-xs text-gray-400 mt-1">
                  Revenue {money(c.revenue_delta_cents)} · Cost {money(c.cost_delta_cents)}
                  {c.delay_days ? ` · +${c.delay_days} day${c.delay_days > 1 ? "s" : ""}` : ""}
                  {c.reason ? ` · Reason: ${c.reason}` : ""}
                </div>
                {c.cost_impact_cents && Object.keys(c.cost_impact_cents).length > 0 && (
                  <div className="text-xs text-gray-500 mt-0.5">Cost impact: {Object.entries(c.cost_impact_cents).map(([k, v]) => `${k} ${money(v)}`).join(" · ")}</div>
                )}
                <div className="text-[11px] text-gray-500 mt-1 space-y-0.5">
                  {(c.history || []).map((h, i) => (
                    <div key={i}>{h.event} {h.by_name ? `by ${h.by_name}` : ""} {h.at ? `· ${fmtDate(h.at.slice(0, 10))}` : ""}</div>
                  ))}
                </div>
              </div>
            ))}
          </div>
      {detail.status !== "estimate" && (
        <AiaBilling job={detail} isOwner={isOwner} onRefresh={onRefresh} />
      )}

      {/* Costs list */}
      {detail.costs?.length > 0 && (
        <div className="mt-2">
          <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Logged Costs</h4>
          <table className="w-full text-sm" data-testid="table-job-costs">
            <tbody>
              {detail.costs.map((c) => (
                <tr key={c.id} className="border-b border-[#171c26]">
                  <td className="py-1.5 capitalize text-gray-400 w-28">{c.category}</td>
                  <td className="py-1.5 text-gray-300">{c.description || "—"}</td>
                  <td className="py-1.5 font-mono text-amber-400 w-32">{c.entry_number}</td>
                  <td className="py-1.5 text-right font-mono text-gray-200">{money(c.amount_cents)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Change order + invoice */}
      {isOwner && detail.status !== "estimate" && <div className="grid sm:grid-cols-2 gap-3 mt-2">
          <div className="p-3 rounded-lg bg-[#0F1218] border border-[#222838]">
            <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">{t("jobs.changeOrder")}</h4>
            <Input data-testid="co-description" value={co.description} onChange={e => setCo({
          ...co,
          description: e.target.value
        })} placeholder={t("jobs.description")} className="bg-[#14171E] border-[#222838] h-8 text-xs mb-2" />
            <Input data-testid="co-reason" value={co.reason} onChange={e => setCo({
          ...co,
          reason: e.target.value
        })} placeholder="Reason (customer request, site condition…)" className="bg-[#14171E] border-[#222838] h-8 text-xs mb-2" />
            <div className="flex gap-2">
              <Input data-testid="co-revenue" value={co.revenue_delta} onChange={e => setCo({
            ...co,
            revenue_delta: e.target.value
          })} placeholder={t("jobs.revenue2")} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" />
              <Input data-testid="co-cost" value={co.cost_delta} onChange={e => setCo({
            ...co,
            cost_delta: e.target.value
          })} placeholder={t("jobs.cost2")} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" />
              <Input data-testid="co-delay" value={co.delay_days} onChange={e => setCo({
            ...co,
            delay_days: e.target.value
          })} placeholder="Delay days" className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" />
              <Button size="sm" data-testid="co-save" onClick={addCo} className="bg-amber-600 hover:bg-amber-500 text-black h-8">{t("jobs.add")}</Button>
            </div>
            <div className="grid grid-cols-5 gap-1 mb-2">
              {CATS.map((c) => (
                <div key={c}><Label className="text-[10px] capitalize text-gray-500">{c}</Label><Input data-testid={`co-impact-${c}`} value={co[c]} onChange={(e) => setCo({ ...co, [c]: e.target.value })} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" placeholder="0" /></div>
              ))}
            </div>
            <Button size="sm" data-testid="co-save" onClick={addCo} className="bg-amber-600 hover:bg-amber-500 text-black h-8">Submit for Approval</Button>
          </div>
          <div className="p-3 rounded-lg bg-[#0F1218] border border-[#222838]">
            <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">{t("jobs.invoiceCustomerRecognizeRevenue")}</h4>
            <div className="flex gap-2">
              <Input data-testid="job-invoice-amount" value={invAmt} onChange={e => setInvAmt(e.target.value)} placeholder={t("jobs.amount")} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" />
              <Button size="sm" data-testid="job-invoice-save" onClick={invoice} className="bg-emerald-600 hover:bg-emerald-500 text-black h-8">{t("jobs.invoice")}</Button>
            </div>
            {detail.invoices?.length > 0 && <div className="mt-2 text-xs text-gray-500">{detail.invoices.length} {t("jobs.invoiceS")} {money(detail.invoices.reduce((s, i) => s + i.subtotal_cents, 0))} {t("jobs.billed")}</div>}
          </div>
        </div>}
    </>;
}

function JobCostingSettings({ jc, saveJc, suggestion, allTeam, reloadJc }) {
  const [defaultPct, setDefaultPct] = useState(String(jc.default_burden_pct ?? 0));
  const [tpl, setTpl] = useState({ name: "", kind: "employee", pct: "", member_user_ids: [] });

  const toggleMember = (id) =>
    setTpl((t) => ({ ...t, member_user_ids: t.member_user_ids.includes(id)
      ? t.member_user_ids.filter((m) => m !== id) : [...t.member_user_ids, id] }));

  const createTemplate = async () => {
    try {
      await api.post("/job-costing/burden-templates", { ...tpl, pct: parseFloat(tpl.pct) || 0 });
      toast.success("Burden template saved");
      setTpl({ name: "", kind: "employee", pct: "", member_user_ids: [] });
      reloadJc();
    } catch (e) { toast.error(errMsg(e)); }
  };
  const deleteTemplate = async (id) => {
    try { await api.del(`/job-costing/burden-templates/${id}`); toast.success("Template deleted"); reloadJc(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  return (
    <div className="space-y-5 text-sm">
      {/* Committed costs — optional, owner-ruled */}
      <div className="p-3 rounded-lg bg-[#0F1218] border border-[#222838]" data-testid="jc-committed-toggle">
        <label className="flex items-start gap-3 cursor-pointer">
          <input type="checkbox" className="mt-1" checked={!!jc.committed_costs_enabled}
            onChange={(e) => saveJc({ committed_costs_enabled: e.target.checked })} />
          <span>
            <span className="text-gray-100 font-medium">Committed costs</span>
            <span className="block text-xs text-gray-500 mt-0.5">
              Off by default — not every shop runs purchase orders. When on, approved POs linked to a job and
              signed subcontract commitments count against the job's budget before the bills land
              (estimated / committed / actual / remaining).
            </span>
          </span>
        </label>
      </div>

      {/* Burdened labor */}
      <div className="p-3 rounded-lg bg-[#0F1218] border border-[#222838]">
        <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Burdened Labor — true cost of an hour</h4>
        {suggestion?.suggested_burden_pct != null && (
          <div className="mb-3 p-2 rounded bg-amber-500/10 border border-amber-500/20 text-xs" data-testid="jc-burden-suggestion">
            <span className="text-amber-300">Your real burden from {suggestion.basis} was {suggestion.suggested_burden_pct}%</span>
            <span className="text-gray-400"> — {suggestion.note} </span>
            <button className="text-amber-400 underline" data-testid="jc-burden-apply"
              onClick={() => { setDefaultPct(String(suggestion.suggested_burden_pct)); saveJc({ default_burden_pct: suggestion.suggested_burden_pct }); }}>
              Use {suggestion.suggested_burden_pct}% as the default
            </button>
          </div>
        )}
        <div className="flex items-end gap-2">
          <div><Label className="text-[10px]">Default burden % (taxes, comp, insurance, benefits)</Label>
            <Input data-testid="jc-default-burden" value={defaultPct} onChange={(e) => setDefaultPct(e.target.value)} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono w-32" /></div>
          <Button size="sm" className="h-8 bg-amber-600 hover:bg-amber-500 text-black"
            onClick={() => saveJc({ default_burden_pct: parseFloat(defaultPct) || 0 })}>Save rate</Button>
        </div>

        {/* Templates */}
        <h5 className="text-[11px] uppercase tracking-wider text-gray-500 mt-4 mb-2">Burden templates — per crew or worker</h5>
        {(jc.templates || []).map((t) => (
          <div key={t.id} className="flex items-center justify-between py-1.5 border-b border-[#171c26]">
            <span className="text-gray-200">{t.name} <span className="text-gray-600 capitalize">({t.kind})</span></span>
            <span className="font-mono text-gray-400 text-xs">{t.pct}% · {(t.member_user_ids || []).length} member(s)</span>
            <Button size="sm" variant="ghost" className="h-6" onClick={() => deleteTemplate(t.id)}><Trash2 size={13} className="text-rose-400" /></Button>
          </div>
        ))}
        <div className="grid grid-cols-3 gap-2 mt-3">
          <div><Label className="text-[10px]">Name</Label><Input data-testid="tpl-name" value={tpl.name} onChange={(e) => setTpl({ ...tpl, name: e.target.value })} placeholder="Employees" className="bg-[#14171E] border-[#222838] h-8 text-xs" /></div>
          <div><Label className="text-[10px]">Kind</Label>
            <Select value={tpl.kind} onValueChange={(v) => setTpl({ ...tpl, kind: v })}>
              <SelectTrigger className="bg-[#14171E] border-[#222838] h-8 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838]">
                <SelectItem value="employee">Employees</SelectItem>
                <SelectItem value="sub">Subcontractors</SelectItem>
                <SelectItem value="custom">Custom</SelectItem>
              </SelectContent>
            </Select></div>
          <div><Label className="text-[10px]">Burden %</Label><Input data-testid="tpl-pct" value={tpl.pct} onChange={(e) => setTpl({ ...tpl, pct: e.target.value })} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" /></div>
        </div>
        {allTeam.length > 0 && (
          <div className="flex flex-wrap gap-2 mt-2">
            {allTeam.map((u) => (
              <label key={u.id} className="flex items-center gap-1 text-xs text-gray-400 cursor-pointer">
                <input type="checkbox" checked={tpl.member_user_ids.includes(u.id)} onChange={() => toggleMember(u.id)} /> {u.name}
              </label>
            ))}
          </div>
        )}
        <Button size="sm" data-testid="tpl-save" onClick={createTemplate} disabled={!tpl.name || !tpl.pct} className="mt-3 bg-sky-600 hover:bg-sky-500 text-black h-8">Add template</Button>
      </div>

      <p className="text-[11px] text-gray-600">
        Labor view (base wage / burdened / both, side by side) is set per job from the job detail — it's a display
        choice and never changes stored numbers. Burden is a costing overlay: journal entries still post base wages.
      </p>
    </div>
  );
}

const PHASE_LEVEL = {
  overrun: ["Over budget", "bg-rose-500/10 text-rose-400 border-rose-500/20"],
  warning: ["Closing in", "bg-amber-500/10 text-amber-400 border-amber-500/20"],
  drift: ["Running hot", "bg-orange-500/10 text-orange-400 border-orange-500/20"],
};

function PhaseSection({ detail, isOwner, refresh }) {
  const pv = detail.phase_variance;
  const [phForm, setPhForm] = useState({ name: "", material: "", labor: "", equipment: "", subcontractor: "", other: "" });
  if (!pv) return null;
  const check = pv.budget_check || {};
  const hasPhases = (pv.phases || []).length > 0;

  const addPhase = async () => {
    try {
      const r = await api.post(`/jobs/${detail.id}/phases`, {
        name: phForm.name,
        budget: Object.fromEntries(CATS.map((c) => [c, parseFloat(phForm[c]) || 0])),
      });
      if (r.budget_check?.warning) toast.warning(r.budget_check.warning);
      else toast.success("Phase added");
      setPhForm({ name: "", material: "", labor: "", equipment: "", subcontractor: "", other: "" });
      refresh();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const delPhase = async (phaseId) => {
    try { await api.delete(`/jobs/${detail.id}/phases/${phaseId}`); toast.success("Phase removed"); refresh(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  return (
    <div className="mt-4" data-testid="job-phases">
      <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Phases / Cost Codes</h4>
      {check.warning && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 mb-2 text-xs text-amber-300" data-testid="phase-budget-warning">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" /><span>{check.warning}</span>
        </div>
      )}
      {hasPhases ? (
        <table className="w-full text-sm" data-testid="table-job-phases">
          <thead><tr className="text-[10px] uppercase text-gray-500 border-b border-[#222838]">
            <th className="py-1.5 text-left">Phase</th><th className="py-1.5 text-right">Budget</th>
            <th className="py-1.5 text-right">Committed</th><th className="py-1.5 text-right">Actual</th>
            <th className="py-1.5 text-right">% Used</th><th className="py-1.5 text-right">Status</th>
            {isOwner && <th className="py-1.5"></th>}
          </tr></thead>
          <tbody>
            {pv.phases.map((ph) => {
              const lvl = ph.level ? PHASE_LEVEL[ph.level] : null;
              return (
                <tr key={ph.phase_id} className={`border-b border-[#171c26] ${ph.level === "overrun" ? "bg-rose-500/5" : ""}`} data-testid={`phase-row-${ph.phase_id}`}>
                  <td className="py-1.5 text-gray-200">{ph.name}</td>
                  <td className="py-1.5 text-right font-mono text-gray-500">{money(ph.budget_cents)}</td>
                  <td className="py-1.5 text-right font-mono text-gray-500">{money(ph.committed_cents)}</td>
                  <td className="py-1.5 text-right font-mono text-gray-200">{money(ph.actual_cents)}</td>
                  <td className={`py-1.5 text-right font-mono ${ph.pct_used == null ? "text-gray-600" : ph.pct_used >= 100 ? "text-rose-400" : ph.pct_used >= 90 ? "text-amber-400" : "text-gray-300"}`}>{ph.pct_used == null ? "—" : `${ph.pct_used}%`}</td>
                  <td className="py-1.5 text-right">{lvl ? <span className={`px-2 py-0.5 rounded-full text-[10px] border ${lvl[1]}`}>{lvl[0]}</span> : <span className="text-gray-600 text-xs">On track</span>}</td>
                  {isOwner && <td className="py-1.5 text-right"><button data-testid={`phase-del-${ph.phase_id}`} onClick={() => delPhase(ph.phase_id)} className="text-gray-600 hover:text-rose-400 text-xs">✕</button></td>}
                </tr>
              );
            })}
            {pv.unphased?.actual_cents > 0 && (
              <tr className="border-b border-[#171c26]">
                <td className="py-1.5 text-gray-500 italic">No phase ({pv.unphased.cost_count} cost{pv.unphased.cost_count === 1 ? "" : "s"})</td>
                <td className="py-1.5 text-right font-mono text-gray-600">—</td>
                <td className="py-1.5 text-right font-mono text-gray-600">—</td>
                <td className="py-1.5 text-right font-mono text-gray-400">{money(pv.unphased.actual_cents)}</td>
                <td className="py-1.5 text-right font-mono text-gray-600">—</td>
                <td></td>{isOwner && <td></td>}
              </tr>
            )}
          </tbody>
        </table>
      ) : (
        <div className="text-xs text-gray-600 mb-2">No phases yet — split this job into phases (rough-in, trim-out…) to see which one is making or losing money.</div>
      )}
      {isOwner && (
        <div className="mt-2 p-3 rounded-lg bg-[#0F1218] border border-[#222838]">
          <h5 className="text-[10px] uppercase tracking-wider text-gray-500 mb-2">Add a Phase — budget drawn from the job estimate</h5>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 items-end">
            <div className="sm:col-span-2"><Label className="text-[10px]">Phase name</Label>
              <Input data-testid="phase-name" value={phForm.name} onChange={(e) => setPhForm({ ...phForm, name: e.target.value })} placeholder="e.g. Rough-in" className="bg-[#14171E] border-[#222838] h-8 text-xs" /></div>
            {CATS.map((c) => (
              <div key={c}><Label className="text-[10px] capitalize">{c} $</Label>
                <Input data-testid={`phase-budget-${c}`} value={phForm[c]} onChange={(e) => setPhForm({ ...phForm, [c]: e.target.value })} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" placeholder="0" /></div>
            ))}
            <Button size="sm" data-testid="phase-save" onClick={addPhase} className="bg-sky-600 hover:bg-sky-500 text-black h-8">Add Phase</Button>
          </div>
        </div>
      )}
    </div>
  );
}

function RetainagePanel({ detail, isOwner }) {
  const [data, setData] = useState(null);
  const [amounts, setAmounts] = useState({});
  const [form, setForm] = useState(null);

  const load = useCallback(() => {
    api.get(`/retainage/jobs/${detail.id}`).then((d) => {
      setData(d);
      setForm({
        retainage_pct: d.retainage_pct ?? 0,
        retainage_payable_pct: d.retainage_payable_pct ?? "",
        retainage_release_days: detail.retainage_release_days ?? "",
        retainage_release_due_date: detail.retainage_release_due_date ?? "",
        substantial_completion_date: detail.substantial_completion_date ?? "",
        retainage_release_note: detail.retainage_release_note ?? "",
      });
    }).catch(() => setData(null));
  }, [detail.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [load]);

  if (!data) return null;
  const hasRetainage = (detail.retainage_pct || 0) > 0 || data.entries.length > 0;
  if (!hasRetainage && !isOwner) return null;

  const mark = async (entry) => {
    try {
      const amt = amounts[entry.id];
      await api.post(`/retainage/entries/${entry.id}/mark-released`, {
        amount: amt ? parseFloat(amt) : null, note: "",
      });
      toast.success("Release recorded — Bookkeeping did not move any money");
      setAmounts((a) => ({ ...a, [entry.id]: "" }));
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const saveSettings = async () => {
    try {
      const body = {
        retainage_pct: parseFloat(form.retainage_pct) || 0,
        retainage_payable_pct: form.retainage_payable_pct === "" ? null : parseFloat(form.retainage_payable_pct),
        retainage_release_days: form.retainage_release_days === "" ? null : parseInt(form.retainage_release_days),
        retainage_release_due_date: form.retainage_release_due_date || null,
        substantial_completion_date: form.substantial_completion_date || null,
        retainage_release_note: form.retainage_release_note || null,
      };
      const d = await api.post(`/retainage/jobs/${detail.id}/settings`, body);
      setData(d);
      toast.success("Retainage settings saved");
    } catch (e) { toast.error(errMsg(e)); }
  };

  const sideBlock = (label, s) => (
    <div className="rounded-lg bg-[#14171E] border border-[#222838] px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-gray-500">{label}</div>
      <div className="font-mono text-lg mt-0.5 text-amber-400">{money(s.outstanding_cents)} <span className="text-xs text-gray-500">still held</span></div>
      <div className="text-[11px] text-gray-500 font-mono">held {money(s.held_cents)} · released {money(s.released_cents)}</div>
    </div>
  );

  return (
    <div className="mt-2 p-3 rounded-lg bg-[#0F1218] border border-[#222838]" data-testid="retainage-panel">
      <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-1">Retainage</h4>
      <p className="text-[11px] text-gray-500 mb-2">
        Tracked here only. Bookkeeping does not hold, collect, or pay out retainage — when a release
        happens outside the app, record it below so the books show what's still held.
        {data.release_due_date && <> Release due: <span className="text-amber-400 font-mono">{data.release_due_date}</span></>}
      </p>
      {data.release_due_date && data.release_due_date <= new Date().toISOString().slice(0, 10) &&
        ((data.receivable?.outstanding_cents || 0) + (data.payable?.outstanding_cents || 0)) > 0 && (
        <div className="mb-2 p-2 rounded bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs" data-testid="retainage-due-banner">
          Release due — {data.release_due_date} has passed with {money((data.receivable?.outstanding_cents || 0) + (data.payable?.outstanding_cents || 0))} still held.
          A due reminder has been recorded in your notifications. Bookkeeping does not collect or pay retainage —
          record the release below once it happens outside the app.
        </div>
      )}
      <div className="grid grid-cols-2 gap-3">
        {sideBlock("Held from us by the customer", data.receivable)}
        {sideBlock("Held by us from subs", data.payable)}
      </div>

      {data.entries.length > 0 && (
        <table className="w-full text-sm mt-2" data-testid="table-retainage-entries">
          <tbody>
            {data.entries.map((e) => (
              <tr key={e.id} className="border-b border-[#171c26]">
                <td className="py-1.5 text-gray-400 w-24 capitalize">{e.side === "receivable" ? "From us" : "From sub"}</td>
                <td className="py-1.5 text-gray-300">{e.counterparty || "—"}{e.phase_id ? <span className="text-gray-500"> · phase {e.phase_id}</span> : ""}</td>
                <td className="py-1.5 text-right font-mono text-gray-400">held {money(e.held_cents)}</td>
                <td className="py-1.5 text-right font-mono text-amber-400 w-32">still held {money(e.outstanding_cents)}</td>
                <td className="py-1.5 text-right w-56">
                  {isOwner && e.outstanding_cents > 0 && (
                    <span className="inline-flex gap-1 items-center">
                      <Input data-testid={`ret-amount-${e.id}`} value={amounts[e.id] || ""} onChange={(ev) => setAmounts((a) => ({ ...a, [e.id]: ev.target.value }))} placeholder="$ (blank = all)" className="bg-[#14171E] border-[#222838] h-7 text-xs font-mono w-28" />
                      <Button size="sm" data-testid={`ret-mark-${e.id}`} onClick={() => mark(e)} className="bg-sky-600 hover:bg-sky-500 text-black h-7 text-xs">Record release</Button>
                    </span>
                  )}
                  {e.outstanding_cents <= 0 && <span className="text-xs text-emerald-400">Released</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {isOwner && form && (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 mt-3 items-end">
          <div><Label className="text-[10px]">Held from us (%)</Label><Input data-testid="ret-set-pct" value={form.retainage_pct} onChange={(e) => setForm({ ...form, retainage_pct: e.target.value })} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" /></div>
          <div><Label className="text-[10px]">Held from subs (%)</Label><Input data-testid="ret-set-payable-pct" value={form.retainage_payable_pct} onChange={(e) => setForm({ ...form, retainage_payable_pct: e.target.value })} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" /></div>
          <div><Label className="text-[10px]">Release due date</Label><Input data-testid="ret-set-due" type="date" value={form.retainage_release_due_date} onChange={(e) => setForm({ ...form, retainage_release_due_date: e.target.value })} className="bg-[#14171E] border-[#222838] h-8 text-xs" /></div>
          <div><Label className="text-[10px]">Substantial completion</Label><Input data-testid="ret-set-completion" type="date" value={form.substantial_completion_date} onChange={(e) => setForm({ ...form, substantial_completion_date: e.target.value })} className="bg-[#14171E] border-[#222838] h-8 text-xs" /></div>
          <div><Label className="text-[10px]">…or days after completion</Label><Input data-testid="ret-set-days" value={form.retainage_release_days} onChange={(e) => setForm({ ...form, retainage_release_days: e.target.value })} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" /></div>
          <div><Label className="text-[10px]">Release condition (in words)</Label><Input data-testid="ret-set-note" value={form.retainage_release_note} onChange={(e) => setForm({ ...form, retainage_release_note: e.target.value })} className="bg-[#14171E] border-[#222838] h-8 text-xs" placeholder="e.g. 30 days after punch list" /></div>
          <Button size="sm" data-testid="ret-set-save" onClick={saveSettings} className="bg-amber-600 hover:bg-amber-500 text-black h-8">Save Retainage Settings</Button>
        </div>
      )}
    </div>
  );
}
