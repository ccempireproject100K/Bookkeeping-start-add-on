import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { toast } from "sonner";
import { Plus, Trash2, FilePlus, ArrowRightLeft, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
const STATUS_CLS = {
  draft: "bg-gray-500/10 text-gray-400 border-gray-500/20",
  sent: "bg-sky-500/10 text-sky-400 border-sky-500/20",
  accepted: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  converted: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  declined: "bg-rose-500/10 text-rose-400 border-rose-500/20"
};
export default function Estimates() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const canManage = user?.role === "owner";
  const [estimates, setEstimates] = useState([]);
  const [recurring, setRecurring] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [open, setOpen] = useState(false);
  const [recOpen, setRecOpen] = useState(false);
  const [client, setClient] = useState("");
  const [tax, setTax] = useState("");
  const [lines, setLines] = useState([{
    name: "",
    revenue_code: "4300",
    amount: ""
  }]);
  const [recForm, setRecForm] = useState({
    client_name: "",
    frequency: "monthly",
    lines: [{
      name: "",
      revenue_code: "4300",
      amount: ""
    }]
  });
  const load = () => Promise.all([api.get("/estimates").then(setEstimates).catch(() => {}), api.get("/estimates/recurring/list").then(setRecurring).catch(() => {})]).then(() => setLoaded(true));
  useEffect(() => {
    load();
  }, []);
  const create = async () => {
    try {
      await api.post("/estimates", {
        client_name: client,
        tax: parseFloat(tax) || 0,
        lines: lines.filter(l => l.name && l.amount).map(l => ({
          kind: "service",
          name: l.name,
          revenue_code: l.revenue_code,
          amount: parseFloat(l.amount) || 0
        }))
      });
      toast.success(t("estimates.estimateCreated"));
      setOpen(false);
      setClient("");
      setTax("");
      setLines([{
        name: "",
        revenue_code: "4300",
        amount: ""
      }]);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const setStatus = async (id, status) => {
    try {
      await api.patch(`/estimates/${id}`, {
        status
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const convert = async id => {
    try {
      await api.post(`/estimates/${id}/convert`);
      toast.success(t("estimates.convertedToInvoice"));
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const createRecurring = async () => {
    try {
      await api.post("/estimates/recurring", {
        client_name: recForm.client_name,
        frequency: recForm.frequency,
        tax: 0,
        lines: recForm.lines.filter(l => l.name && l.amount).map(l => ({
          kind: "service",
          name: l.name,
          revenue_code: l.revenue_code,
          amount: parseFloat(l.amount) || 0
        }))
      });
      toast.success(t("estimates.recurringTemplateCreated"));
      setRecOpen(false);
      setRecForm({
        client_name: "",
        frequency: "monthly",
        lines: [{
          name: "",
          revenue_code: "4300",
          amount: ""
        }]
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const generate = async id => {
    try {
      await api.post(`/estimates/recurring/${id}/generate`);
      toast.success(t("estimates.invoiceGenerated"));
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!loaded) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("estimates.estimatesRecurring")} subtitle="Quotes, estimate→invoice conversion & recurring invoices" testid="estimates-page" />
      <Tabs defaultValue="estimates">
        <TabsList className="bg-[#14171E] border border-[#222838]">
          <TabsTrigger value="estimates" data-testid="tab-estimates">{t("estimates.estimates")}</TabsTrigger>
          <TabsTrigger value="recurring" data-testid="tab-recurring">{t("estimates.recurringInvoices")}</TabsTrigger>
        </TabsList>

        <TabsContent value="estimates" className="mt-4">
          {canManage && <div className="mb-3 flex justify-end"><Button data-testid="btn-new-estimate" onClick={() => setOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" />{t("estimates.newEstimate")}</Button></div>}
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-estimates">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-4 py-3 text-left font-medium">{t("estimates.number")}</th><th className="px-4 py-3 text-left font-medium">{t("estimates.client")}</th>
                <th className="px-4 py-3 text-right font-medium">{t("estimates.total")}</th><th className="px-4 py-3 text-left font-medium">{t("estimates.status")}</th>
                {canManage && <th className="px-4 py-3 text-right font-medium">{t("estimates.actions")}</th>}</tr></thead>
              <tbody>
                {estimates.map(e => <tr key={e.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-4 py-3 font-mono text-amber-400">{e.number}</td>
                    <td className="px-4 py-3 text-gray-100">{e.client_name}</td>
                    <td className="px-4 py-3 text-right font-mono text-gray-200">{money(e.total_cents)}</td>
                    <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[e.status] || ""}`}>{e.status}</span></td>
                    {canManage && <td className="px-4 py-3 text-right space-x-2">
                      {e.status === "draft" && <Button size="sm" variant="outline" className="h-7 border-[#222838]" data-testid={`send-${e.id}`} onClick={() => setStatus(e.id, "sent")}>{t("estimates.markSent")}</Button>}
                      {(e.status === "draft" || e.status === "sent") && <Button size="sm" variant="outline" className="h-7 border-[#222838]" onClick={() => setStatus(e.id, "accepted")}>{t("estimates.accept")}</Button>}
                      {e.status !== "converted" && e.status !== "declined" && <Button size="sm" className="h-7 bg-emerald-600 hover:bg-emerald-500 text-black" data-testid={`convert-${e.id}`} onClick={() => convert(e.id)}><ArrowRightLeft size={13} className="mr-1" />{t("estimates.convert")}</Button>}
                    </td>}
                  </tr>)}
                {estimates.length === 0 && <tr><td colSpan={canManage ? 5 : 4} className="px-5 py-12 text-center text-gray-500">{t("estimates.noEstimatesYet")}</td></tr>}
              </tbody>
            </table>
          </Card>
        </TabsContent>

        <TabsContent value="recurring" className="mt-4">
          {canManage && <div className="mb-3 flex justify-end"><Button data-testid="btn-new-recurring" onClick={() => setRecOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" />{t("estimates.newTemplate")}</Button></div>}
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-recurring">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-4 py-3 text-left font-medium">{t("estimates.client")}</th><th className="px-4 py-3 text-left font-medium">{t("estimates.frequency")}</th>
                <th className="px-4 py-3 text-left font-medium">{t("estimates.lastGenerated")}</th><th className="px-4 py-3 text-right font-medium">{t("estimates.count")}</th>
                {canManage && <th className="px-4 py-3 text-right font-medium">{t("estimates.action")}</th>}</tr></thead>
              <tbody>
                {recurring.map(r => <tr key={r.id} className="border-b border-[#171c26]">
                    <td className="px-4 py-3 text-gray-100">{r.client_name}</td><td className="px-4 py-3 text-gray-400 capitalize">{r.frequency}</td>
                    <td className="px-4 py-3 text-gray-400">{r.last_generated ? fmtDate(r.last_generated) : "—"}</td>
                    <td className="px-4 py-3 text-right text-gray-300">{r.generated_count}</td>
                    {canManage && <td className="px-4 py-3 text-right"><Button size="sm" className="h-7 bg-amber-600 hover:bg-amber-500 text-black" data-testid={`generate-${r.id}`} onClick={() => generate(r.id)}><RefreshCw size={13} className="mr-1" />{t("estimates.generate")}</Button></td>}
                  </tr>)}
                {recurring.length === 0 && <tr><td colSpan={canManage ? 5 : 4} className="px-5 py-12 text-center text-gray-500">{t("estimates.noRecurringTemplates")}</td></tr>}
              </tbody>
            </table>
          </Card>
        </TabsContent>
      </Tabs>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg">
          <DialogHeader><DialogTitle>{t("estimates.newEstimate")}</DialogTitle></DialogHeader>
          <div><Label className="text-xs">{t("estimates.client")}</Label><Input data-testid="est-client" value={client} onChange={e => setClient(e.target.value)} className="bg-[#0F1218] border-[#222838]" /></div>
          <div className="space-y-2"><Label className="text-xs">{t("estimates.lineItems")}</Label>
            {lines.map((l, i) => <div key={i} className="flex gap-2 items-center">
                <Input placeholder={t("estimates.description")} value={l.name} onChange={e => {
              const c = [...lines];
              c[i].name = e.target.value;
              setLines(c);
            }} className="bg-[#0F1218] border-[#222838] flex-1" data-testid={`est-line-name-${i}`} />
                <Input placeholder="0.00" value={l.amount} onChange={e => {
              const c = [...lines];
              c[i].amount = e.target.value;
              setLines(c);
            }} className="bg-[#0F1218] border-[#222838] w-28 font-mono text-right" data-testid={`est-line-amount-${i}`} />
                {lines.length > 1 && <button onClick={() => setLines(lines.filter((_, x) => x !== i))} className="text-gray-600 hover:text-rose-400"><Trash2 size={14} /></button>}
              </div>)}
            <Button variant="ghost" size="sm" onClick={() => setLines([...lines, {
            name: "",
            revenue_code: "4300",
            amount: ""
          }])} className="text-amber-400 hover:bg-amber-500/10"><FilePlus size={14} className="mr-1" />{t("estimates.addLine")}</Button>
          </div>
          <div><Label className="text-xs">{t("estimates.tax")}</Label><Input data-testid="est-tax" value={tax} onChange={e => setTax(e.target.value)} className="bg-[#0F1218] border-[#222838] w-32 font-mono" /></div>
          <DialogFooter><Button data-testid="est-save" onClick={create} className="bg-amber-600 hover:bg-amber-500 text-black">{t("estimates.createEstimate")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={recOpen} onOpenChange={setRecOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg">
          <DialogHeader><DialogTitle>{t("estimates.newRecurringInvoice")}</DialogTitle></DialogHeader>
          <div><Label className="text-xs">{t("estimates.client")}</Label><Input data-testid="rec-client" value={recForm.client_name} onChange={e => setRecForm({
            ...recForm,
            client_name: e.target.value
          })} className="bg-[#0F1218] border-[#222838]" /></div>
          <div className="space-y-2"><Label className="text-xs">{t("estimates.lineItems")}</Label>
            {recForm.lines.map((l, i) => <div key={i} className="flex gap-2 items-center">
                <Input placeholder={t("estimates.description")} value={l.name} onChange={e => {
              const c = [...recForm.lines];
              c[i].name = e.target.value;
              setRecForm({
                ...recForm,
                lines: c
              });
            }} className="bg-[#0F1218] border-[#222838] flex-1" data-testid={`recinv-name-${i}`} />
                <Input placeholder="0.00" value={l.amount} onChange={e => {
              const c = [...recForm.lines];
              c[i].amount = e.target.value;
              setRecForm({
                ...recForm,
                lines: c
              });
            }} className="bg-[#0F1218] border-[#222838] w-28 font-mono text-right" data-testid={`recinv-amount-${i}`} />
              </div>)}
            <Button variant="ghost" size="sm" onClick={() => setRecForm({
            ...recForm,
            lines: [...recForm.lines, {
              name: "",
              revenue_code: "4300",
              amount: ""
            }]
          })} className="text-amber-400 hover:bg-amber-500/10"><FilePlus size={14} className="mr-1" />{t("estimates.addLine")}</Button>
          </div>
          <DialogFooter><Button data-testid="rec-save" onClick={createRecurring} className="bg-amber-600 hover:bg-amber-500 text-black">{t("estimates.createTemplate")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
