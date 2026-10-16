import { useEffect, useState } from "react";
import { api, errMsg, downloadCsv } from "@/lib/api";
import { dollars } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Download, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";
export default function BudgetVsActual() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const canManage = user?.role === "owner";
  const [year, setYear] = useState("2026");
  const [data, setData] = useState(null);
  const [coa, setCoa] = useState([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    account_code: "4300",
    amount: ""
  });
  const load = () => api.get(`/reports/budget-vs-actual?year=${year}`).then(setData).catch(e => toast.error(errMsg(e)));
  useEffect(() => {
    load();
    api.get("/ledger/accounts").then(setCoa).catch(() => {});
  }, []);
  const plAccts = coa.filter(c => (c.type === "revenue" || c.type === "expense") && !c.archived);
  const saveBudget = async () => {
    try {
      await api.post("/reports/budgets", {
        year,
        account_code: form.account_code,
        amount: parseFloat(form.amount) || 0
      });
      toast.success(t("budgetvsactual.budgetSet"));
      setOpen(false);
      setForm({
        account_code: "4300",
        amount: ""
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!data) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("budgetvsactual.budgetVsActual")} subtitle="Plan against real ledger performance" testid="bva-page">
        {canManage && <Button data-testid="btn-set-budget" onClick={() => setOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" />{t("budgetvsactual.setBudget")}</Button>}
        <Button data-testid="btn-bva-csv" variant="outline" onClick={() => downloadCsv(`/reports/budget-vs-actual.csv?year=${year}`, `budget_${year}.csv`)} className="border-[#222838]"><Download size={15} className="mr-1" />{t("budgetvsactual.csv")}</Button>
      </PageHeader>
      <div className="flex items-end gap-3 mb-5">
        <div><Label className="text-xs">{t("budgetvsactual.year")}</Label><Input data-testid="bva-year" value={year} onChange={e => setYear(e.target.value)} className="bg-[#0F1218] border-[#222838] w-28" /></div>
        <Button data-testid="bva-run" onClick={load} className="bg-amber-600 hover:bg-amber-500 text-black">{t("budgetvsactual.run")}</Button>
      </div>
      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm" data-testid="table-bva">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="px-4 py-3 text-left font-medium">{t("budgetvsactual.account")}</th><th className="px-4 py-3 text-left font-medium">{t("budgetvsactual.type")}</th>
            <th className="px-4 py-3 text-right font-medium">{t("budgetvsactual.budget")}</th><th className="px-4 py-3 text-right font-medium">{t("budgetvsactual.actual")}</th>
            <th className="px-4 py-3 text-right font-medium">{t("budgetvsactual.variance")}</th>
            <th className="px-4 py-3 text-right font-medium">Var %</th></tr></thead>
          <tbody>
            {data.rows.map(r => <tr key={r.code} className="border-b border-[#171c26] hover:bg-[#161b25]">
                <td className="px-4 py-3 text-gray-100">{r.code} · {r.name}</td>
                <td className="px-4 py-3 text-gray-500 capitalize">{r.type}</td>
                <td className="px-4 py-3 text-right font-mono text-gray-400">{dollars(r.budget)}</td>
                <td className="px-4 py-3 text-right font-mono text-gray-200">{dollars(r.actual)}</td>
                <td className={`px-4 py-3 text-right font-mono ${r.variance_cents < 0 ? "text-rose-400" : "text-emerald-400"}`}>{dollars(r.variance)}</td>
                <td className={`px-4 py-3 text-right font-mono ${r.variance_pct == null ? "text-gray-600" : r.variance_cents < 0 ? "text-rose-400" : "text-emerald-400"}`}>{r.variance_pct == null ? "—" : `${r.variance_pct}%`}</td>
              </tr>)}
            {data.rows.length === 0 && <tr><td colSpan={6} className="px-5 py-12 text-center text-gray-500">{t("budgetvsactual.noRevenueExpenseActivityOrBudgets")} {year}.</td></tr>}
          </tbody>
        </table>
      </Card>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("budgetvsactual.setBudget2")} {year}</DialogTitle></DialogHeader>
          <div><Label className="text-xs">{t("budgetvsactual.account")}</Label>
            <Select value={form.account_code} onValueChange={v => setForm({
            ...form,
            account_code: v
          })}>
              <SelectTrigger data-testid="budget-account" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100 max-h-72">{plAccts.map(c => <SelectItem key={c.code} value={c.code}>{c.code} · {c.name}</SelectItem>)}</SelectContent>
            </Select></div>
          <div><Label className="text-xs">{t("budgetvsactual.annualBudget")}</Label><Input data-testid="budget-amount" value={form.amount} onChange={e => setForm({
            ...form,
            amount: e.target.value
          })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          <DialogFooter><Button data-testid="budget-save" onClick={saveBudget} className="bg-amber-600 hover:bg-amber-500 text-black">{t("budgetvsactual.saveBudget")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
