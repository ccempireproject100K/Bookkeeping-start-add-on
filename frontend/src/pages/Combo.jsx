import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Plus, Phone, MessageSquare, ArrowRight, Sparkles, Trash2 } from "lucide-react";

const STAGES = ["new", "contacted", "quoted", "won", "lost"];
const STAGE_CLS = { new: "bg-blue-500/15 text-blue-400", contacted: "bg-sky-500/15 text-sky-400",
  quoted: "bg-amber-500/15 text-amber-400", won: "bg-emerald-500/15 text-emerald-400", lost: "bg-gray-500/15 text-gray-400" };

export default function Combo() {
  const { user } = useAuth();
  const canManage = user?.role === "owner";
  const [loaded, setLoaded] = useState(false);
  const [funnel, setFunnel] = useState(null);
  const [leads, setLeads] = useState([]);
  const [leadOpen, setLeadOpen] = useState(false);
  const [detail, setDetail] = useState(null);
  const [interactions, setInteractions] = useState([]);
  const [draft, setDraft] = useState(null);
  const [lead, setLead] = useState({ name: "", phone: "", email: "", source: "call", service_needed: "", notes: "" });
  const [ix, setIx] = useState({ kind: "call", direction: "inbound", summary: "" });
  const [conv, setConv] = useState({ amount: "", description: "" });

  const reload = () => {
    api.get("/combo/funnel").then(setFunnel).catch(() => {});
    api.get("/combo/leads").then(setLeads).catch(() => {});
  };
  useEffect(() => { reload(); setLoaded(true); }, []);

  const openDetail = async (l) => {
    setDetail(l); setDraft(null);
    api.get(`/combo/leads/${l.id}/interactions`).then(setInteractions).catch(() => {});
  };

  const saveLead = async () => {
    if (!lead.name.trim()) { toast.error("Name is required"); return; }
    try { await api.post("/combo/leads", lead); toast.success("Lead captured");
      setLeadOpen(false); setLead({ name: "", phone: "", email: "", source: "call", service_needed: "", notes: "" }); reload(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const setStage = async (id, stage) => {
    try { const l = await api.patch(`/combo/leads/${id}`, { stage }); setDetail(l); reload(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const logIx = async () => {
    if (!ix.summary.trim()) { toast.error("Add a summary"); return; }
    try { await api.post(`/combo/leads/${detail.id}/interactions`, ix);
      setIx({ kind: "call", direction: "inbound", summary: "" });
      const l = await api.patch(`/combo/leads/${detail.id}`, {});
      setDetail(l);
      api.get(`/combo/leads/${detail.id}/interactions`).then(setInteractions).catch(() => {});
      reload(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const getDraft = async () => {
    try { setDraft({ loading: true }); setDraft(await api.post(`/combo/leads/${detail.id}/draft-followup`)); }
    catch (e) { toast.error(errMsg(e)); setDraft(null); }
  };

  const convert = async () => {
    const amt = parseFloat(conv.amount || 0);
    if (!amt) { toast.error("Enter the quoted amount"); return; }
    try {
      const r = await api.post(`/combo/leads/${detail.id}/convert`, { amount: amt, description: conv.description });
      toast.success(`Converted — estimate ${r.estimate.number}`);
      setDetail({ ...detail, stage: "quoted", estimate_id: r.estimate.id });
      setConv({ amount: "", description: "" }); reload();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const del = async (id) => {
    if (!confirm("Delete this lead?")) return;
    try { await api.del(`/combo/leads/${id}`); setDetail(null); reload(); } catch (e) { toast.error(errMsg(e)); }
  };

  if (!loaded) return <Loading />;
  return (
    <div className="space-y-6">
      <PageHeader title="Empire Combo" subtitle="Receptionist books the job · Books do the books · Lead-gen on top."
        actions={canManage && <Button onClick={() => setLeadOpen(true)} data-testid="lead-add"><Plus size={14} className="mr-1" />Capture lead</Button>} />
      {funnel && (
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
          {funnel.stages.map((s) => (
            <Card key={s} className="p-3"><div className={`text-xs capitalize font-medium ${STAGE_CLS[s]?.split(" ")[1] || ""}`}>{s}</div>
              <div className="text-2xl font-semibold">{funnel.by_stage[s] || 0}</div></Card>
          ))}
        </div>
      )}
      {funnel && (
        <div className="grid grid-cols-3 gap-4">
          <Card className="p-4"><div className="text-xs text-gray-500">Conversion rate</div><div className="text-2xl font-semibold text-emerald-400">{funnel.conversion_pct}%</div></Card>
          <Card className="p-4"><div className="text-xs text-gray-500">Revenue won</div><div className="text-2xl font-semibold">{money(funnel.won_revenue_cents)}</div></Card>
          <Card className="p-4"><div className="text-xs text-gray-500">Top source</div><div className="text-2xl font-semibold capitalize">
            {Object.entries(funnel.by_source).sort((a, b) => b[1] - a[1])[0]?.[0] || "—"}</div></Card>
        </div>
      )}
      <div className="grid lg:grid-cols-[1fr_420px] gap-6">
        <Card className="p-0 overflow-hidden h-fit">
          <table className="w-full text-sm">
            <thead><tr className="text-left text-gray-500 border-b border-[#222838]">
              <th className="px-4 py-3 font-medium">Lead</th><th className="px-4 py-3 font-medium">Source</th>
              <th className="px-4 py-3 font-medium">Stage</th><th className="px-4 py-3"></th>
            </tr></thead>
            <tbody>
              {leads.map((l) => (
                <tr key={l.id} className={`border-b border-[#1a2030] hover:bg-[#141926] cursor-pointer ${detail?.id === l.id ? "bg-[#141926]" : ""}`} onClick={() => openDetail(l)}>
                  <td className="px-4 py-3"><div className="font-medium">{l.name}</div><div className="text-xs text-gray-500">{l.service_needed}</div></td>
                  <td className="px-4 py-3 text-xs text-gray-400 capitalize">{l.source}</td>
                  <td className="px-4 py-3"><span className={`text-xs px-2 py-0.5 rounded-full capitalize ${STAGE_CLS[l.stage]}`}>{l.stage}</span></td>
                  <td className="px-4 py-3 text-right"><ArrowRight size={14} className="inline text-gray-600" /></td>
                </tr>
              ))}
              {!leads.length && <tr><td colSpan={4} className="px-4 py-8 text-center text-gray-500 text-sm">No leads yet — capture the first one above.</td></tr>}
            </tbody>
          </table>
        </Card>
        <div>
          {!detail && <Card className="p-10 text-center text-gray-500 text-sm">Select a lead to see the receptionist log.</Card>}
          {detail && (
            <Card className="p-5 space-y-4">
              <div className="flex items-start justify-between">
                <div><div className="font-medium text-lg">{detail.name}</div>
                  <div className="text-xs text-gray-500">{detail.phone} · {detail.email} · via {detail.source}</div>
                  <div className="text-xs text-gray-400 mt-1">{detail.service_needed}</div></div>
                {canManage && <Button size="sm" variant="ghost" onClick={() => del(detail.id)}><Trash2 size={13} /></Button>}
              </div>
              {canManage && (
                <div className="flex gap-1 flex-wrap">
                  {STAGES.map((s) => (
                    <Button key={s} size="sm" variant={detail.stage === s ? "default" : "outline"}
                      className="capitalize h-7 text-xs" onClick={() => setStage(detail.id, s)}>{s}</Button>
                  ))}
                </div>
              )}
              <div>
                <div className="text-sm font-medium mb-2 flex items-center gap-1"><Phone size={13} /> Receptionist log</div>
                <div className="space-y-1.5 max-h-44 overflow-auto">
                  {interactions.map((i) => (
                    <div key={i.id} className="text-xs bg-[#0F1218] rounded p-2">
                      <span className="text-gray-500 capitalize">{i.kind} · {i.direction}</span>
                      <p className="text-gray-300 mt-0.5">{i.summary}</p>
                    </div>
                  ))}
                  {!interactions.length && <p className="text-xs text-gray-600">No touches logged yet.</p>}
                </div>
                {canManage && (
                  <div className="flex gap-1.5 mt-2">
                    <Select value={ix.kind} onValueChange={(v) => setIx({ ...ix, kind: v })}>
                      <SelectTrigger className="w-24 bg-[#0F1218] border-[#222838] h-8 text-xs"><SelectValue /></SelectTrigger>
                      <SelectContent><SelectItem value="call">Call</SelectItem><SelectItem value="text">Text</SelectItem><SelectItem value="note">Note</SelectItem></SelectContent>
                    </Select>
                    <Input placeholder="What happened…" value={ix.summary} onChange={(e) => setIx({ ...ix, summary: e.target.value })}
                      className="bg-[#0F1218] border-[#222838] h-8 text-xs" />
                    <Button size="sm" className="h-8" onClick={logIx}>Log</Button>
                  </div>
                )}
              </div>
              {canManage && (
                <div>
                  <Button size="sm" variant="outline" onClick={getDraft} className="mb-2">
                    <Sparkles size={13} className="mr-1" /> Draft follow-up</Button>
                  {draft?.loading && <p className="text-xs text-gray-500">Writing…</p>}
                  {draft?.draft && <p className="text-xs bg-[#0F1218] border border-[#222838] rounded p-2.5 text-gray-300">{draft.draft}
                    {!draft.ai && <span className="text-gray-600"> (template — add EMERGENT_LLM_KEY for AI drafts)</span>}</p>}
                </div>
              )}
              {canManage && detail.stage !== "quoted" && detail.stage !== "won" && (
                <div className="border-t border-[#222838] pt-3">
                  <div className="text-sm font-medium mb-2">Convert to estimate</div>
                  <div className="flex gap-1.5">
                    <Input type="number" placeholder="Amount $" value={conv.amount} onChange={(e) => setConv({ ...conv, amount: e.target.value })}
                      className="bg-[#0F1218] border-[#222838] h-8 text-xs font-mono" />
                    <Input placeholder="Description" value={conv.description} onChange={(e) => setConv({ ...conv, description: e.target.value })}
                      className="bg-[#0F1218] border-[#222838] h-8 text-xs" />
                    <Button size="sm" className="h-8" onClick={convert} data-testid="lead-convert">Convert</Button>
                  </div>
                </div>
              )}
              {detail.estimate_id && <p className="text-xs text-emerald-400 flex items-center gap-1"><MessageSquare size={12} /> Estimate created — see Estimates.</p>}
            </Card>
          )}
        </div>
      </div>

      <Dialog open={leadOpen} onOpenChange={setLeadOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader><DialogTitle>Capture lead</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2"><Label className="text-xs">Name *</Label><Input value={lead.name} onChange={(e) => setLead({ ...lead, name: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">Phone</Label><Input value={lead.phone} onChange={(e) => setLead({ ...lead, phone: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">Email</Label><Input value={lead.email} onChange={(e) => setLead({ ...lead, email: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">Source</Label>
              <Select value={lead.source} onValueChange={(v) => setLead({ ...lead, source: v })}>
                <SelectTrigger className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                <SelectContent>{["call", "text", "web", "referral", "walk-in", "other"].map((s) => <SelectItem key={s} value={s} className="capitalize">{s}</SelectItem>)}</SelectContent>
              </Select></div>
            <div><Label className="text-xs">Service needed</Label><Input value={lead.service_needed} onChange={(e) => setLead({ ...lead, service_needed: e.target.value })} placeholder="AC not cooling" className="bg-[#0F1218] border-[#222838]" /></div>
            <div className="col-span-2"><Label className="text-xs">Notes</Label><Input value={lead.notes} onChange={(e) => setLead({ ...lead, notes: e.target.value })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <DialogFooter><Button variant="ghost" onClick={() => setLeadOpen(false)}>Cancel</Button><Button onClick={saveLead}>Capture</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
