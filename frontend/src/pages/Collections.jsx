import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { toast } from "sonner";
import { Play, Eye, Settings2, PauseCircle } from "lucide-react";

const ACTIONS = ["nudge", "firm", "final", "pause_service"];
const ACTION_LABEL = { nudge: "Friendly nudge", firm: "Firm reminder", final: "Final notice", pause_service: "Pause service" };

export default function Collections() {
  const { user } = useAuth();
  const canManage = user?.role === "owner";
  const [loaded, setLoaded] = useState(false);
  const [pipe, setPipe] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [cfgOpen, setCfgOpen] = useState(false);
  const [preview, setPreview] = useState(null);
  const [running, setRunning] = useState(false);
  const [form, setForm] = useState(null);

  const reload = () => {
    api.get("/collections/pipeline").then(setPipe).catch(() => {});
    api.get("/collections/config").then((c) => { setCfg(c); setForm(c); }).catch(() => {});
  };
  useEffect(() => { reload(); setLoaded(true); }, []);

  const saveCfg = async () => {
    try { await api.put("/collections/config", form); toast.success("Autopilot config saved"); setCfgOpen(false); reload(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const doPreview = async () => {
    try { setPreview(await api.post("/collections/autopilot/preview")); }
    catch (e) { toast.error(errMsg(e)); setPreview(null); }
  };

  const doRun = async () => {
    if (!confirm(`Run the autopilot on ${preview?.count ?? "all due"} overdue invoices?`)) return;
    setRunning(true);
    try {
      const res = await api.post("/collections/autopilot/run");
      toast.success(`${res.actions.length} actions taken${res.errors.length ? `, ${res.errors.length} errors` : ""}`);
      setPreview(null); reload();
    } catch (e) { toast.error(errMsg(e)); }
    setRunning(false);
  };

  const updStep = (i, k, v) => {
    const steps = form.steps.map((s, j) => j === i ? { ...s, [k]: v } : s);
    setForm({ ...form, steps });
  };

  if (!loaded) return <Loading />;
  const buckets = {};
  (pipe?.rows || []).forEach((r) => { (buckets[r.bucket] = buckets[r.bucket] || []).push(r); });
  return (
    <div className="space-y-6">
      <PageHeader title="Collections Autopilot" subtitle="Escalating reminders, late fees, and service holds — you approve the ladder, it works the list."
        actions={canManage && <>
          <Button variant="outline" onClick={() => setCfgOpen(true)}><Settings2 size={14} className="mr-1" />Ladder config</Button>
          <Button variant="outline" onClick={doPreview}><Eye size={14} className="mr-1" />Preview</Button>
          <Button onClick={doRun} disabled={running || !cfg?.enabled} data-testid="collections-run">
            <Play size={14} className="mr-1" />{running ? "Running…" : "Run autopilot"}</Button>
        </>} />
      {!cfg?.enabled && <Card className="p-4 text-sm text-amber-400 bg-amber-500/5">Autopilot is off. Open Ladder config to enable it.</Card>}
      <div className="grid grid-cols-2 gap-4">
        <Card className="p-4"><div className="text-xs text-gray-500">Overdue invoices</div><div className="text-2xl font-semibold">{pipe?.count ?? 0}</div></Card>
        <Card className="p-4"><div className="text-xs text-gray-500">Total overdue</div><div className="text-2xl font-semibold text-red-400">{money(pipe?.total_overdue_cents ?? 0)}</div></Card>
      </div>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {["1-30", "31-60", "61-90", "90+"].map((b) => (
          <Card key={b} className="p-4">
            <div className="text-xs text-gray-500 mb-1">{b} days · {(buckets[b] || []).length} invoices</div>
            <div className="text-xl font-semibold">{money((buckets[b] || []).reduce((s, r) => s + r.balance_cents, 0))}</div>
          </Card>
        ))}
      </div>
      {preview && (
        <Card className="p-4">
          <div className="text-sm font-medium mb-2">Preview — {preview.count} actions would run</div>
          <div className="max-h-48 overflow-auto text-sm">
            {preview.actions.map((a, i) => (
              <div key={i} className="py-1 border-b border-[#1a2030] text-gray-300">
                <span className="text-xs px-2 py-0.5 rounded-full bg-blue-500/15 text-blue-400 mr-2">{ACTION_LABEL[a.action] || a.action}</span>
                {a.client_name} · {a.days_overdue}d overdue · {money(a.balance_cents)}
              </div>
            ))}
            {!preview.actions.length && <p className="text-gray-500 text-sm">Nothing due — the ladder is caught up.</p>}
          </div>
        </Card>
      )}
      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead><tr className="text-left text-gray-500 border-b border-[#222838]">
            <th className="px-4 py-3 font-medium">Client</th><th className="px-4 py-3 font-medium">Due</th>
            <th className="px-4 py-3 font-medium text-right">Overdue</th><th className="px-4 py-3 font-medium text-right">Balance</th>
            <th className="px-4 py-3 font-medium">Stages done</th><th className="px-4 py-3 font-medium">Next</th><th className="px-4 py-3"></th>
          </tr></thead>
          <tbody>
            {(pipe?.rows || []).map((r) => (
              <tr key={r.invoice_id} className="border-b border-[#1a2030] hover:bg-[#141926]">
                <td className="px-4 py-3">{r.client_name}</td>
                <td className="px-4 py-3 text-xs text-gray-400">{r.due_date ? fmtDate(r.due_date) : "—"}</td>
                <td className="px-4 py-3 text-right text-red-400">{r.days_overdue}d</td>
                <td className="px-4 py-3 text-right font-mono">{money(r.balance_cents)}</td>
                <td className="px-4 py-3">{(r.stages_done || []).map((s) => (
                  <span key={s} className="text-xs px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 mr-1">{s}</span>))}
                  {!r.stages_done?.length && <span className="text-gray-600 text-xs">—</span>}</td>
                <td className="px-4 py-3 text-xs text-blue-400">{r.next_step ? ACTION_LABEL[r.next_step.action] : "caught up"}</td>
                <td className="px-4 py-3 text-right">{r.on_hold && <PauseCircle size={15} className="inline text-amber-400" title="Service paused" />}</td>
              </tr>
            ))}
            {!(pipe?.rows || []).length && <tr><td colSpan={7} className="px-4 py-8 text-center text-gray-500 text-sm">Nothing overdue. Nice.</td></tr>}
          </tbody>
        </table>
      </Card>

      <Dialog open={cfgOpen} onOpenChange={setCfgOpen}>
        <DialogContent className="max-w-2xl max-h-[85vh] overflow-auto">
          <DialogHeader><DialogTitle>Autopilot ladder</DialogTitle></DialogHeader>
          {form && <>
            <div className="flex items-center gap-2 mb-2">
              <Switch checked={form.enabled} onCheckedChange={(v) => setForm({ ...form, enabled: v })} />
              <Label className="text-sm">Autopilot enabled</Label>
            </div>
            <div className="space-y-2">
              {form.steps.map((s, i) => (
                <div key={i} className="grid grid-cols-[90px_150px_1fr] gap-2 items-start p-2 rounded bg-[#0F1218]">
                  <div><Label className="text-xs">Days</Label><Input type="number" value={s.days} onChange={(e) => updStep(i, "days", parseInt(e.target.value) || 0)} className="border-[#222838] font-mono" /></div>
                  <div><Label className="text-xs">Action</Label>
                    <Select value={s.action} onValueChange={(v) => updStep(i, "action", v)}>
                      <SelectTrigger className="border-[#222838]"><SelectValue /></SelectTrigger>
                      <SelectContent>{ACTIONS.map((a) => <SelectItem key={a} value={a}>{ACTION_LABEL[a]}</SelectItem>)}</SelectContent>
                    </Select></div>
                  <div><Label className="text-xs">Message</Label>
                    <textarea value={s.template || ""} rows={2} onChange={(e) => updStep(i, "template", e.target.value)}
                      className="w-full text-xs bg-[#141926] border border-[#222838] rounded p-2" placeholder="Use {client_name} {invoice_ref} {balance} {days} {due_date}" /></div>
                </div>
              ))}
            </div>
            <div className="grid grid-cols-4 gap-2 mt-3">
              <div><Label className="text-xs">Fee from day</Label><Input type="number" value={form.late_fee_days} onChange={(e) => setForm({ ...form, late_fee_days: parseInt(e.target.value) || 0 })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
              <div><Label className="text-xs">Fee %</Label><Input type="number" step="0.1" value={form.late_fee_percent} onChange={(e) => setForm({ ...form, late_fee_percent: parseFloat(e.target.value) || 0 })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
              <div><Label className="text-xs">Min fee $</Label><Input type="number" step="0.01" value={(form.late_fee_min_cents / 100).toFixed(2)} onChange={(e) => setForm({ ...form, late_fee_min_cents: Math.round(parseFloat(e.target.value || 0) * 100) })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
              <div><Label className="text-xs">Pause from day</Label><Input type="number" value={form.pause_service_days} onChange={(e) => setForm({ ...form, pause_service_days: parseInt(e.target.value) || 0 })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            </div>
          </>}
          <DialogFooter><Button variant="ghost" onClick={() => setCfgOpen(false)}>Cancel</Button><Button onClick={saveCfg}>Save ladder</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
