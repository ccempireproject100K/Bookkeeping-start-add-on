import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, today } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Plus, Trash2, FilePlus, Play, Pencil } from "lucide-react";
import { useTranslation } from "react-i18next";
const FREQ_LABEL = {
  weekly: "Weekly",
  monthly: "Monthly"
};
export default function RecurringInvoices() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const isOwner = user?.role === "owner";
  const [tpls, setTpls] = useState(null);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState({
    name: "",
    client_name: "",
    tax: "",
    frequency: "monthly",
    next_run: today(),
    active: true
  });
  const [lines, setLines] = useState([{
    name: "",
    revenue_code: "4020",
    amount: ""
  }]);
  const resetForm = () => {
    setEditing(null);
    setForm({
      name: "",
      client_name: "",
      tax: "",
      frequency: "monthly",
      next_run: today(),
      active: true
    });
    setLines([{
      name: "",
      revenue_code: "4020",
      amount: ""
    }]);
  };
  const load = () => api.get("/recurring-invoices").then(setTpls).catch(() => setTpls([]));
  useEffect(() => {
    load();
  }, []);
  const save = async () => {
    const payload = {
      name: form.name,
      client_name: form.client_name,
      tax: parseFloat(form.tax) || 0,
      frequency: form.frequency,
      next_run: form.next_run || null,
      active: form.active,
      lines: lines.filter(l => l.name && l.amount).map(l => ({
        kind: "service",
        name: l.name,
        revenue_code: l.revenue_code,
        amount: parseFloat(l.amount) || 0
      }))
    };
    try {
      if (editing) {
        await api.patch(`/recurring-invoices/${editing.id}`, payload);
        toast.success(t("recurringinvoices.templateUpdated"));
      } else {
        await api.post("/recurring-invoices", payload);
        toast.success(t("recurringinvoices.templateCreated"));
      }
      setOpen(false);
      resetForm();
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const openEdit = t => {
    setEditing(t);
    setForm({
      name: t.name || "",
      client_name: t.client_name || "",
      tax: t.tax || "",
      frequency: t.frequency || "monthly",
      next_run: t.next_run || today(),
      active: t.active !== false
    });
    setLines((t.lines || []).map(l => ({
      name: l.name || "",
      revenue_code: l.revenue_code || "4020",
      amount: l.amount || ""
    })));
    setOpen(true);
  };
  const runDue = async () => {
    try {
      const r = await api.post("/recurring-invoices/run-due");
      toast.success(r.generated === 0 ? "Nothing due yet" : `Generated ${r.generated} invoice(s)`);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const del = async t => {
    if (!window.confirm(`Delete template "${t.name}"?`)) return;
    try {
      await api.del(`/recurring-invoices/${t.id}`);
      toast.success(t("recurringinvoices.templateDeleted"));
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (tpls === null) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("recurringinvoices.recurringInvoices")} subtitle="Templates that generate real invoices on schedule" testid="recurring-invoices-page">
        {isOwner && <div className="flex gap-2">
            <Button variant="outline" data-testid="btn-run-due" onClick={runDue} className="border-[#222838]"><Play size={15} className="me-1" /> {t("recurringinvoices.generateDue")}</Button>
            <Button data-testid="btn-new-template" onClick={() => {
          resetForm();
          setOpen(true);
        }} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="me-1" /> {t("recurringinvoices.newTemplate")}</Button>
          </div>}
      </PageHeader>

      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm" data-testid="table-recurring">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="px-5 py-3 text-start font-medium">{t("recurringinvoices.template")}</th>
            <th className="px-5 py-3 text-start font-medium">{t("recurringinvoices.client")}</th>
            <th className="px-5 py-3 text-start font-medium">{t("recurringinvoices.frequency")}</th>
            <th className="px-5 py-3 text-start font-medium">{t("recurringinvoices.nextRun")}</th>
            <th className="px-5 py-3 text-start font-medium">{t("recurringinvoices.status")}</th>
            <th className="px-5 py-3 text-end font-medium">{t("recurringinvoices.estTotal")}</th>
            {isOwner && <th className="px-5 py-3 text-end font-medium">{t("recurringinvoices.actions")}</th>}</tr></thead>
          <tbody>
            {tpls.map(t => {
            const est = (t.lines || []).reduce((s, l) => s + (parseFloat(l.amount) || 0), 0) + (parseFloat(t.tax) || 0);
            return <tr key={t.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                  <td className="px-5 py-3 text-gray-100">{t.name}</td>
                  <td className="px-5 py-3 text-gray-400">{t.client_name}</td>
                  <td className="px-5 py-3 text-gray-400 capitalize">{FREQ_LABEL[t.frequency] || t.frequency}</td>
                  <td className="px-5 py-3 text-gray-400 font-mono text-xs">{t.next_run || "—"}</td>
                  <td className="px-5 py-3">{t.active ? <span className="px-2 py-0.5 rounded-full text-xs bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">{t("recurringinvoices.active")}</span> : <span className="px-2 py-0.5 rounded-full text-xs bg-gray-500/10 text-gray-400 border border-gray-500/20">{t("recurringinvoices.paused")}</span>}</td>
                  <td className="px-5 py-3 text-end font-mono text-gray-300">${est.toFixed(2)}</td>
                  {isOwner && <td className="px-5 py-3 text-end">
                      <div className="flex justify-end gap-1">
                        <button onClick={() => openEdit(t)} aria-label={`Edit ${t.name}`} data-testid={`template-edit-${t.id}`} className="inline-flex items-center justify-center w-8 h-8 rounded text-gray-500 hover:text-amber-400 hover:bg-amber-500/10"><Pencil size={15} /></button>
                        <button onClick={() => del(t)} aria-label={`Delete ${t.name}`} data-testid={`template-delete-${t.id}`} className="inline-flex items-center justify-center w-8 h-8 rounded text-gray-500 hover:text-rose-400 hover:bg-rose-500/10"><Trash2 size={15} /></button>
                      </div>
                    </td>}
                </tr>;
          })}
            {tpls.length === 0 && <tr><td colSpan={isOwner ? 7 : 6} className="px-5 py-12 text-center text-gray-500">{t("recurringinvoices.noTemplatesYet")}</td></tr>}
          </tbody>
        </table>
      </Card>

      <Dialog open={open} onOpenChange={o => {
      setOpen(o);
      if (!o) resetForm();
    }}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg">
          <DialogHeader><DialogTitle>{editing ? "Edit Recurring Template" : "New Recurring Template"}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("recurringinvoices.templateName")}</Label><Input data-testid="tpl-name" value={form.name} onChange={e => setForm({
              ...form,
              name: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("recurringinvoices.client")}</Label><Input data-testid="tpl-client" value={form.client_name} onChange={e => setForm({
              ...form,
              client_name: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <div className="space-y-2">
            <Label className="text-xs">{t("recurringinvoices.lineItems")}</Label>
            {lines.map((l, i) => <div key={i} className="flex gap-2 items-center">
                <Input placeholder={t("recurringinvoices.description")} value={l.name} onChange={e => {
              const c = [...lines];
              c[i].name = e.target.value;
              setLines(c);
            }} className="bg-[#0F1218] border-[#222838] flex-1" data-testid={`tpl-line-name-${i}`} />
                <Input placeholder="0.00" value={l.amount} onChange={e => {
              const c = [...lines];
              c[i].amount = e.target.value;
              setLines(c);
            }} className="bg-[#0F1218] border-[#222838] w-28 font-mono text-end" data-testid={`tpl-line-amount-${i}`} />
                {lines.length > 1 && <button onClick={() => setLines(lines.filter((_, x) => x !== i))} className="text-gray-600 hover:text-rose-400"><Trash2 size={14} /></button>}
              </div>)}
            <Button variant="ghost" size="sm" onClick={() => setLines([...lines, {
            name: "",
            revenue_code: "4020",
            amount: ""
          }])} className="text-amber-400 hover:bg-amber-500/10"><FilePlus size={14} className="me-1" />{t("recurringinvoices.addLine")}</Button>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div><Label className="text-xs">{t("recurringinvoices.tax")}</Label><Input data-testid="tpl-tax" value={form.tax} onChange={e => setForm({
              ...form,
              tax: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">{t("recurringinvoices.frequency")}</Label>
              <Select value={form.frequency} onValueChange={v => setForm({
              ...form,
              frequency: v
            })}>
                <SelectTrigger data-testid="tpl-frequency" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100"><SelectItem value="weekly">{t("recurringinvoices.weekly")}</SelectItem><SelectItem value="monthly">{t("recurringinvoices.monthly")}</SelectItem></SelectContent>
              </Select></div>
            <div><Label className="text-xs">{t("recurringinvoices.nextRun")}</Label><Input type="date" data-testid="tpl-next-run" value={form.next_run} onChange={e => setForm({
              ...form,
              next_run: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <label className="flex items-center gap-2 text-sm text-gray-300">
            <input type="checkbox" checked={form.active} onChange={e => setForm({
            ...form,
            active: e.target.checked
          })} className="accent-amber-500" data-testid="tpl-active" />{t("recurringinvoices.activeGenerateInvoicesOnSchedule")}</label>
          <DialogFooter><Button data-testid="tpl-save" onClick={save} className="bg-amber-600 hover:bg-amber-500 text-black">{editing ? "Save changes" : "Save Template"}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
