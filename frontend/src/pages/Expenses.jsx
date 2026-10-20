import { useEffect, useState } from "react";
import { api, errMsg, downloadCsv } from "@/lib/api";
import { money, dollars, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { toast } from "sonner";
import { Plus, Receipt, Users, Undo2, FileText, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
export default function Expenses() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const canManage = user?.role === "owner";
  const [expenses, setExpenses] = useState([]);
  const [vendors, setVendors] = useState([]);
  const [credits, setCredits] = useState([]);
  const [coa, setCoa] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [expOpen, setExpOpen] = useState(false);
  const [venOpen, setVenOpen] = useState(false);
  const [crOpen, setCrOpen] = useState(false);
  const [year, setYear] = useState("2026");
  const [report1099, setReport1099] = useState(null);
  const [exp, setExp] = useState({
    vendor: "",
    category_code: "5700",
    amount: "",
    payment_method: "bank",
    description: "",
    merchant: "",
    total: "",
    tax: ""
  });
  const [ven, setVen] = useState({
    name: "",
    email: "",
    phone: "",
    is_1099: false,
    tax_id: "",
    default_expense_code: "5700"
  });
  const [cr, setCr] = useState({
    vendor: "",
    amount: "",
    expense_code: "5700",
    memo: ""
  });
  const reload = () => Promise.all([api.get("/vendors/expenses/list").then(setExpenses).catch(() => {}), api.get("/vendors").then(setVendors).catch(() => {}), api.get("/vendors/credits/list").then(setCredits).catch(() => {})]).then(() => setLoaded(true));
  useEffect(() => {
    reload();
    api.get("/ledger/accounts").then(setCoa).catch(() => {});
  }, []);
  const expenseAccts = coa.filter(c => c.type === "expense" && !c.archived);
  const saveExpense = async () => {
    try {
      await api.post("/vendors/expenses", {
        vendor: exp.vendor,
        category_code: exp.category_code,
        amount: parseFloat(exp.amount) || 0,
        payment_method: exp.payment_method,
        description: exp.description,
        receipt: exp.merchant || exp.total ? {
          merchant: exp.merchant,
          total: parseFloat(exp.total) || 0,
          tax: parseFloat(exp.tax) || 0
        } : null
      });
      toast.success(t("expenses.expenseRecorded"));
      setExpOpen(false);
      setExp({
        vendor: "",
        category_code: "5700",
        amount: "",
        payment_method: "bank",
        description: "",
        merchant: "",
        total: "",
        tax: ""
      });
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const saveVendor = async () => {
    try {
      await api.post("/vendors", ven);
      toast.success(t("expenses.vendorAdded"));
      setVenOpen(false);
      setVen({
        name: "",
        email: "",
        phone: "",
        is_1099: false,
        tax_id: "",
        default_expense_code: "5700"
      });
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const saveCredit = async () => {
    try {
      await api.post("/vendors/credits", {
        ...cr,
        amount: parseFloat(cr.amount) || 0
      });
      toast.success(t("expenses.vendorCreditRecorded"));
      setCrOpen(false);
      setCr({
        vendor: "",
        amount: "",
        expense_code: "5700",
        memo: ""
      });
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const delVendor = async v => {
    if (!window.confirm(`Delete vendor "${v.name}"? Expenses already recorded keep their vendor name.`)) return;
    try {
      await api.del(`/vendors/${v.id}`);
      toast.success(t("expenses.vendorDeleted"));
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const run1099 = () => api.get(`/vendors/1099?year=${year}`).then(setReport1099).catch(e => toast.error(errMsg(e)));
  if (!loaded) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("expenses.expensesVendors")} subtitle="Direct expenses, vendor records, credits & 1099" testid="expenses-page" />
      <Tabs defaultValue="expenses">
        <TabsList className="bg-[#14171E] border border-[#222838]">
          <TabsTrigger value="expenses" data-testid="tab-expenses"><Receipt size={14} className="mr-1" />{t("expenses.expenses")}</TabsTrigger>
          <TabsTrigger value="vendors" data-testid="tab-vendors"><Users size={14} className="mr-1" />{t("expenses.vendors")}</TabsTrigger>
          <TabsTrigger value="credits" data-testid="tab-credits"><Undo2 size={14} className="mr-1" />{t("expenses.credits")}</TabsTrigger>
          <TabsTrigger value="1099" data-testid="tab-1099"><FileText size={14} className="mr-1" />1099</TabsTrigger>
        </TabsList>

        <TabsContent value="expenses" className="mt-4">
          {canManage && <div className="mb-3 flex justify-end"><Button data-testid="btn-new-expense" onClick={() => setExpOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" />{t("expenses.newExpense")}</Button></div>}
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-expenses">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-4 py-3 text-left font-medium">{t("expenses.date")}</th><th className="px-4 py-3 text-left font-medium">{t("expenses.vendor")}</th>
                <th className="px-4 py-3 text-left font-medium">{t("expenses.category")}</th><th className="px-4 py-3 text-left font-medium">{t("expenses.receipt")}</th>
                <th className="px-4 py-3 text-right font-medium">{t("expenses.amount")}</th></tr></thead>
              <tbody>
                {expenses.map(e => <tr key={e.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-4 py-3 text-gray-400">{fmtDate(e.expense_date)}</td>
                    <td className="px-4 py-3 text-gray-100">{e.vendor}<div className="text-xs text-gray-500">{e.description}</div></td>
                    <td className="px-4 py-3 text-gray-400">{e.category_name}</td>
                    <td className="px-4 py-3 text-gray-500 text-xs">{e.receipt ? `📎 ${e.receipt.merchant || "attached"}` : "—"}</td>
                    <td className="px-4 py-3 text-right font-mono text-rose-400">{money(e.amount_cents)}</td>
                  </tr>)}
                {expenses.length === 0 && <tr><td colSpan={5} className="px-5 py-12 text-center text-gray-500">{t("expenses.noExpensesYet")}</td></tr>}
              </tbody>
            </table>
          </Card>
        </TabsContent>

        <TabsContent value="vendors" className="mt-4">
          {canManage && <div className="mb-3 flex justify-end"><Button data-testid="btn-new-vendor" onClick={() => setVenOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" />{t("expenses.newVendor")}</Button></div>}
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-vendors">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-4 py-3 text-left font-medium">{t("expenses.vendor")}</th><th className="px-4 py-3 text-left font-medium">{t("expenses.contact")}</th>
                <th className="px-4 py-3 text-left font-medium">1099</th><th className="px-4 py-3 text-left font-medium">{t("expenses.taxID")}</th>
                {canManage && <th className="px-4 py-3 text-right font-medium">{t("expenses.actions")}</th>}</tr></thead>
              <tbody>
                {vendors.map(v => <tr key={v.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-4 py-3 text-gray-100">{v.name}</td>
                    <td className="px-4 py-3 text-gray-400 text-xs">{v.email || v.phone || "—"}</td>
                    <td className="px-4 py-3">{v.is_1099 ? <span className="px-2 py-0.5 rounded-full text-xs bg-amber-500/10 text-amber-400 border border-amber-500/20">1099</span> : <span className="text-gray-600">—</span>}</td>
                    <td className="px-4 py-3 text-gray-400 font-mono text-xs">{v.tax_id_masked || "—"}</td>
                    {canManage && <td className="px-4 py-3 text-right">
                        <button onClick={() => delVendor(v)} aria-label={`Delete ${v.name}`} data-testid={`vendor-delete-${v.id}`} className="inline-flex items-center justify-center w-8 h-8 rounded text-gray-500 hover:text-rose-400 hover:bg-rose-500/10"><Trash2 size={15} /></button>
                      </td>}
                  </tr>)}
                {vendors.length === 0 && <tr><td colSpan={canManage ? 5 : 4} className="px-5 py-12 text-center text-gray-500">{t("expenses.noVendorsYet")}</td></tr>}
              </tbody>
            </table>
          </Card>
        </TabsContent>

        <TabsContent value="credits" className="mt-4">
          {canManage && <div className="mb-3 flex justify-end"><Button data-testid="btn-new-vendor-credit" onClick={() => setCrOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" />{t("expenses.newVendorCredit")}</Button></div>}
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-vendor-credits">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-4 py-3 text-left font-medium">{t("expenses.vendor")}</th><th className="px-4 py-3 text-left font-medium">{t("expenses.memo")}</th>
                <th className="px-4 py-3 text-right font-medium">{t("expenses.amount")}</th><th className="px-4 py-3 text-right font-medium">{t("expenses.remaining")}</th></tr></thead>
              <tbody>
                {credits.map(c => <tr key={c.id} className="border-b border-[#171c26]">
                    <td className="px-4 py-3 text-gray-100">{c.vendor}</td><td className="px-4 py-3 text-gray-400">{c.memo}</td>
                    <td className="px-4 py-3 text-right font-mono text-gray-300">{money(c.amount_cents)}</td>
                    <td className="px-4 py-3 text-right font-mono text-emerald-400">{money(c.remaining_cents)}</td>
                  </tr>)}
                {credits.length === 0 && <tr><td colSpan={4} className="px-5 py-12 text-center text-gray-500">{t("expenses.noVendorCredits")}</td></tr>}
              </tbody>
            </table>
          </Card>
        </TabsContent>

        <TabsContent value="1099" className="mt-4">
          <Card>
            <div className="flex items-end gap-3 mb-4">
              <div><Label className="text-xs">{t("expenses.taxYear")}</Label><Input data-testid="1099-year" value={year} onChange={e => setYear(e.target.value)} className="bg-[#0F1218] border-[#222838] w-28" /></div>
              <Button data-testid="btn-run-1099" onClick={run1099} className="bg-amber-600 hover:bg-amber-500 text-black">{t("expenses.runReport")}</Button>
            </div>
            {report1099 && <table className="w-full text-sm" data-testid="table-1099">
                <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="px-4 py-3 text-left font-medium">{t("expenses.vendor")}</th><th className="px-4 py-3 text-left font-medium">{t("expenses.taxID")}</th>
                  <th className="px-4 py-3 text-right font-medium">{t("expenses.paid")}</th><th className="px-4 py-3 text-left font-medium">{t("expenses.reportable600")}</th></tr></thead>
                <tbody>
                  {report1099.vendors.map(v => <tr key={v.vendor_id} className="border-b border-[#171c26]">
                      <td className="px-4 py-3 text-gray-100">{v.name}</td><td className="px-4 py-3 text-gray-400 font-mono text-xs">{v.tax_id_masked || "—"}</td>
                      <td className="px-4 py-3 text-right font-mono text-gray-200">{dollars(v.paid)}</td>
                      <td className="px-4 py-3">{v.reportable ? <span className="text-amber-400">{t("expenses.yes")}</span> : <span className="text-gray-600">{t("expenses.no")}</span>}</td>
                    </tr>)}
                  {report1099.vendors.length === 0 && <tr><td colSpan={4} className="px-5 py-10 text-center text-gray-500">{t("expenses.no1099VendorsFound")}</td></tr>}
                </tbody>
              </table>}
          </Card>
        </TabsContent>
      </Tabs>

      {/* Expense dialog */}
      <Dialog open={expOpen} onOpenChange={setExpOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg">
          <DialogHeader><DialogTitle>{t("expenses.newExpense")}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("expenses.vendor")}</Label><Input data-testid="exp-vendor" value={exp.vendor} onChange={e => setExp({
              ...exp,
              vendor: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("expenses.amount2")}</Label><Input data-testid="exp-amount" value={exp.amount} onChange={e => setExp({
              ...exp,
              amount: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("expenses.category")}</Label>
              <Select value={exp.category_code} onValueChange={v => setExp({
              ...exp,
              category_code: v
            })}>
                <SelectTrigger data-testid="exp-category" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100 max-h-72">{expenseAccts.map(c => <SelectItem key={c.code} value={c.code}>{c.code} · {c.name}</SelectItem>)}</SelectContent>
              </Select></div>
            <div><Label className="text-xs">{t("expenses.paidVia")}</Label>
              <Select value={exp.payment_method} onValueChange={v => setExp({
              ...exp,
              payment_method: v
            })}>
                <SelectTrigger data-testid="exp-method" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100"><SelectItem value="bank">{t("expenses.bank")}</SelectItem><SelectItem value="card">{t("expenses.card")}</SelectItem><SelectItem value="cash">{t("expenses.cash")}</SelectItem></SelectContent>
              </Select></div>
          </div>
          <div><Label className="text-xs">{t("expenses.description")}</Label><Input data-testid="exp-desc" value={exp.description} onChange={e => setExp({
            ...exp,
            description: e.target.value
          })} className="bg-[#0F1218] border-[#222838]" /></div>
          <div className="border-t border-[#222838] pt-3 text-xs text-gray-500">{t("expenses.receiptMetadataOptional")}</div>
          <div className="grid grid-cols-3 gap-3">
            <div><Label className="text-xs">{t("expenses.merchant")}</Label><Input data-testid="exp-merchant" value={exp.merchant} onChange={e => setExp({
              ...exp,
              merchant: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("expenses.receiptTotal")}</Label><Input data-testid="exp-total" value={exp.total} onChange={e => setExp({
              ...exp,
              total: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">{t("expenses.tax")}</Label><Input data-testid="exp-tax" value={exp.tax} onChange={e => setExp({
              ...exp,
              tax: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          </div>
          <DialogFooter><Button data-testid="exp-save" onClick={saveExpense} className="bg-amber-600 hover:bg-amber-500 text-black">{t("expenses.recordExpense")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Vendor dialog */}
      <Dialog open={venOpen} onOpenChange={setVenOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("expenses.newVendor")}</DialogTitle></DialogHeader>
          <div><Label className="text-xs">{t("expenses.name")}</Label><Input data-testid="ven-name" value={ven.name} onChange={e => setVen({
            ...ven,
            name: e.target.value
          })} className="bg-[#0F1218] border-[#222838]" /></div>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("expenses.email")}</Label><Input data-testid="ven-email" value={ven.email} onChange={e => setVen({
              ...ven,
              email: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("expenses.phone")}</Label><Input data-testid="ven-phone" value={ven.phone} onChange={e => setVen({
              ...ven,
              phone: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <div className="flex items-center gap-2"><Checkbox data-testid="ven-1099" checked={ven.is_1099} onCheckedChange={c => setVen({
            ...ven,
            is_1099: !!c
          })} /><Label className="text-xs">{t("expenses.trackFor1099")}</Label></div>
          {ven.is_1099 && <div><Label className="text-xs">{t("expenses.taxIDEIN")}</Label><Input data-testid="ven-taxid" value={ven.tax_id} onChange={e => setVen({
            ...ven,
            tax_id: e.target.value
          })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>}
          <DialogFooter><Button data-testid="ven-save" onClick={saveVendor} className="bg-amber-600 hover:bg-amber-500 text-black">{t("expenses.addVendor")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Vendor credit dialog */}
      <Dialog open={crOpen} onOpenChange={setCrOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("expenses.newVendorCredit")}</DialogTitle></DialogHeader>
          <div><Label className="text-xs">{t("expenses.vendor")}</Label><Input data-testid="vc-vendor" value={cr.vendor} onChange={e => setCr({
            ...cr,
            vendor: e.target.value
          })} className="bg-[#0F1218] border-[#222838]" /></div>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("expenses.amount2")}</Label><Input data-testid="vc-amount" value={cr.amount} onChange={e => setCr({
              ...cr,
              amount: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">{t("expenses.offsetAccount")}</Label>
              <Select value={cr.expense_code} onValueChange={v => setCr({
              ...cr,
              expense_code: v
            })}>
                <SelectTrigger data-testid="vc-code" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100 max-h-72">{expenseAccts.map(c => <SelectItem key={c.code} value={c.code}>{c.code} · {c.name}</SelectItem>)}</SelectContent>
              </Select></div>
          </div>
          <div><Label className="text-xs">{t("expenses.memo")}</Label><Input data-testid="vc-memo" value={cr.memo} onChange={e => setCr({
            ...cr,
            memo: e.target.value
          })} className="bg-[#0F1218] border-[#222838]" /></div>
          <DialogFooter><Button data-testid="vc-save" onClick={saveCredit} className="bg-amber-600 hover:bg-amber-500 text-black">{t("expenses.recordCredit")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
