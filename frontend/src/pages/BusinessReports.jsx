import { useEffect, useState } from "react";
import { api, errMsg, downloadCsv } from "@/lib/api";
import { dollars, today } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Download } from "lucide-react";
import { toast } from "sonner";

export default function BusinessReports() {
  const [ibc, setIbc] = useState(null);
  const [ebv, setEbv] = useState(null);

  useEffect(() => {
    api.get("/reports/income-by-customer").then(setIbc).catch(() => setIbc({ rows: [], total: 0 }));
    api.get("/reports/expenses-by-vendor").then(setEbv).catch(() => setEbv({ rows: [], total: 0 }));
  }, []);

  if (!ibc || !ebv) return <Loading />;
  return (
    <div className="animate-rise">
      <PageHeader title="Business Reports" subtitle="Income by customer & expenses by vendor" testid="business-reports-page" />
      <Tabs defaultValue="income">
        <TabsList className="bg-[#14171E] border border-[#222838]">
          <TabsTrigger value="income" data-testid="tab-income-customer">Income by Customer</TabsTrigger>
          <TabsTrigger value="expense" data-testid="tab-expense-vendor">Expenses by Vendor</TabsTrigger>
          <TabsTrigger value="booth-rent" data-testid="tab-booth-rent">Booth Rent</TabsTrigger>
        </TabsList>

        <TabsContent value="income" className="mt-4">
          <div className="mb-3 flex justify-end"><Button data-testid="btn-ibc-csv" variant="outline" onClick={() => downloadCsv("/reports/income-by-customer.csv", "income_by_customer.csv")} className="border-[#222838]"><Download size={15} className="mr-1" />CSV</Button></div>
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-ibc">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-4 py-3 text-left font-medium">Customer</th><th className="px-4 py-3 text-right font-medium">Invoices</th>
                <th className="px-4 py-3 text-right font-medium">Paid</th><th className="px-4 py-3 text-right font-medium">Income</th></tr></thead>
              <tbody>
                {ibc.rows.map((r) => (
                  <tr key={r.customer} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-4 py-3 text-gray-100">{r.customer}</td><td className="px-4 py-3 text-right text-gray-400">{r.invoices}</td>
                    <td className="px-4 py-3 text-right font-mono text-gray-400">{dollars(r.paid)}</td>
                    <td className="px-4 py-3 text-right font-mono text-emerald-400">{dollars(r.income)}</td>
                  </tr>
                ))}
                {ibc.rows.length === 0 && <tr><td colSpan={4} className="px-5 py-12 text-center text-gray-500">No customer income yet.</td></tr>}
              </tbody>
              {ibc.rows.length > 0 && <tfoot><tr className="font-semibold"><td className="px-4 py-3 text-gray-300">Total</td><td /><td /><td className="px-4 py-3 text-right font-mono text-amber-400">{dollars(ibc.total)}</td></tr></tfoot>}
            </table>
          </Card>
        </TabsContent>

        <TabsContent value="expense" className="mt-4">
          <div className="mb-3 flex justify-end"><Button data-testid="btn-ebv-csv" variant="outline" onClick={() => downloadCsv("/reports/expenses-by-vendor.csv", "expenses_by_vendor.csv")} className="border-[#222838]"><Download size={15} className="mr-1" />CSV</Button></div>
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-ebv">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-4 py-3 text-left font-medium">Vendor</th><th className="px-4 py-3 text-right font-medium">Transactions</th>
                <th className="px-4 py-3 text-right font-medium">Spend</th></tr></thead>
              <tbody>
                {ebv.rows.map((r) => (
                  <tr key={r.vendor} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-4 py-3 text-gray-100">{r.vendor}</td><td className="px-4 py-3 text-right text-gray-400">{r.count}</td>
                    <td className="px-4 py-3 text-right font-mono text-rose-400">{dollars(r.spend)}</td>
                  </tr>
                ))}
                {ebv.rows.length === 0 && <tr><td colSpan={3} className="px-5 py-12 text-center text-gray-500">No vendor expenses yet.</td></tr>}
              </tbody>
              {ebv.rows.length > 0 && <tfoot><tr className="font-semibold"><td className="px-4 py-3 text-gray-300">Total</td><td /><td className="px-4 py-3 text-right font-mono text-amber-400">{dollars(ebv.total)}</td></tr></tfoot>}
            </table>
          </Card>
        </TabsContent>
        <TabsContent value="booth-rent" className="mt-4">
          <BoothRentTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function BoothRentTab() {
  const { user } = useAuth();
  const canManage = user?.role === "owner";
  const [data, setData] = useState(null);
  const [start, setStart] = useState(today().slice(0, 8) + "01");
  const [end, setEnd] = useState(today());
  const [payOpen, setPayOpen] = useState(false);
  const [payForm, setPayForm] = useState({ barber_id: "", amount: "", payment_method: "bank", pay_date: today() });

  const load = () => api.get(`/reports/booth-rent?date_from=${start}&date_to=${end}`)
    .then(setData).catch(() => setData({ rows: [] }));
  useEffect(() => { load(); }, [start, end]);

  const record = async () => {
    try {
      await api.post("/reports/booth-rent/payment", {
        barber_id: payForm.barber_id, amount: parseFloat(payForm.amount) || 0,
        payment_method: payForm.payment_method, pay_date: payForm.pay_date || null,
      });
      toast.success("Booth rent payment recorded");
      setPayOpen(false);
      setPayForm({ barber_id: "", amount: "", payment_method: "bank", pay_date: today() });
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  if (!data) return <Loading />;
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
        <div className="flex items-end gap-2">
          <div><Label className="text-xs">From</Label><Input type="date" value={start} onChange={(e) => setStart(e.target.value)} className="bg-[#0F1218] border-[#222838] w-40" data-testid="booth-start" /></div>
          <div><Label className="text-xs">To</Label><Input type="date" value={end} onChange={(e) => setEnd(e.target.value)} className="bg-[#0F1218] border-[#222838] w-40" data-testid="booth-end" /></div>
        </div>
        {canManage && <Button data-testid="btn-booth-pay" onClick={() => setPayOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold">Record payment</Button>}
      </div>
      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm" data-testid="table-booth-rent">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="px-4 py-3 text-left font-medium">Team member</th>
            <th className="px-4 py-3 text-right font-medium">Monthly rent</th>
            <th className="px-4 py-3 text-right font-medium">Expected ({data.months} mo)</th>
            <th className="px-4 py-3 text-right font-medium">Collected</th>
            <th className="px-4 py-3 text-right font-medium">Balance due</th></tr></thead>
          <tbody>
            {data.rows.map((r) => (
              <tr key={r.barber_id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                <td className="px-4 py-3 text-gray-100">{r.name}</td>
                <td className="px-4 py-3 text-right font-mono text-gray-400">{dollars(r.monthly_rent)}</td>
                <td className="px-4 py-3 text-right font-mono text-gray-300">{dollars(r.expected)}</td>
                <td className="px-4 py-3 text-right font-mono text-emerald-400">{dollars(r.collected)}</td>
                <td className="px-4 py-3 text-right font-mono text-amber-400">{dollars(r.balance)}</td>
              </tr>
            ))}
            {data.rows.length === 0 && <tr><td colSpan={5} className="px-5 py-12 text-center text-gray-500">No team members with booth rent yet — set booth rent on team members to see them here.</td></tr>}
          </tbody>
          {data.rows.length > 0 && (
            <tfoot><tr className="font-semibold border-t border-[#222838]">
              <td className="px-4 py-3 text-gray-300">Total</td>
              <td />
              <td className="px-4 py-3 text-right font-mono text-gray-300">{dollars(data.total_expected)}</td>
              <td className="px-4 py-3 text-right font-mono text-emerald-400">{dollars(data.total_collected)}</td>
              <td className="px-4 py-3 text-right font-mono text-amber-400">{dollars(data.total_balance)}</td>
            </tr></tfoot>
          )}
        </table>
      </Card>

      <Dialog open={payOpen} onOpenChange={setPayOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>Record booth rent payment</DialogTitle></DialogHeader>
          <div><Label className="text-xs">Team member</Label>
            <select value={payForm.barber_id} onChange={(e) => setPayForm({ ...payForm, barber_id: e.target.value })}
              className="w-full rounded-md bg-[#0F1218] border border-[#222838] px-3 py-2 text-sm text-gray-100" data-testid="booth-pay-member">
              <option value="">Select…</option>
              {data.rows.map((r) => <option key={r.barber_id} value={r.barber_id}>{r.name}</option>)}
            </select></div>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">Amount ($)</Label><Input data-testid="booth-pay-amount" value={payForm.amount} onChange={(e) => setPayForm({ ...payForm, amount: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">Date</Label><Input type="date" data-testid="booth-pay-date" value={payForm.pay_date} onChange={(e) => setPayForm({ ...payForm, pay_date: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <DialogFooter><Button data-testid="booth-pay-save" onClick={record} className="bg-amber-600 hover:bg-amber-500 text-black">Record</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
