import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate, today } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Plus, RefreshCw } from "lucide-react";

const STATUS_CLS = {
  open: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  partial: "bg-sky-500/10 text-sky-400 border-sky-500/20",
  paid: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
};

export default function Bills() {
  const { user } = useAuth();
  const canManage = user?.role === "owner";
  const [bills, setBills] = useState(null);
  const [coa, setCoa] = useState([]);
  const [credits, setCredits] = useState([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ vendor: "", expense_code: "5700", amount: "", due_date: "", recurring: false, frequency: "monthly" });
  const [payBill, setPayBill] = useState(null);
  const [pay, setPay] = useState({ amount: "", payment_method: "bank", credit_id: "" });

  const load = () => api.get("/bills").then(setBills).catch(() => setBills([]));
  useEffect(() => { load(); api.get("/ledger/accounts").then(setCoa).catch(() => {}); api.get("/vendors/credits/list").then(setCredits).catch(() => {}); }, []);
  const expenseAccts = coa.filter((c) => c.type === "expense" && !c.archived);

  const create = async () => {
    try { await api.post("/bills", { vendor: form.vendor, expense_code: form.expense_code, amount: parseFloat(form.amount) || 0, due_date: form.due_date || null, recurring: form.recurring, frequency: form.recurring ? form.frequency : null }); toast.success("Bill created"); setOpen(false); setForm({ vendor: "", expense_code: "5700", amount: "", due_date: "", recurring: false, frequency: "monthly" }); load(); }
    catch (e) { toast.error(errMsg(e)); }
  };
  const doPay = async () => {
    try {
      await api.post(`/bills/${payBill.id}/pay`, { payment_method: pay.payment_method, amount: pay.amount ? parseFloat(pay.amount) : null, credit_id: pay.credit_id || null });
      toast.success("Payment recorded"); setPayBill(null); setPay({ amount: "", payment_method: "bank", credit_id: "" }); load(); api.get("/vendors/credits/list").then(setCredits);
    } catch (e) { toast.error(errMsg(e)); }
  };
  const genNext = async (id) => { try { await api.post(`/bills/${id}/generate-next`); toast.success("Next occurrence created"); load(); } catch (e) { toast.error(errMsg(e)); } };

  if (!bills) return <Loading />;
  const vendorCredits = payBill ? credits.filter((c) => c.vendor === payBill.vendor && c.remaining_cents > 0) : [];

  return (
    <div className="animate-rise">
      <PageHeader title="Bills" subtitle="Accounts payable — partial payments, vendor credits & recurring" testid="bills-page">
        {canManage && <Button data-testid="btn-new-bill" onClick={() => setOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" />New Bill</Button>}
      </PageHeader>
      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm" data-testid="table-bills">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="px-4 py-3 text-left font-medium">Vendor</th><th className="px-4 py-3 text-left font-medium">Due</th>
            <th className="px-4 py-3 text-right font-medium">Amount</th><th className="px-4 py-3 text-right font-medium">Balance</th>
            <th className="px-4 py-3 text-left font-medium">Status</th>{canManage && <th className="px-4 py-3 text-right font-medium">Actions</th>}</tr></thead>
          <tbody>
            {bills.map((b) => (
              <tr key={b.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                <td className="px-4 py-3 text-gray-100">{b.vendor}{b.recurring && <span className="ml-2 text-xs text-amber-500/70">↻ {b.frequency}</span>}</td>
                <td className="px-4 py-3 text-gray-400">{fmtDate(b.due_date)}</td>
                <td className="px-4 py-3 text-right font-mono text-gray-300">{money(b.amount_cents)}</td>
                <td className="px-4 py-3 text-right font-mono text-amber-400">{money(b.balance_cents)}</td>
                <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[b.status] || ""}`}>{b.status}</span></td>
                {canManage && <td className="px-4 py-3 text-right space-x-2">
                  {b.balance_cents > 0 && <Button size="sm" className="h-7 bg-emerald-600 hover:bg-emerald-500 text-black" data-testid={`pay-bill-${b.id}`} onClick={() => { setPayBill(b); setPay({ amount: "", payment_method: "bank", credit_id: "" }); }}>Pay</Button>}
                  {b.recurring && <Button size="sm" variant="outline" className="h-7 border-[#222838]" data-testid={`gen-${b.id}`} onClick={() => genNext(b.id)}><RefreshCw size={13} /></Button>}
                </td>}
              </tr>
            ))}
            {bills.length === 0 && <tr><td colSpan={canManage ? 6 : 5} className="px-5 py-12 text-center text-gray-500">No bills yet.</td></tr>}
          </tbody>
        </table>
      </Card>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>New Bill</DialogTitle></DialogHeader>
          <div><Label className="text-xs">Vendor</Label><Input data-testid="bill-vendor" value={form.vendor} onChange={(e) => setForm({ ...form, vendor: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">Amount ($)</Label><Input data-testid="bill-amount" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">Due date</Label><Input data-testid="bill-due" type="date" value={form.due_date} onChange={(e) => setForm({ ...form, due_date: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <div><Label className="text-xs">Expense account</Label>
            <Select value={form.expense_code} onValueChange={(v) => setForm({ ...form, expense_code: v })}>
              <SelectTrigger data-testid="bill-code" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100 max-h-72">{expenseAccts.map((c) => <SelectItem key={c.code} value={c.code}>{c.code} · {c.name}</SelectItem>)}</SelectContent>
            </Select></div>
          <div className="flex items-center gap-2"><Checkbox data-testid="bill-recurring" checked={form.recurring} onCheckedChange={(c) => setForm({ ...form, recurring: !!c })} /><Label className="text-xs">Recurring bill</Label>
            {form.recurring && <Select value={form.frequency} onValueChange={(v) => setForm({ ...form, frequency: v })}><SelectTrigger className="bg-[#0F1218] border-[#222838] w-32 h-8 ml-2"><SelectValue /></SelectTrigger><SelectContent className="bg-[#14171E] border-[#222838] text-gray-100"><SelectItem value="weekly">Weekly</SelectItem><SelectItem value="monthly">Monthly</SelectItem><SelectItem value="quarterly">Quarterly</SelectItem></SelectContent></Select>}
          </div>
          <DialogFooter><Button data-testid="bill-save" onClick={create} className="bg-amber-600 hover:bg-amber-500 text-black">Create Bill</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!payBill} onOpenChange={(o) => !o && setPayBill(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>Pay {payBill?.vendor}</DialogTitle></DialogHeader>
          {payBill && <div className="text-sm text-gray-400">Balance due: <span className="font-mono text-amber-400">{money(payBill.balance_cents)}</span></div>}
          <div><Label className="text-xs">Amount (blank = full balance)</Label><Input data-testid="paybill-amount" value={pay.amount} onChange={(e) => setPay({ ...pay, amount: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" placeholder="Full balance" /></div>
          <div><Label className="text-xs">Pay from</Label>
            <Select value={pay.payment_method} onValueChange={(v) => setPay({ ...pay, payment_method: v })}>
              <SelectTrigger data-testid="paybill-method" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100"><SelectItem value="bank">Bank</SelectItem><SelectItem value="card">Card</SelectItem><SelectItem value="cash">Cash</SelectItem></SelectContent>
            </Select></div>
          {vendorCredits.length > 0 && <div><Label className="text-xs">Apply vendor credit</Label>
            <Select value={pay.credit_id} onValueChange={(v) => setPay({ ...pay, credit_id: v })}>
              <SelectTrigger data-testid="paybill-credit" className="bg-[#0F1218] border-[#222838]"><SelectValue placeholder="None" /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">{vendorCredits.map((c) => <SelectItem key={c.id} value={c.id}>{money(c.remaining_cents)} — {c.memo}</SelectItem>)}</SelectContent>
            </Select></div>}
          <DialogFooter><Button data-testid="paybill-save" onClick={doPay} className="bg-emerald-600 hover:bg-emerald-500 text-black">Record Payment</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
