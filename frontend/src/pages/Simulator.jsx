import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Calculator, TrendingDown, TrendingUp, Minus } from "lucide-react";
import { useTranslation } from "react-i18next";
const VERDICT = {
  yes: {
    color: "text-emerald-400",
    bg: "bg-emerald-500/10",
    icon: TrendingUp,
    label: "Yes — go for it"
  },
  tight: {
    color: "text-amber-400",
    bg: "bg-amber-500/10",
    icon: Minus,
    label: "Tight — proceed with care"
  },
  no: {
    color: "text-red-400",
    bg: "bg-red-500/10",
    icon: TrendingDown,
    label: "No — not right now"
  }
};
export default function Simulator() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const [loaded, setLoaded] = useState(false);
  const [base, setBase] = useState(null);
  const [res, setRes] = useState(null);
  const [form, setForm] = useState({
    name: "",
    kind: "hire",
    monthly_cost_cents: "",
    one_time_cents: "",
    annual_salary_cents: "",
    burden_percent: "25",
    months: "12",
    min_cash_cents: "5000"
  });
  useEffect(() => {
    api.get("/simulator/baseline").then(setBase).catch(() => {});
    setLoaded(true);
  }, []);
  const run = async () => {
    const d = v => Math.round(parseFloat(v || 0) * 100);
    const body = {
      name: form.name || "Scenario",
      kind: form.kind,
      monthly_cost_cents: d(form.monthly_cost_cents),
      one_time_cents: d(form.one_time_cents),
      annual_salary_cents: d(form.annual_salary_cents),
      burden_percent: parseFloat(form.burden_percent) || 0,
      months: parseInt(form.months) || 12,
      min_cash_cents: d(form.min_cash_cents)
    };
    if (form.kind === "hire" && !body.annual_salary_cents) {
      toast.error(t("simulator.enterTheAnnualSalary"));
      return;
    }
    if (form.kind === "recurring" && !body.monthly_cost_cents) {
      toast.error(t("simulator.enterTheMonthlyCost"));
      return;
    }
    if (form.kind === "one_time" && !body.one_time_cents) {
      toast.error(t("simulator.enterThePurchasePrice"));
      return;
    }
    try {
      setRes(await api.post("/simulator/afford", body));
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const presets = [{
    name: "Hire a tech",
    kind: "hire",
    annual_salary_cents: "52000"
  }, {
    name: "Buy a work truck",
    kind: "one_time",
    one_time_cents: "45000"
  }, {
    name: "Sign a shop lease",
    kind: "recurring",
    monthly_cost_cents: "2500"
  }];
  if (!loaded) return <Loading />;
  const v = res && VERDICT[res.verdict];
  const maxCash = res ? Math.max(...res.projection.map(p => p.cash_cents), base?.cash_cents ?? 0) : 0;
  return <div className="space-y-6">
      <PageHeader title={t("simulator.canIAffordIt")} subtitle="Test big decisions against your real cash flow before you commit." />
      <div className="grid lg:grid-cols-[380px_1fr] gap-6">
        <Card className="p-5 space-y-3 h-fit">
          <div className="text-sm font-medium flex items-center gap-2"><Calculator size={15} /> {t("simulator.scenario")}</div>
          <div className="flex gap-2 flex-wrap">
            {presets.map(p => <Button key={p.name} size="sm" variant="outline" onClick={() => setForm({
            ...form,
            name: p.name,
            kind: p.kind,
            annual_salary_cents: p.annual_salary_cents ? String(parseFloat(p.annual_salary_cents)) : "",
            one_time_cents: p.one_time_cents ? String(parseFloat(p.one_time_cents)) : "",
            monthly_cost_cents: p.monthly_cost_cents ? String(parseFloat(p.monthly_cost_cents)) : ""
          })}>
                {p.name}</Button>)}
          </div>
          <div><Label className="text-xs">{t("simulator.whatAreYouConsidering")}</Label>
            <Input value={form.name} onChange={e => setForm({
            ...form,
            name: e.target.value
          })} placeholder={t("simulator.hire2Techs")} className="bg-[#0F1218] border-[#222838]" /></div>
          <div><Label className="text-xs">{t("simulator.kind")}</Label>
            <Select value={form.kind} onValueChange={x => setForm({
            ...form,
            kind: x
          })}>
              <SelectTrigger className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="hire">{t("simulator.newHireSalaryBurden")}</SelectItem>
                <SelectItem value="recurring">{t("simulator.monthlyCostLeaseLoan")}</SelectItem>
                <SelectItem value="one_time">{t("simulator.oneTimePurchase")}</SelectItem>
              </SelectContent>
            </Select></div>
          {form.kind === "hire" && <>
            <div><Label className="text-xs">{t("simulator.annualSalary")}</Label><Input type="number" value={form.annual_salary_cents} onChange={e => setForm({
              ...form,
              annual_salary_cents: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">{t("simulator.burdenTaxesBenefits")}</Label><Input type="number" value={form.burden_percent} onChange={e => setForm({
              ...form,
              burden_percent: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          </>}
          {form.kind === "recurring" && <div><Label className="text-xs">{t("simulator.monthlyCost")}</Label><Input type="number" value={form.monthly_cost_cents} onChange={e => setForm({
            ...form,
            monthly_cost_cents: e.target.value
          })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>}
          {form.kind === "one_time" && <div><Label className="text-xs">{t("simulator.purchasePrice")}</Label><Input type="number" value={form.one_time_cents} onChange={e => setForm({
            ...form,
            one_time_cents: e.target.value
          })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>}
          <div className="grid grid-cols-2 gap-2">
            <div><Label className="text-xs">{t("simulator.horizonMo")}</Label><Input type="number" value={form.months} onChange={e => setForm({
              ...form,
              months: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">{t("simulator.safetyFloor")}</Label><Input type="number" value={form.min_cash_cents} onChange={e => setForm({
              ...form,
              min_cash_cents: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          </div>
          <Button className="w-full" onClick={run} data-testid="sim-run">{t("simulator.runSimulation")}</Button>
          {base && <p className="text-xs text-gray-500">{t("simulator.basedOn")} {money(base.cash_cents)} {t("simulator.cashAnd")} {money(base.avg_monthly_net_cents)}/mo avg net (trailing 6 mo).</p>}
        </Card>
        <div className="space-y-4">
          {res && v && <>
            <Card className={`p-5 ${v.bg}`}>
              <div className={`flex items-center gap-2 text-lg font-semibold ${v.color}`}><v.icon size={20} />{v.label}</div>
              <p className="text-sm text-gray-300 mt-1">{res.note}</p>
              <p className="text-xs text-gray-500 mt-2">{t("simulator.lowestPoint")} {money(res.low_point.cash_cents)} {t("simulator.inMonth")} {res.low_point.month}
                {res.monthly_cost_cents ? ` · costs ${money(res.monthly_cost_cents)}/mo` : ""}{res.one_time_cents ? ` · ${money(res.one_time_cents)} up front` : ""}</p>
            </Card>
            <Card className="p-5">
              <div className="text-sm font-medium mb-3">{t("simulator.projectedCashBalance")}</div>
              <div className="flex items-end gap-1 h-40">
                {res.projection.map(p => {
                const h = maxCash > 0 ? Math.max(4, p.cash_cents / maxCash * 140) : 4;
                const bad = p.cash_cents < 0;
                return <div key={p.month} title={`Month ${p.month}: ${money(p.cash_cents)}`} className={`flex-1 rounded-t ${bad ? "bg-red-500" : "bg-emerald-500/70"}`} style={{
                  height: h
                }} />;
              })}
              </div>
              <div className="flex justify-between text-xs text-gray-500 mt-1"><span>{t("simulator.month1")}</span><span>{t("simulator.month")} {res.months}</span></div>
            </Card>
          </>}
          {!res && <Card className="p-10 text-center text-gray-500 text-sm">{t("simulator.buildAScenarioOnTheLeft")}</Card>}
        </div>
      </div>
    </div>;
}
