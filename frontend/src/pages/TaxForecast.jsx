import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Landmark, Plus } from "lucide-react";

export default function TaxForecast() {
  const [loaded, setLoaded] = useState(false);
  const [fc, setFc] = useState(null);
  const [payOpen, setPayOpen] = useState(false);
  const [pay, setPay] = useState({ quarter: "Q4", amount_cents: "", paid_date: "", note: "" });

  const reload = () => api.get("/tax-forecast?year=2026").then(setFc).catch((e) => toast.error(errMsg(e)));
  useEffect(() => { reload(); setLoaded(true); }, []);

  const savePay = async () => {
    const amt = Math.round(parseFloat(pay.amount_cents || 0) * 100);
    if (!amt) { toast.error("Enter the payment amount"); return; }
    try {
      await api.post("/tax-forecast/payments?year=2026", { ...pay, amount_cents: amt });
      toast.success("Payment recorded"); setPayOpen(false);
      setPay({ quarter: "Q4", amount_cents: "", paid_date: "", note: "" }); reload();
    } catch (e) { toast.error(errMsg(e)); }
  };

  if (!loaded) return <Loading />;
  const full = fc?.full_year_estimate || {};
  return (
    <div className="space-y-6">
      <PageHeader title="Tax Forecast" subtitle="Your estimated bill, alive — recalculated with every transaction."
        actions={<Button variant="outline" onClick={() => setPayOpen(true)}><Plus size={14} className="mr-1" />Record estimated payment</Button>} />
      {fc?.pace_note && (
        <Card className="p-5 bg-blue-500/5">
          <div className="flex items-start gap-3">
            <Landmark size={20} className="text-blue-400 mt-0.5 shrink-0" />
            <p className="text-[15px]">{fc.pace_note}</p>
          </div>
        </Card>
      )}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <Card className="p-4"><div className="text-xs text-gray-500">Projected year owed</div>
          <div className="text-2xl font-semibold">{money(full.total ?? 0)}</div>
          <div className="text-xs text-gray-500">effective {full.effective_rate ?? 0}%</div></Card>
        <Card className="p-4"><div className="text-xs text-gray-500">Paid so far</div>
          <div className="text-2xl font-semibold text-emerald-400">{money(fc?.paid_cents ?? 0)}</div></Card>
        <Card className="p-4"><div className="text-xs text-gray-500">Remaining</div>
          <div className="text-2xl font-semibold text-amber-400">{money(fc?.remaining_cents ?? 0)}</div></Card>
        <Card className="p-4"><div className="text-xs text-gray-500">Annualized net income</div>
          <div className="text-2xl font-semibold">{money(fc?.annualized_net_cents ?? 0)}</div>
          <div className="text-xs text-gray-500">YTD {money(fc?.ytd_net_cents ?? 0)}</div></Card>
      </div>
      <Card className="p-0 overflow-hidden">
        <div className="px-4 py-3 text-sm font-medium border-b border-[#222838]">Quarterly breakdown</div>
        <table className="w-full text-sm">
          <thead><tr className="text-left text-gray-500 border-b border-[#222838]">
            <th className="px-4 py-2 font-medium">Quarter</th><th className="px-4 py-2 font-medium">Due</th>
            <th className="px-4 py-2 font-medium text-right">Owed</th><th className="px-4 py-2 font-medium text-right">Paid</th>
            <th className="px-4 py-2 font-medium text-right">Remaining</th>
          </tr></thead>
          <tbody>
            {(fc?.quarters || []).map((q) => (
              <tr key={q.quarter} className="border-b border-[#1a2030]">
                <td className="px-4 py-2.5 font-medium">{q.quarter}</td>
                <td className="px-4 py-2.5 text-xs text-gray-400">{q.due}</td>
                <td className="px-4 py-2.5 text-right font-mono">{money(q.owed_cents)}</td>
                <td className="px-4 py-2.5 text-right font-mono text-emerald-400">{money(q.paid_cents)}</td>
                <td className={`px-4 py-2.5 text-right font-mono ${q.remaining_cents > 0 ? "text-amber-400" : "text-gray-500"}`}>{money(q.remaining_cents)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <p className="text-xs text-gray-600">Estimate only — SE tax at 15.3% × 92.35%, federal income tax on simplified brackets after the standard deduction. Not tax advice; confirm with your preparer.</p>

      <Dialog open={payOpen} onOpenChange={setPayOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader><DialogTitle>Record estimated payment</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">Quarter</Label>
              <Select value={pay.quarter} onValueChange={(v) => setPay({ ...pay, quarter: v })}>
                <SelectTrigger className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                <SelectContent>{["Q1", "Q2", "Q3", "Q4"].map((q) => <SelectItem key={q} value={q}>{q}</SelectItem>)}</SelectContent>
              </Select></div>
            <div><Label className="text-xs">Amount $</Label><Input type="number" value={pay.amount_cents} onChange={(e) => setPay({ ...pay, amount_cents: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">Date paid</Label><Input type="date" value={pay.paid_date} onChange={(e) => setPay({ ...pay, paid_date: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">Note</Label><Input value={pay.note} onChange={(e) => setPay({ ...pay, note: e.target.value })} placeholder="IRS Direct Pay" className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <DialogFooter><Button variant="ghost" onClick={() => setPayOpen(false)}>Cancel</Button><Button onClick={savePay}>Save</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
