import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { SlidersHorizontal, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
const DEFAULTS = {
  revenue_change_pct: 0,
  new_payroll_monthly: "",
  expense_cut_pct: 0,
  one_time: "",
  months: 12
};
export default function Playground() {
  const {
    t
  } = useTranslation();
  const [loaded, setLoaded] = useState(false);
  const [levers, setLevers] = useState(DEFAULTS);
  const [res, setRes] = useState(null);
  const timer = useRef(null);
  const run = async lv => {
    try {
      setRes(await api.post("/playground/run", {
        revenue_change_pct: parseFloat(lv.revenue_change_pct) || 0,
        new_payroll_monthly_cents: Math.round(parseFloat(lv.new_payroll_monthly || 0) * 100),
        expense_cut_pct: parseFloat(lv.expense_cut_pct) || 0,
        one_time_cents: Math.round(parseFloat(lv.one_time || 0) * 100),
        months: parseInt(lv.months) || 12
      }));
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const set = (k, v) => {
    const lv = {
      ...levers,
      [k]: v
    };
    setLevers(lv);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => run(lv), 350);
  };
  useEffect(() => {
    run(DEFAULTS);
    setLoaded(true);
    return () => clearTimeout(timer.current);
  }, []);
  if (!loaded) return <Loading />;
  const maxCash = res ? Math.max(...res.scenario.map(p => p.cash_cents), ...res.baseline.map(p => p.cash_cents)) : 0;
  const minCash = res ? Math.min(...res.scenario.map(p => p.cash_cents), ...res.baseline.map(p => p.cash_cents)) : 0;
  const span = maxCash - minCash || 1;
  return <div className="space-y-6">
      <PageHeader title={t("playground.scenarioPlayground")} subtitle="Drag the levers — watch the year play out, live." actions={<Button variant="outline" onClick={() => {
      setLevers(DEFAULTS);
      run(DEFAULTS);
    }}><RotateCcw size={14} className="mr-1" />{t("playground.reset")}</Button>} />
      <div className="grid lg:grid-cols-[340px_1fr] gap-6">
        <Card className="p-5 space-y-5 h-fit">
          <div className="text-sm font-medium flex items-center gap-2"><SlidersHorizontal size={15} /> {t("playground.levers")}</div>
          <div>
            <div className="flex justify-between text-xs mb-1"><Label>{t("playground.raisePricesWinMore")}</Label><span className="font-mono">{levers.revenue_change_pct}%</span></div>
            <input type="range" min="-20" max="50" step="1" value={levers.revenue_change_pct} onChange={e => set("revenue_change_pct", e.target.value)} className="w-full accent-emerald-500" />
          </div>
          <div>
            <div className="flex justify-between text-xs mb-1"><Label>{t("playground.cutExpenses")}</Label><span className="font-mono">{levers.expense_cut_pct}%</span></div>
            <input type="range" min="0" max="30" step="1" value={levers.expense_cut_pct} onChange={e => set("expense_cut_pct", e.target.value)} className="w-full accent-emerald-500" />
          </div>
          <div><Label className="text-xs">{t("playground.newMonthlyPayroll")}</Label>
            <Input type="number" placeholder={t("playground.eG4500")} value={levers.new_payroll_monthly} onChange={e => set("new_payroll_monthly", e.target.value)} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          <div><Label className="text-xs">{t("playground.oneTimeInvestment")}</Label>
            <Input type="number" placeholder={t("playground.eG30000")} value={levers.one_time} onChange={e => set("one_time", e.target.value)} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          <div><Label className="text-xs">{t("playground.horizonMonths")}</Label>
            <Input type="number" min="1" max="24" value={levers.months} onChange={e => set("months", e.target.value)} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
        </Card>
        <div className="space-y-4">
          {res && <>
            <Card className={`p-5 ${res.verdict === "ahead" ? "bg-emerald-500/5" : res.verdict === "behind" ? "bg-red-500/5" : ""}`}>
              <div className="text-sm">{t("playground.after")} {res.scenario.length} {t("playground.monthsThisScenarioLeavesYou")}<span className={`font-semibold ${res.end_cash_delta_cents > 0 ? "text-emerald-400" : res.end_cash_delta_cents < 0 ? "text-red-400" : ""}`}>
                  {" "}{res.end_cash_delta_cents >= 0 ? "+" : ""}{money(res.end_cash_delta_cents)}{" "}
                </span>{t("playground.vsDoingNothing")}</div>
              <div className="text-xs text-gray-500 mt-1">{t("playground.scenarioNet")} {money(res.scenario_monthly_net_cents)}/mo ·
                baseline {money(res.avg_monthly_revenue_cents - res.avg_monthly_expense_cents)}/mo</div>
            </Card>
            <Card className="p-5">
              <div className="text-sm font-medium mb-1">{t("playground.cashScenarioVsBaseline")}</div>
              <div className="flex items-center gap-4 text-xs text-gray-500 mb-3">
                <span className="flex items-center gap-1"><span className="w-3 h-1 bg-emerald-500 rounded inline-block" />{t("playground.scenario")}</span>
                <span className="flex items-center gap-1"><span className="w-3 h-1 bg-gray-600 rounded inline-block" />{t("playground.baseline")}</span>
              </div>
              <div className="relative h-48">
                {[0.25, 0.5, 0.75].map(f => <div key={f} className="absolute w-full border-t border-[#1a2030]" style={{
                top: `${f * 100}%`
              }} />)}
                <svg viewBox="0 0 600 180" className="w-full h-full" preserveAspectRatio="none">
                  {[res.baseline, res.scenario].map((line, li) => {
                  const pts = line.map((p, i) => `${i / (line.length - 1) * 600},${180 - (p.cash_cents - minCash) / span * 160 - 10}`).join(" ");
                  return <polyline key={li} points={pts} fill="none" stroke={li === 1 ? "#10b981" : "#4b5563"} strokeWidth="2.5" vectorEffect="non-scaling-stroke" />;
                })}
                </svg>
              </div>
              <div className="flex justify-between text-xs text-gray-500 mt-1"><span>{t("playground.now")}</span><span>{t("playground.month")} {res.scenario.length}</span></div>
            </Card>
          </>}
        </div>
      </div>
    </div>;
}
