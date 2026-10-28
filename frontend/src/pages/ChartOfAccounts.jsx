import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Plus, Archive } from "lucide-react";
import { useTranslation } from "react-i18next";
const TYPE_LABEL = {
  asset: "Assets",
  liability: "Liabilities",
  equity: "Equity",
  revenue: "Revenue",
  expense: "Expenses"
};
const ORDER = ["asset", "liability", "equity", "revenue", "expense"];
export default function ChartOfAccounts() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const [accounts, setAccounts] = useState(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    code: "",
    name: "",
    type: "expense"
  });
  const load = () => api.get("/ledger/accounts").then(setAccounts).catch(() => setAccounts([]));
  useEffect(() => {
    load();
  }, []);
  const isOwner = user?.role === "owner";
  const create = async () => {
    try {
      await api.post("/ledger/accounts", form);
      toast.success(t("chartofaccounts.accountCreated"));
      setOpen(false);
      setForm({
        code: "",
        name: "",
        type: "expense"
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const archive = async code => {
    try {
      await api.patch(`/ledger/accounts/${code}`, {
        archived: true
      });
      toast.success(t("chartofaccounts.archived"));
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!accounts) return <Loading />;
  const byType = t => accounts.filter(a => a.type === t);
  return <div className="animate-rise">
      <PageHeader title={t("chartofaccounts.chartOfAccounts")} subtitle={`${accounts.length} accounts`} testid="coa-page">
        {isOwner && <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button data-testid="btn-new-account" className="bg-amber-600 hover:bg-amber-500 text-black font-semibold">
                <Plus size={16} className="me-1" /> {t("chartofaccounts.newAccount")}</Button>
            </DialogTrigger>
            <DialogContent className="bg-[#14171E] border-[#222838]">
              <DialogHeader><DialogTitle>{t("chartofaccounts.newAccount")}</DialogTitle></DialogHeader>
              <div className="space-y-3">
                <div><Label className="text-xs">{t("chartofaccounts.code")}</Label>
                  <Input data-testid="account-code" value={form.code} onChange={e => setForm({
                ...form,
                code: e.target.value
              })} className="bg-[#0F1218] border-[#222838]" placeholder="6000" /></div>
                <div><Label className="text-xs">{t("chartofaccounts.name")}</Label>
                  <Input data-testid="account-name" value={form.name} onChange={e => setForm({
                ...form,
                name: e.target.value
              })} className="bg-[#0F1218] border-[#222838]" placeholder={t("chartofaccounts.newExpense")} /></div>
                <div><Label className="text-xs">{t("chartofaccounts.type")}</Label>
                  <Select value={form.type} onValueChange={v => setForm({
                ...form,
                type: v
              })}>
                    <SelectTrigger data-testid="account-type" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                    <SelectContent className="bg-[#14171E] border-[#222838]">
                      {ORDER.map(t => <SelectItem key={t} value={t}>{TYPE_LABEL[t]}</SelectItem>)}
                    </SelectContent>
                  </Select></div>
              </div>
              <DialogFooter>
                <Button data-testid="account-save" onClick={create} className="bg-amber-600 hover:bg-amber-500 text-black">{t("chartofaccounts.create")}</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>}
      </PageHeader>

      <div className="space-y-5">
        {ORDER.map(t => <Card key={t} className="p-0 overflow-hidden" data-testid={`coa-group-${t}`}>
            <div className="px-5 py-3 border-b border-[#222838] bg-[#11151c]">
              <h3 className="font-display font-semibold text-sm uppercase tracking-wider text-amber-500/90">{TYPE_LABEL[t]}</h3>
            </div>
            <table className="w-full text-sm">
              <tbody>
                {byType(t).map(a => <tr key={a.code} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-5 py-2.5 font-mono text-amber-400 w-24">{a.code}</td>
                    <td className="px-5 py-2.5 text-gray-200">{a.name}{a.archived && <span className="ms-2 text-xs text-rose-400">{t("chartofaccounts.archived2")}</span>}</td>
                    <td className="px-5 py-2.5 text-end">
                      <span className={`text-xs px-2 py-0.5 rounded-full ${a.normal_balance === "debit" ? "bg-emerald-500/10 text-emerald-400" : "bg-rose-500/10 text-rose-400"}`}>
                        {a.normal_balance === "debit" ? "DR normal" : "CR normal"}
                      </span>
                    </td>
                    <td className="px-5 py-2.5 w-12 text-end">
                      {isOwner && !a.is_system && !a.archived && <button onClick={() => archive(a.code)} className="text-gray-500 hover:text-rose-400" title={t("chartofaccounts.archive")}><Archive size={15} /></button>}
                    </td>
                  </tr>)}
              </tbody>
            </table>
          </Card>)}
      </div>
    </div>;
}
