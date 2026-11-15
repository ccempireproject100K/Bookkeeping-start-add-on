import { useEffect, useState, useCallback } from "react";
import { api, errMsg } from "@/lib/api";
import { money, dollars, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Plus, AlertTriangle, ArrowRight, Briefcase, BellRing, Settings2 } from "lucide-react";
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
  const [notifs, setNotifs] = useState([]);
  const [alertCfg, setAlertCfg] = useState(null);
  const [cfgForm, setCfgForm] = useState({
    warning_pct: "90",
    overrun_pct: "100",
    enabled: true
  });
  const [cfgOpen, setCfgOpen] = useState(false);
  const [form, setForm] = useState({
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
  const [cost, setCost] = useState({
    category: "material",
    description: "",
    amount: "",
    hours: "",
    rate: "",
    technician_id: "",
    paid: false,
    product_id: "",
    qty: "1"
  });
  const [co, setCo] = useState({
    description: "",
    revenue_delta: "",
    cost_delta: ""
  });
  const [invAmt, setInvAmt] = useState("");
  const load = () => api.get("/jobs").then(setRows).catch(() => setRows([]));
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
  const openDetail = useCallback(id => api.get(`/jobs/${id}`).then(setDetail), []);
  const createJob = async () => {
    try {
      await api.post("/jobs", {
        customer_name: form.customer_name,
        job_type: form.job_type,
        description: form.description,
        estimated_revenue: parseFloat(form.estimated_revenue) || 0,
        estimated_costs: Object.fromEntries(CATS.map(c => [c, parseFloat(form[c]) || 0]))
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
        qty: useStock ? parseInt(cost.qty) || 1 : 1
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
        qty: "1"
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
        description: co.description,
        revenue_delta: parseFloat(co.revenue_delta) || 0,
        cost_delta: parseFloat(co.cost_delta) || 0,
        approved: true
      });
      toast.success(t("jobs.changeOrderAdded"));
      setCo({
        description: "",
        revenue_delta: "",
        cost_delta: ""
      });
      openDetail(detail.id);
      load();
      loadNotifs();
    } catch (e) {
      toast.error(errMsg(e));
    }
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
        {isOwner && <Button data-testid="btn-alert-settings" variant="outline" onClick={() => setCfgOpen(true)} className="border-[#222838] text-gray-300 me-2"><Settings2 size={16} className="me-1" />Budget Alerts{alertCfg ? ` (${alertCfg.warning_pct}% / ${alertCfg.overrun_pct}%)` : ""}</Button>}
        {isOwner && <Button data-testid="btn-new-job" onClick={() => setCreateOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="me-1" />{t("jobs.newJob")}</Button>}
      </PageHeader>

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
                  <td className="px-5 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[j.status]}`}>{j.status}</span></td>
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
      }} cost={cost} setCost={setCost} addCost={addCost} co={co} setCo={setCo} addCo={addCo} invAmt={invAmt} setInvAmt={setInvAmt} invoice={invoice} />}
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

      {/* Estimate vs actual */}
      <div className="mt-2">
        <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">{t("jobs.estimateVsActual")}</h4>
        <table className="w-full text-sm">
          <thead><tr className="text-[10px] uppercase text-gray-500 border-b border-[#222838]"><th className="py-1.5 text-start">{t("jobs.category")}</th><th className="py-1.5 text-end">{t("jobs.estimated")}</th><th className="py-1.5 text-end">{t("jobs.actual")}</th><th className="py-1.5 text-end">{t("jobs.variance")}</th></tr></thead>
          <tbody>
            {CATS.map(c => {
            const est = estCents[c] || 0;
            const act = p.actual_by_category_cents[c] || 0;
            const v = act - est;
            return <tr key={c} className="border-b border-[#171c26]">
                  <td className="py-1.5 capitalize text-gray-300">{c}</td>
                  <td className="py-1.5 text-end font-mono text-gray-500">{money(est)}</td>
                  <td className="py-1.5 text-end font-mono text-gray-200">{money(act)}</td>
                  <td className={`py-1.5 text-end font-mono ${v > 0 ? "text-rose-400" : v < 0 ? "text-emerald-400" : "text-gray-500"}`}>{v > 0 ? "+" : ""}{money(v)}</td>
                </tr>;
          })}
          </tbody>
        </table>
      </div>

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
            <Button size="sm" data-testid="cost-save" onClick={addCost} className="bg-emerald-600 hover:bg-emerald-500 text-black h-8">{t("jobs.logCost")}</Button>
          </div>
        </div>}

      {/* Costs list */}
      {detail.costs?.length > 0 && <div className="mt-2">
          <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">{t("jobs.loggedCosts")}</h4>
          <table className="w-full text-sm" data-testid="table-job-costs">
            <tbody>
              {detail.costs.map(c => <tr key={c.id} className="border-b border-[#171c26]">
                  <td className="py-1.5 capitalize text-gray-400 w-28">{c.category}</td>
                  <td className="py-1.5 text-gray-300">{c.description || "—"}</td>
                  <td className="py-1.5 font-mono text-amber-400 w-32">{c.entry_number}</td>
                  <td className="py-1.5 text-end font-mono text-gray-200">{money(c.amount_cents)}</td>
                </tr>)}
            </tbody>
          </table>
        </div>}

      {/* Change order + invoice */}
      {isOwner && detail.status !== "estimate" && <div className="grid sm:grid-cols-2 gap-3 mt-2">
          <div className="p-3 rounded-lg bg-[#0F1218] border border-[#222838]">
            <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">{t("jobs.changeOrder")}</h4>
            <Input data-testid="co-description" value={co.description} onChange={e => setCo({
          ...co,
          description: e.target.value
        })} placeholder={t("jobs.description")} className="bg-[#14171E] border-[#222838] h-8 text-xs mb-2" />
            <div className="flex gap-2">
              <Input data-testid="co-revenue" value={co.revenue_delta} onChange={e => setCo({
            ...co,
            revenue_delta: e.target.value
          })} placeholder={t("jobs.revenue2")} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" />
              <Input data-testid="co-cost" value={co.cost_delta} onChange={e => setCo({
            ...co,
            cost_delta: e.target.value
          })} placeholder={t("jobs.cost2")} className="bg-[#14171E] border-[#222838] h-8 text-xs font-mono" />
              <Button size="sm" data-testid="co-save" onClick={addCo} className="bg-amber-600 hover:bg-amber-500 text-black h-8">{t("jobs.add")}</Button>
            </div>
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
