import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { toast } from "sonner";
import { Plus, Trash2, FilePlus } from "lucide-react";

const STATUS_CLS = {
  issued: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  paid: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
};

export default function Invoices() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [invoices, setInvoices] = useState(null);
  const [open, setOpen] = useState(false);
  const [client, setClient] = useState("");
  const [tax, setTax] = useState("");
  const [lines, setLines] = useState([{ name: "", revenue_code: "4020", amount: "" }]);

  const load = () => api.get("/invoices").then(setInvoices).catch(() => setInvoices([]));
  useEffect(() => { load(); }, []);

  const create = async () => {
    try {
      await api.post("/invoices", {
        client_name: client, tax: parseFloat(tax) || 0,
        lines: lines.filter((l) => l.name && l.amount).map((l) => ({ kind: "service", name: l.name, revenue_code: l.revenue_code, amount: parseFloat(l.amount) || 0 })),
      });
      toast.success("Invoice issued");
      setOpen(false); setClient(""); setTax(""); setLines([{ name: "", revenue_code: "4020", amount: "" }]); load();
    } catch (e) { toast.error(errMsg(e)); }
  };
  const pay = async (id) => {
    try { await api.post(`/invoices/${id}/pay`, { payment_method: "bank" }); toast.success("Invoice marked paid"); load(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  if (!invoices) return <Loading />;

  return (
    <div className="animate-rise">
      <PageHeader title="Invoices" subtitle="Credit sales & accounts receivable" testid="invoices-page">
        {isOwner && <Button data-testid="btn-new-invoice" onClick={() => setOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" /> New Invoice</Button>}
      </PageHeader>

      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm" data-testid="table-invoices">
          <thead>
            <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
              <th className="px-5 py-3 text-left font-medium">Client</th>
              <th className="px-5 py-3 text-left font-medium">Issued</th>
              <th className="px-5 py-3 text-right font-medium">Total</th>
              <th className="px-5 py-3 text-left font-medium">Status</th>
              {isOwner && <th className="px-5 py-3 text-right font-medium">Actions</th>}
            </tr>
          </thead>
          <tbody>
            {invoices.map((inv) => (
              <tr key={inv.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                <td className="px-5 py-3 text-gray-100">{inv.client_name}</td>
                <td className="px-5 py-3 text-gray-400">{fmtDate(inv.created_at?.slice(0, 10))}</td>
                <td className="px-5 py-3 text-right font-mono text-gray-200">{money(inv.total_cents)}</td>
                <td className="px-5 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[inv.status] || ""}`}>{inv.status}</span></td>
                {isOwner && (
                  <td className="px-5 py-3 text-right">
                    {inv.status === "issued" && <Button size="sm" className="h-7 bg-emerald-600 hover:bg-emerald-500 text-black" onClick={() => pay(inv.id)} data-testid={`pay-${inv.id}`}>Mark Paid</Button>}
                  </td>
                )}
              </tr>
            ))}
            {invoices.length === 0 && <tr><td colSpan={isOwner ? 5 : 4} className="px-5 py-12 text-center text-gray-500">No invoices</td></tr>}
          </tbody>
        </table>
      </Card>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg">
          <DialogHeader><DialogTitle>New Invoice</DialogTitle></DialogHeader>
          <div><Label className="text-xs">Client</Label><Input data-testid="invoice-client" value={client} onChange={(e) => setClient(e.target.value)} className="bg-[#0F1218] border-[#222838]" /></div>
          <div className="space-y-2">
            <Label className="text-xs">Line items</Label>
            {lines.map((l, i) => (
              <div key={i} className="flex gap-2 items-center">
                <Input placeholder="Description" value={l.name} onChange={(e) => { const c = [...lines]; c[i].name = e.target.value; setLines(c); }} className="bg-[#0F1218] border-[#222838] flex-1" data-testid={`invoice-line-name-${i}`} />
                <Input placeholder="0.00" value={l.amount} onChange={(e) => { const c = [...lines]; c[i].amount = e.target.value; setLines(c); }} className="bg-[#0F1218] border-[#222838] w-28 font-mono text-right" data-testid={`invoice-line-amount-${i}`} />
                {lines.length > 1 && <button onClick={() => setLines(lines.filter((_, x) => x !== i))} className="text-gray-600 hover:text-rose-400"><Trash2 size={14} /></button>}
              </div>
            ))}
            <Button variant="ghost" size="sm" onClick={() => setLines([...lines, { name: "", revenue_code: "4020", amount: "" }])} className="text-amber-400 hover:bg-amber-500/10"><FilePlus size={14} className="mr-1" />Add line</Button>
          </div>
          <div><Label className="text-xs">Tax ($)</Label><Input data-testid="invoice-tax" value={tax} onChange={(e) => setTax(e.target.value)} className="bg-[#0F1218] border-[#222838] w-32 font-mono" /></div>
          <DialogFooter><Button data-testid="invoice-save" onClick={create} className="bg-amber-600 hover:bg-amber-500 text-black">Issue Invoice</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
