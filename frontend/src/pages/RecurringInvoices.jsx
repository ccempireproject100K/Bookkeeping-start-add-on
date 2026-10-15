import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, today } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { Plus, Trash2, FilePlus, Play } from "lucide-react";

const FREQ_LABEL = { weekly: "Weekly", monthly: "Monthly" };

export default function RecurringInvoices() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [tpls, setTpls] = useState(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", client_name: "", tax: "", frequency: "monthly", next_run: today(), active: true });
  const [lines, setLines] = useState([{ name: "", revenue_code: "4020", amount: "" }]);

  const load = () => api.get("/recurring-invoices").then(setTpls).catch(() => setTpls([]));
  useEffect(() => { load(); }, []);

  const create = async () => {
    try {
      await api.post("/recurring-invoices", {
        name: form.name, client_name: form.client_name, tax: parseFloat(form.tax) || 0,
        frequency: form.frequency, next_run: form.next_run || null, active: form.active,
        lines: lines.filter((l) => l.name && l.amount).map((l) => ({ kind: "service", name: l.name, revenue_code: l.revenue_code, amount: parseFloat(l.amount) || 0 })),
      });
      toast.success("Template created");
      setOpen(false);
      setForm({ name: "", client_name: "", tax: "", frequency: "monthly", next_run: today(), active: true });
      setLines([{ name: "", revenue_code: "4020", amount: "" }]);
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };
  const runDue = async () => {
    try {
      const r = await api.post("/recurring-invoices/run-due");
      toast.success(r.generated === 0 ? "Nothing due yet" : `Generated ${r.generated} invoice(s)`);
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };
  const del = async (t) => {
    if (!window.confirm(`Delete template "${t.name}"?`)) return;
    try { await api.del(`/recurring-invoices/${t.id}`); toast.success("Template deleted"); load(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  if (tpls === null) return <Loading />;
  return (
    <div className="animate-rise">
      <PageHeader title="Recurring Invoices" subtitle="Templates that generate real invoices on schedule" testid="recurring-invoices-page">
        {isOwner && (
          <div className="flex gap-2">
            <Button variant="outline" data-testid="btn-run-due" onClick={runDue} className="border-[#222838]"><Play size={15} className="mr-1" /> Generate due</Button>
            <Button data-testid="btn-new-template" onClick={() => setOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" /> New Template</Button>
          </div>
        )}
      </PageHeader>

      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm" data-testid="table-recurring">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="px-5 py-3 text-left font-medium">Template</th>
            <th className="px-5 py-3 text-left font-medium">Client</th>
            <th className="px-5 py-3 text-left font-medium">Frequency</th>
            <th className="px-5 py-3 text-left font-medium">Next run</th>
            <th className="px-5 py-3 text-left font-medium">Status</th>
            <th className="px-5 py-3 text-right font-medium">Est. total</th>
            {isOwner && <th className="px-5 py-3 text-right font-medium">Actions</th>}</tr></thead>
          <tbody>
            {tpls.map((t) => {
              const est = (t.lines || []).reduce((s, l) => s + (parseFloat(l.amount) || 0), 0) + (parseFloat(t.tax) || 0);
              return (
                <tr key={t.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                  <td className="px-5 py-3 text-gray-100">{t.name}</td>
                  <td className="px-5 py-3 text-gray-400">{t.client_name}</td>
                  <td className="px-5 py-3 text-gray-400 capitalize">{FREQ_LABEL[t.frequency] || t.frequency}</td>
                  <td className="px-5 py-3 text-gray-400 font-mono text-xs">{t.next_run || "—"}</td>
                  <td className="px-5 py-3">{t.active
                    ? <span className="px-2 py-0.5 rounded-full text-xs bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">Active</span>
                    : <span className="px-2 py-0.5 rounded-full text-xs bg-gray-500/10 text-gray-400 border border-gray-500/20">Paused</span>}</td>
                  <td className="px-5 py-3 text-right font-mono text-gray-300">${est.toFixed(2)}</td>
                  {isOwner && (
                    <td className="px-5 py-3 text-right">
                      <button onClick={() => del(t)} aria-label={`Delete ${t.name}`} data-testid={`template-delete-${t.id}`}
                        className="inline-flex items-center justify-center w-8 h-8 rounded text-gray-500 hover:text-rose-400 hover:bg-rose-500/10"><Trash2 size={15} /></button>
                    </td>
                  )}
                </tr>
              );
            })}
            {tpls.length === 0 && <tr><td colSpan={isOwner ? 7 : 6} className="px-5 py-12 text-center text-gray-500">No templates yet.</td></tr>}
          </tbody>
        </table>
      </Card>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg">
          <DialogHeader><DialogTitle>New Recurring Template</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">Template name</Label><Input data-testid="tpl-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">Client</Label><Input data-testid="tpl-client" value={form.client_name} onChange={(e) => setForm({ ...form, client_name: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <div className="space-y-2">
            <Label className="text-xs">Line items</Label>
            {lines.map((l, i) => (
              <div key={i} className="flex gap-2 items-center">
                <Input placeholder="Description" value={l.name} onChange={(e) => { const c = [...lines]; c[i].name = e.target.value; setLines(c); }} className="bg-[#0F1218] border-[#222838] flex-1" data-testid={`tpl-line-name-${i}`} />
                <Input placeholder="0.00" value={l.amount} onChange={(e) => { const c = [...lines]; c[i].amount = e.target.value; setLines(c); }} className="bg-[#0F1218] border-[#222838] w-28 font-mono text-right" data-testid={`tpl-line-amount-${i}`} />
                {lines.length > 1 && <button onClick={() => setLines(lines.filter((_, x) => x !== i))} className="text-gray-600 hover:text-rose-400"><Trash2 size={14} /></button>}
              </div>
            ))}
            <Button variant="ghost" size="sm" onClick={() => setLines([...lines, { name: "", revenue_code: "4020", amount: "" }])} className="text-amber-400 hover:bg-amber-500/10"><FilePlus size={14} className="mr-1" />Add line</Button>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div><Label className="text-xs">Tax ($)</Label><Input data-testid="tpl-tax" value={form.tax} onChange={(e) => setForm({ ...form, tax: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">Frequency</Label>
              <Select value={form.frequency} onValueChange={(v) => setForm({ ...form, frequency: v })}>
                <SelectTrigger data-testid="tpl-frequency" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100"><SelectItem value="weekly">Weekly</SelectItem><SelectItem value="monthly">Monthly</SelectItem></SelectContent>
              </Select></div>
            <div><Label className="text-xs">Next run</Label><Input type="date" data-testid="tpl-next-run" value={form.next_run} onChange={(e) => setForm({ ...form, next_run: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <DialogFooter><Button data-testid="tpl-save" onClick={create} className="bg-amber-600 hover:bg-amber-500 text-black">Save Template</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
