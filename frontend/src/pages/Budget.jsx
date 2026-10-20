import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { Copy, Plus, Trash2, TrendingUp } from "lucide-react";
import { useTranslation } from "react-i18next";
function VarianceBar({
  pct
}) {
  const w = Math.min(pct, 100);
  const tone = pct > 100 ? "bg-red-500" : pct > 80 ? "bg-amber-500" : "bg-emerald-500";
  return <div className="h-1.5 w-24 overflow-hidden rounded-full bg-[#222838]" title={`${pct}% used`}>
      <div className={`h-full ${tone}`} style={{
      width: `${w}%`
    }} />
    </div>;
}
function CashBars({
  points
}) {
  const {
    t
  } = useTranslation();
  if (!points || points.length === 0) return null;
  const step = Math.max(1, Math.ceil(points.length / 72));
  const sampled = points.filter((_, i) => i % step === 0);
  const vals = sampled.map(p => p.cash_cents);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const span = hi - lo || 1;
  return <div>
      <div className="flex items-end gap-[2px] h-40">
        {sampled.map(p => {
        const h = 8 + (p.cash_cents - lo) / span * 92;
        const neg = p.cash_cents < 0;
        return <div key={p.date} title={`${p.date}: ${money(p.cash_cents)}`} className={`flex-1 rounded-t ${neg ? "bg-red-500/70" : "bg-emerald-500/70"}`} style={{
          height: `${h}%`
        }} />;
      })}
      </div>
      <div className="flex justify-between text-xs text-gray-500 mt-1">
        <span>{sampled[0]?.date}</span>
        <span className="font-mono">{t("budget.low")} {money(lo)} {t("budget.high")} {money(hi)}</span>
        <span>{sampled[sampled.length - 1]?.date}</span>
      </div>
    </div>;
}
export default function Budget() {
  const {
    user
  } = useAuth();
  const canWrite = user?.role === "owner";
  const [period, setPeriod] = useState(new Date().toISOString().slice(0, 7));
  const isQuarter = period.includes("Q");
  const cy = new Date().getFullYear();
  const quarterOpts = [];
  for (let y = cy - 1; y <= cy + 1; y++) for (let q = 1; q <= 4; q++) quarterOpts.push(`${y}-Q${q}`);
  const monthToQuarter = m => {
    const [y, mo] = m.split("-").map(Number);
    return `${y}-Q${Math.ceil(mo / 3)}`;
  };
  const quarterToMonth = p => {
    const [y, q] = p.split("-Q").map(Number);
    return `${y}-${String((q - 1) * 3 + 1).padStart(2, "0")}`;
  };
  const prevQuarter = p => {
    let [y, q] = p.split("-Q").map(Number);
    q -= 1;
    if (q < 1) {
      q = 4;
      y -= 1;
    }
    return `${y}-Q${q}`;
  };
  const [days, setDays] = useState(90);
  const [data, setData] = useState(null);
  const [cats, setCats] = useState([]);
  const [fc, setFc] = useState(null);
  const [runRate, setRunRate] = useState(false);
  const [expDates, setExpDates] = useState(false);
  const [haircut, setHaircut] = useState(0);
  const [shiftDays, setShiftDays] = useState(0);
  const [scenarios, setScenarios] = useState([]);
  const [scenarioId, setScenarioId] = useState("");
  const [scenName, setScenName] = useState("");
  const [events, setEvents] = useState([]);
  const [evForm, setEvForm] = useState({
    name: "",
    direction: "out",
    amount: "",
    date: "",
    frequency: "",
    shape: "custom",
    annual_salary: ""
  });
  const [showItems, setShowItems] = useState(false);
  const loadScenarios = () => api.get("/budget/forecast/scenarios").then(setScenarios).catch(() => {});
  const loadEvents = () => api.get("/budget/forecast/events").then(setEvents).catch(() => {});
  const saveScenario = async e => {
    e.preventDefault();
    try {
      const sc = await api.post("/budget/forecast/scenarios", {
        name: scenName.trim(),
        run_rate: runRate,
        expected_pay_dates: expDates,
        payroll: true,
        tax: true,
        excluded_keys: [],
        ar_haircut_pct: Number(haircut) || 0,
        ar_shift_days: Number(shiftDays) || 0
      });
      toast.success("Scenario saved");
      setScenName("");
      loadScenarios();
      setScenarioId(sc.id);
    } catch (err) {
      toast.error(errMsg(err));
    }
  };
  const delScenario = async () => {
    try {
      await api.del(`/budget/forecast/scenarios/${scenarioId}`);
      toast.success("Scenario deleted");
      setScenarioId("");
      loadScenarios();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const excludeItem = async key => {
    const sc = scenarios.find(s => s.id === scenarioId);
    if (!sc) {
      toast.error("Save or select a scenario first — exclusions live on scenarios");
      return;
    }
    try {
      const {
        id,
        tenant_id,
        created_at,
        updated_at,
        ...rest
      } = sc;
      await api.put(`/budget/forecast/scenarios/${sc.id}`, {
        ...rest,
        excluded_keys: [...(sc.excluded_keys || []), key]
      });
      loadScenarios();
      loadFc();
    } catch (err) {
      toast.error(errMsg(err));
    }
  };
  const addEvent = async e => {
    e.preventDefault();
    try {
      await api.post("/budget/forecast/events", {
        name: evForm.name.trim(),
        direction: evForm.direction,
        amount: parseFloat(evForm.amount) || 0,
        date: evForm.date,
        frequency: evForm.frequency || null,
        shape: evForm.shape,
        scenario_id: scenarioId || null,
        annual_salary: evForm.shape === "hire" ? parseFloat(evForm.annual_salary) || 0 : null
      });
      toast.success("Event added");
      setEvForm({
        name: "",
        direction: "out",
        amount: "",
        date: "",
        frequency: "",
        shape: "custom",
        annual_salary: ""
      });
      loadEvents();
      loadFc();
    } catch (err) {
      toast.error(errMsg(err));
    }
  };
  const delEvent = async id => {
    try {
      await api.del(`/budget/forecast/events/${id}`);
      loadEvents();
      loadFc();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const [form, setForm] = useState({
    category: "",
    amount: ""
  });
  const loadBudget = () => api.get(`/budget?period=${period}`).then(setData).catch(e => {
    toast.error(errMsg(e));
    setData({
      rows: [],
      totals: {}
    });
  });
  const loadFc = () => {
    const p = new URLSearchParams({
      days: String(days)
    });
    if (runRate) p.set("run_rate", "true");
    if (expDates) p.set("expected_pay_dates", "true");
    if (scenarioId) p.set("scenario_id", scenarioId); else {
      if (Number(haircut) > 0) p.set("ar_haircut_pct", String(haircut));
      if (Number(shiftDays) !== 0) p.set("ar_shift_days", String(shiftDays));
    }
    api.get(`/budget/forecast?${p.toString()}`).then(setFc).catch(e => {
      toast.error(errMsg(e));
      setFc(null);
    });
  };
  useEffect(() => {
    loadBudget();
  }, [period]);
  useEffect(() => {
    loadFc();
  }, [days, runRate, expDates, scenarioId, haircut, shiftDays]);
  useEffect(() => {
    loadScenarios();
    loadEvents();
  }, []);
  useEffect(() => {
    api.get("/budget/categories").then(setCats).catch(() => {});
  }, []);
  const save = async e => {
    e.preventDefault();
    try {
      await api.post("/budget", {
        period,
        category: form.category.trim(),
        amount: parseFloat(form.amount) || 0
      });
      toast.success(t("budget.budgetSaved"));
      setForm({
        category: "",
        amount: ""
      });
      loadBudget();
    } catch (err) {
      toast.error(errMsg(err));
    }
  };
  const del = async id => {
    try {
      await api.del(`/budget/${id}`);
      toast.success(t("budget.budgetRemoved"));
      loadBudget();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const copy = async () => {
    let from;
    if (isQuarter) {
      from = prevQuarter(period);
    } else {
      const [y, m] = period.split("-").map(Number);
      const prev = new Date(y, m - 2, 1);
      from = `${prev.getFullYear()}-${String(prev.getMonth() + 1).padStart(2, "0")}`;
    }
    try {
      const r = await api.post("/budget/copy", {
        from_period: from,
        to_period: period
      });
      toast.success(`Copied ${r.copied} budgets from ${from}`);
      loadBudget();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!data) return <Loading />;
  const rows = data.rows || [];
  const t = data.totals || {};
  return <div className="animate-rise">
      <PageHeader title={t("budget.budgetForecast")} subtitle="Monthly or quarterly spending budgets with variance, plus a 30/60/90-day cash projection" testid="budget-page">
        <div className="flex gap-1">
          <Button size="sm" variant={!isQuarter ? "default" : "outline"} className={`h-9 ${!isQuarter ? "bg-amber-600 hover:bg-amber-500 text-black" : "border-[#222838]"}`} onClick={() => setPeriod(p => quarterToMonth(p))}>Monthly</Button>
          <Button size="sm" variant={isQuarter ? "default" : "outline"} className={`h-9 ${isQuarter ? "bg-amber-600 hover:bg-amber-500 text-black" : "border-[#222838]"}`} onClick={() => setPeriod(p => monthToQuarter(p))}>Quarterly</Button>
        </div>
        {isQuarter ? <select value={period} onChange={e => setPeriod(e.target.value)} className="bg-[#0F1218] border border-[#222838] rounded-md h-9 px-3 text-sm text-gray-200" data-testid="budget-period-quarter">
            {quarterOpts.map(q => <option key={q} value={q}>{q.replace("-", " ")}</option>)}
          </select> : <Input type="month" value={period} onChange={e => setPeriod(e.target.value)} className="bg-[#0F1218] border-[#222838] w-40" data-testid="budget-period" />}
      </PageHeader>

      <Card className="p-0 overflow-hidden mb-6" data-testid="budget-table">
        <div className="px-5 py-4 flex items-center justify-between border-b border-[#222838]">
          <h3 className="text-sm font-semibold text-gray-200">{t("budget.budgets")} {period}</h3>
          {canWrite && <Button size="sm" variant="outline" className="border-[#222838] h-7" onClick={copy}><Copy size={14} className="mr-1" /> {t("budget.copyLastMonth")}</Button>}
        </div>
        <table className="w-full text-sm">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="px-5 py-3 text-left font-medium">{t("budget.category")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("budget.budgeted")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("budget.actual")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("budget.remaining")}</th>
            <th className="px-5 py-3 text-right font-medium">Variance</th>
            <th className="px-5 py-3 text-left font-medium">{t("budget.used")}</th><th /></tr></thead>
          <tbody>
            {rows.map(r => <tr key={r.category} className="border-b border-[#171c26]" data-testid={`budget-row-${r.category}`}>
                <td className="px-5 py-3 text-gray-100">{r.category}{!r.budgeted_cents && r.actual_cents > 0 && <span className="ml-1 text-xs text-amber-500">{t("budget.noBudget")}</span>}</td>
                <td className="px-5 py-3 text-right font-mono text-gray-300">{money(r.budgeted_cents)}</td>
                <td className={`px-5 py-3 text-right font-mono ${r.pct_used > 100 ? "text-red-400 font-medium" : "text-gray-200"}`}>{money(r.actual_cents)}</td>
                <td className={`px-5 py-3 text-right font-mono ${r.remaining_cents < 0 ? "text-red-400" : "text-gray-300"}`}>{money(r.remaining_cents)}</td>
                <td className={`px-5 py-3 text-right font-mono ${r.variance_cents == null || r.variance_cents >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                  {money(r.variance_cents)}
                  {r.variance_pct != null && <span className="text-xs text-gray-500"> ({r.variance_pct}%)</span>}
                  {r.variance_pct == null && <span className="text-xs text-gray-500"> —</span>}
                </td>
                <td className="px-5 py-3"><div className="flex items-center gap-2"><VarianceBar pct={r.pct_used} /><span className="text-xs text-gray-500 font-mono">{r.pct_used}%</span></div></td>
                <td className="px-5 py-3 text-right">{canWrite && r.id && <button onClick={() => del(r.id)} className="text-gray-600 hover:text-rose-400"><Trash2 size={14} /></button>}</td>
              </tr>)}
            {rows.length === 0 && <tr><td colSpan={7} className="px-5 py-12 text-center text-gray-500">{t("budget.noBudgetsOrSpendingForThis")}</td></tr>}
          </tbody>
          {rows.length > 0 && <tfoot><tr className="font-semibold text-gray-100">
              <td className="px-5 py-3">{t("budget.total")}</td>
              <td className="px-5 py-3 text-right font-mono">{money(t.budgeted_cents)}</td>
              <td className="px-5 py-3 text-right font-mono">{money(t.actual_cents)}</td>
              <td className="px-5 py-3 text-right font-mono">{money(t.remaining_cents)}</td>
              <td className="px-5 py-3 text-right font-mono">{t.variance_pct != null ? <span className={t.variance_cents < 0 ? "text-red-400" : "text-emerald-400"}>{t.variance_pct}%</span> : "—"}</td>
              <td colSpan={2} />
            </tr></tfoot>}
        </table>
        {canWrite && <form className="px-5 py-4 flex flex-wrap items-end gap-2 border-t border-[#222838]" onSubmit={save}>
            <div><Label className="text-xs">{t("budget.category")}</Label>
              <Input value={form.category} onChange={e => setForm({
            ...form,
            category: e.target.value
          })} className="bg-[#0F1218] border-[#222838] w-44" list="budget-cat-list" placeholder={t("budget.eGSuppliesOffice")} required />
              <datalist id="budget-cat-list">{cats.map(c => <option key={c} value={c} />)}</datalist></div>
            <div><Label className="text-xs">{t("budget.amount")}</Label><Input type="number" step="0.01" min={0} value={form.amount} onChange={e => setForm({
            ...form,
            amount: e.target.value
          })} className="bg-[#0F1218] border-[#222838] w-32 font-mono" required /></div>
            <Button size="sm" type="submit" className="bg-amber-600 hover:bg-amber-500 text-black"><Plus size={14} className="mr-1" /> {t("budget.setBudget")}</Button>
          </form>}
      </Card>

      <Card data-testid="forecast-panel">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-gray-200 flex items-center gap-1"><TrendingUp size={14} className="text-emerald-400" /> {t("budget.cashForecast")}</h3>
          <div className="flex gap-1">
            {[30, 60, 90].map(d => <Button key={d} size="sm" variant={days === d ? "default" : "outline"} className={`h-7 ${days === d ? "bg-amber-600 hover:bg-amber-500 text-black" : "border-[#222838]"}`} onClick={() => setDays(d)}>{d}d</Button>)}
          </div>
        </div>
        <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-gray-300">
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={runRate} onChange={e => setRunRate(e.target.checked)} data-testid="fc-runrate" />
            Run-rate band <span className="text-gray-500">(trailing 90d cash, net of scheduled items)</span>
          </label>
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={expDates} onChange={e => setExpDates(e.target.checked)} data-testid="fc-expdates" />
            Expected pay dates <span className="text-gray-500">(customers' own history)</span>
          </label>
          <label className="flex items-center gap-1.5">Scenario
            <select value={scenarioId} onChange={e => setScenarioId(e.target.value)} className="bg-[#0F1218] border border-[#222838] rounded-md h-7 px-2 text-xs text-gray-200" data-testid="fc-scenario">
              <option value="">Base forecast</option>
              {scenarios.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
          {!scenarioId && <>
            <label className="flex items-center gap-1.5">AR haircut %
              <Input type="number" min={0} max={100} value={haircut} onChange={e => setHaircut(e.target.value)} className="bg-[#0F1218] border-[#222838] w-16 h-7 font-mono" data-testid="fc-haircut" />
            </label>
            <label className="flex items-center gap-1.5">AR shift days
              <Input type="number" value={shiftDays} onChange={e => setShiftDays(e.target.value)} className="bg-[#0F1218] border-[#222838] w-16 h-7 font-mono" data-testid="fc-shift" />
            </label>
          </>}
        </div>
        {fc ? <>
            {fc.alerts?.first_below_date && <div className="mb-4 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300" data-testid="fc-lowcash">
              Cash is projected to dip below {money(fc.alerts.threshold_cents)} on {fc.alerts.first_below_date} (low point {money(fc.min_cash_cents)} on {fc.min_cash_date}).
            </div>}
            <div className="mb-4 grid grid-cols-2 gap-2 text-sm md:grid-cols-4">
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] p-3"><div className="text-xs text-gray-500">{t("budget.cashToday")}</div><div className="font-mono font-medium text-gray-100">{money(fc.start_cash_cents)}</div></div>
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] p-3"><div className="text-xs text-gray-500">{t("budget.projectedIn")} {days}d</div><div className="font-mono font-medium text-gray-100">{money(fc.end_cash_cents)}</div></div>
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] p-3"><div className="text-xs text-gray-500">{t("budget.lowestPoint")}</div><div className="font-mono font-medium text-amber-400">{money(fc.min_cash_cents)} <span className="text-xs text-gray-500">({fc.min_cash_date})</span></div></div>
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] p-3"><div className="text-xs text-gray-500">{t("budget.scheduledARBills")}</div>
                <div className="font-mono font-medium text-emerald-400">+{money(fc.scheduled.ar_cents)}</div>
                <div className="font-mono text-xs text-red-400">−{money(fc.scheduled.bills_cents)}</div></div>
            </div>
            <CashBars points={fc.points} />
            <div className="mt-4 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] p-2.5"><div className="text-gray-500">Recurring</div><div className="font-mono text-emerald-400">+{money(fc.scheduled.recurring_inflow_cents)}</div><div className="font-mono text-red-400">−{money(fc.scheduled.recurring_outflow_cents)}</div></div>
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] p-2.5"><div className="text-gray-500">Payroll (based on last run)</div><div className="font-mono text-red-400">−{money(fc.scheduled.payroll_cents)}</div></div>
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] p-2.5"><div className="text-gray-500">Tax estimates + sales tax</div><div className="font-mono text-red-400">−{money((fc.scheduled.tax_cents || 0) + (fc.scheduled.sales_tax_cents || 0))}</div></div>
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] p-2.5"><div className="text-gray-500">Manual events</div><div className="font-mono text-emerald-400">+{money(fc.scheduled.events_in_cents)}</div><div className="font-mono text-red-400">−{money(fc.scheduled.events_out_cents)}</div></div>
            </div>
            {fc.notes?.length > 0 && <div className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-200/90" data-testid="fc-notes">
              <div className="font-medium mb-1">Coverage notes</div>
              <ul className="list-disc pl-4 space-y-0.5">{fc.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
            </div>}
            <div className="mt-4 text-xs text-gray-500">
              <div className="font-medium text-gray-300 mb-1">{t("budget.assumptions")}</div>
              <ul className="list-disc pl-4 space-y-0.5">{fc.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
              <p className="mt-2">Run-rate band: {money(fc.avg_daily_inflow_cents)}/day in, {money(fc.avg_daily_outflow_cents)}/day out (trailing 90d cash, excluding scheduled categories) — {fc.layers?.run_rate?.enabled ? "included in the curve above" : "not included; tick the band to layer it on"}.</p>
              {fc.layers?.expected_pay_dates?.adjustments?.length > 0 && <p className="mt-1">{fc.layers.expected_pay_dates.adjustments.map(a => `${a.customer} usually pays ~${a.avg_days_late} days ${a.avg_days_late >= 0 ? "late" : "early"} (${a.samples} invoices)`).join("; ")}</p>}
            </div>

            <div className="mt-6">
              <h4 className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-2">13-week roll-up</h4>
              <div className="overflow-x-auto">
                <table className="w-full text-xs" data-testid="fc-weekly">
                  <thead><tr className="text-gray-500 border-b border-[#222838]">
                    <th className="py-2 text-left font-medium">Week</th>
                    <th className="py-2 text-right font-medium">In</th>
                    <th className="py-2 text-right font-medium">Out</th>
                    <th className="py-2 text-right font-medium">Net</th>
                    <th className="py-2 text-right font-medium">End cash</th></tr></thead>
                  <tbody>
                    {(fc.weekly || []).map(w => <tr key={w.week} className="border-b border-[#171c26]">
                      <td className="py-1.5 text-gray-300">{w.start_date} → {w.end_date}</td>
                      <td className="py-1.5 text-right font-mono text-emerald-400">{money(w.inflow_cents)}</td>
                      <td className="py-1.5 text-right font-mono text-red-400">{money(w.outflow_cents)}</td>
                      <td className={`py-1.5 text-right font-mono ${w.net_cents < 0 ? "text-red-400" : "text-gray-200"}`}>{money(w.net_cents)}</td>
                      <td className={`py-1.5 text-right font-mono ${w.end_cash_cents < 0 ? "text-red-400" : "text-gray-100"}`}>{money(w.end_cash_cents)}</td></tr>)}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="mt-6">
              <button className="text-xs font-semibold uppercase tracking-wider text-gray-400" onClick={() => setShowItems(s => !s)} data-testid="fc-items-toggle">
                {showItems ? "Hide" : "Show"} the {fc.items?.length || 0} scheduled items behind this curve
              </button>
              {showItems && <div className="mt-2 max-h-72 overflow-y-auto">
                <table className="w-full text-xs" data-testid="fc-items">
                  <tbody>
                    {(fc.items || []).map(i => <tr key={i.key} className="border-b border-[#171c26]">
                      <td className="py-1.5 text-gray-400 font-mono">{i.date}</td>
                      <td className="py-1.5 text-gray-300">{i.label}</td>
                      <td className="py-1.5 text-gray-500">{i.kind.replace("_", " ")}</td>
                      <td className={`py-1.5 text-right font-mono ${i.direction === "in" ? "text-emerald-400" : "text-red-400"}`}>{i.direction === "in" ? "+" : "−"}{money(i.amount_cents)}</td>
                      <td className="py-1.5 text-right">{canWrite && <button className="text-gray-600 hover:text-amber-400" title="Exclude this item in the selected scenario" onClick={() => excludeItem(i.key)}>exclude</button>}</td></tr>)}
                  </tbody>
                </table>
              </div>}
            </div>

            <div className="mt-6 border-t border-[#222838] pt-4">
              <h4 className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-2">What-if events {scenarioId ? "(base + this scenario)" : "(base forecast)"}</h4>
              {events.length > 0 && <ul className="mb-3 space-y-1 text-xs">
                {events.map(ev => <li key={ev.id} className="flex items-center justify-between rounded bg-[#0F1218] border border-[#222838] px-2.5 py-1.5">
                  <span className="text-gray-300">{ev.name} <span className="text-gray-500">· {ev.date}{ev.frequency ? ` · ${ev.frequency}` : ""}{ev.scenario_id ? " · scenario" : ""}</span></span>
                  <span className="flex items-center gap-2">
                    <span className={`font-mono ${ev.direction === "in" ? "text-emerald-400" : "text-red-400"}`}>{ev.direction === "in" ? "+" : "−"}{money(ev.amount_cents)}</span>
                    {canWrite && <button onClick={() => delEvent(ev.id)} className="text-gray-600 hover:text-rose-400"><Trash2 size={12} /></button>}
                  </span></li>)}
              </ul>}
              {canWrite && <form className="flex flex-wrap items-end gap-2" onSubmit={addEvent}>
                <div><Label className="text-xs">Name</Label><Input value={evForm.name} onChange={e => setEvForm({ ...evForm, name: e.target.value })} className="bg-[#0F1218] border-[#222838] w-40" required /></div>
                <div><Label className="text-xs">Shape</Label>
                  <select value={evForm.shape} onChange={e => setEvForm({ ...evForm, shape: e.target.value })} className="bg-[#0F1218] border border-[#222838] rounded-md h-9 px-2 text-sm text-gray-200">
                    <option value="custom">Custom</option>
                    <option value="one_time">One-time</option>
                    <option value="recurring">Recurring cost</option>
                    <option value="hire">New hire</option>
                  </select></div>
                <div><Label className="text-xs">Direction</Label>
                  <select value={evForm.direction} onChange={e => setEvForm({ ...evForm, direction: e.target.value })} className="bg-[#0F1218] border border-[#222838] rounded-md h-9 px-2 text-sm text-gray-200">
                    <option value="out">Money out</option>
                    <option value="in">Money in</option>
                  </select></div>
                {evForm.shape === "hire"
                  ? <div><Label className="text-xs">Annual salary</Label><Input type="number" step="0.01" min={0} value={evForm.annual_salary} onChange={e => setEvForm({ ...evForm, annual_salary: e.target.value })} className="bg-[#0F1218] border-[#222838] w-28 font-mono" required /></div>
                  : <div><Label className="text-xs">Amount</Label><Input type="number" step="0.01" min={0} value={evForm.amount} onChange={e => setEvForm({ ...evForm, amount: e.target.value })} className="bg-[#0F1218] border-[#222838] w-28 font-mono" required /></div>}
                <div><Label className="text-xs">Date</Label><Input type="date" value={evForm.date} onChange={e => setEvForm({ ...evForm, date: e.target.value })} className="bg-[#0F1218] border-[#222838] w-36" required /></div>
                {evForm.shape !== "one_time" && evForm.shape !== "hire" && <div><Label className="text-xs">Repeats</Label>
                  <select value={evForm.frequency} onChange={e => setEvForm({ ...evForm, frequency: e.target.value })} className="bg-[#0F1218] border border-[#222838] rounded-md h-9 px-2 text-sm text-gray-200">
                    <option value="">Never</option>
                    <option value="weekly">Weekly</option>
                    <option value="monthly">Monthly</option>
                    <option value="quarterly">Quarterly</option>
                  </select></div>}
                <Button size="sm" type="submit" className="bg-amber-600 hover:bg-amber-500 text-black"><Plus size={14} className="mr-1" /> Add event</Button>
              </form>}
              {canWrite && <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={saveScenario}>
                <div><Label className="text-xs">Save current toggles + sliders as a scenario</Label><Input value={scenName} onChange={e => setScenName(e.target.value)} className="bg-[#0F1218] border-[#222838] w-52" placeholder="e.g. Slow-paying winter" required /></div>
                <Button size="sm" type="submit" variant="outline" className="border-[#222838]">Save scenario</Button>
                {scenarioId && <Button size="sm" type="button" variant="outline" className="border-[#222838]" onClick={delScenario}>Delete selected scenario</Button>}
              </form>}
            </div>
          </> : <p className="text-sm text-gray-500">{t("budget.projecting")}</p>}
      </Card>
    </div>;
}
