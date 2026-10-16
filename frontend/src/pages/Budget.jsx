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
  const loadFc = () => api.get(`/budget/forecast?days=${days}`).then(setFc).catch(e => {
    toast.error(errMsg(e));
    setFc(null);
  });
  useEffect(() => {
    loadBudget();
  }, [period]);
  useEffect(() => {
    loadFc();
  }, [days]);
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
        {fc ? <>
            <div className="mb-4 grid grid-cols-2 gap-2 text-sm md:grid-cols-4">
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] p-3"><div className="text-xs text-gray-500">{t("budget.cashToday")}</div><div className="font-mono font-medium text-gray-100">{money(fc.start_cash_cents)}</div></div>
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] p-3"><div className="text-xs text-gray-500">{t("budget.projectedIn")} {days}d</div><div className="font-mono font-medium text-gray-100">{money(fc.end_cash_cents)}</div></div>
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] p-3"><div className="text-xs text-gray-500">{t("budget.lowestPoint")}</div><div className="font-mono font-medium text-amber-400">{money(fc.min_cash_cents)} <span className="text-xs text-gray-500">({fc.min_cash_date})</span></div></div>
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] p-3"><div className="text-xs text-gray-500">{t("budget.scheduledARBills")}</div>
                <div className="font-mono font-medium text-emerald-400">+{money(fc.scheduled.ar_cents)}</div>
                <div className="font-mono text-xs text-red-400">−{money(fc.scheduled.bills_cents)}</div></div>
            </div>
            <CashBars points={fc.points} />
            <div className="mt-4 text-xs text-gray-500">
              <div className="font-medium text-gray-300 mb-1">{t("budget.assumptions")}</div>
              <ul className="list-disc pl-4 space-y-0.5">{fc.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
              <p className="mt-2">{t("budget.baseline")} {money(fc.avg_daily_inflow_cents)}/day in, {money(fc.avg_daily_outflow_cents)}/day out (trailing 90d). Recurring: +{money(fc.scheduled.recurring_inflow_cents)} {t("budget.in")}{money(fc.scheduled.recurring_outflow_cents)} {t("budget.out")}</p>
            </div>
          </> : <p className="text-sm text-gray-500">{t("budget.projecting")}</p>}
      </Card>
    </div>;
}
