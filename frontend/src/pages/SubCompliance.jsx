import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Checkbox } from "@/components/ui/checkbox";
import { toast } from "sonner";
import { ShieldCheck, ShieldAlert, ShieldX, Pencil } from "lucide-react";
import { useTranslation } from "react-i18next";
const ISSUE_LABEL = {
  missing_w9: "W-9 missing",
  insurance_expired: "Insurance expired",
  insurance_expiring: "Insurance expiring ≤30d",
  license_expired: "License expired"
};
export default function SubCompliance() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const canManage = user?.role === "owner";
  const [loaded, setLoaded] = useState(false);
  const [subs, setSubs] = useState([]);
  const [dash, setDash] = useState(null);
  const [edit, setEdit] = useState(null);
  const [form, setForm] = useState({
    w9_received: false,
    w9_date: "",
    insurance_expiry: "",
    license_expiry: "",
    notes: ""
  });
  const reload = () => {
    api.get("/subcompliance/subs").then(setSubs).catch(() => {});
    api.get("/subcompliance/dashboard").then(setDash).catch(() => {});
  };
  useEffect(() => {
    reload();
    setLoaded(true);
  }, []);
  const openEdit = s => {
    const c = s.compliance || {};
    setForm({
      w9_received: !!c.w9_received,
      w9_date: c.w9_date || "",
      insurance_expiry: c.insurance_expiry || "",
      license_expiry: c.license_expiry || "",
      notes: c.notes || ""
    });
    setEdit(s);
  };
  const save = async () => {
    try {
      await api.put(`/subcompliance/subs/${edit.id}`, form);
      toast.success(t("subcompliance.complianceUpdated"));
      setEdit(null);
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!loaded) return <Loading />;
  return <div className="space-y-6">
      <PageHeader title={t("subcompliance.subcontractorCompliance")} subtitle="W-9s, insurance certs, and licenses — 1099 season with no surprises." />
      {dash && <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <Card className="p-4"><div className="text-xs text-gray-500">{t("subcompliance.Readiness")}</div>
            <div className={`text-2xl font-semibold ${dash.readiness_pct === 100 ? "text-emerald-400" : "text-amber-400"}`}>{dash.readiness_pct}%</div></Card>
          <Card className="p-4"><div className="text-xs text-gray-500">{t("subcompliance.subcontractors")}</div><div className="text-2xl font-semibold">{dash.total_subs}</div></Card>
          <Card className="p-4"><div className="text-xs text-gray-500">{t("subcompliance.w9sMissing")}</div><div className="text-2xl font-semibold text-red-400">{dash.counts.missing_w9}</div></Card>
          <Card className="p-4"><div className="text-xs text-gray-500">{t("subcompliance.insuranceIssues")}</div><div className="text-2xl font-semibold text-amber-400">{dash.counts.insurance_expired + dash.counts.insurance_expiring}</div></Card>
        </div>}
      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead><tr className="text-left text-gray-500 border-b border-[#222838]">
            <th className="px-4 py-3 font-medium">{t("subcompliance.subcontractor")}</th><th className="px-4 py-3 font-medium">{t("subcompliance.status")}</th>
            <th className="px-4 py-3 font-medium">{t("subcompliance.issues")}</th><th className="px-4 py-3"></th>
          </tr></thead>
          <tbody>
            {subs.map(s => {
            const st = s.compliance_status || {};
            return <tr key={s.id} className="border-b border-[#1a2030] hover:bg-[#141926]">
                  <td className="px-4 py-3">{s.name}</td>
                  <td className="px-4 py-3">
                    {st.compliant ? <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400"><ShieldCheck size={12} />{t("subcompliance.compliant")}</span> : <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-red-500/15 text-red-400"><ShieldX size={12} />{t("subcompliance.attention")}</span>}
                  </td>
                  <td className="px-4 py-3">{(st.issues || []).map(i => <span key={i} className="text-xs px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-400 mr-1">{ISSUE_LABEL[i] || i}</span>)}
                    {!(st.issues || []).length && <span className="text-gray-600 text-xs">—</span>}</td>
                  <td className="px-4 py-3 text-right">
                    {canManage && <Button size="sm" variant="ghost" onClick={() => openEdit(s)}><Pencil size={13} /></Button>}
                  </td>
                </tr>;
          })}
            {!subs.length && <tr><td colSpan={4} className="px-4 py-8 text-center text-gray-500 text-sm">{t("subcompliance.no1099SubcontractorsYetMarkVendors")}</td></tr>}
          </tbody>
        </table>
      </Card>

      <Dialog open={!!edit} onOpenChange={() => setEdit(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader><DialogTitle>{edit?.name} {t("subcompliance.compliance")}</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Checkbox checked={form.w9_received} onCheckedChange={v => setForm({
              ...form,
              w9_received: !!v
            })} />
              <Label className="text-sm">{t("subcompliance.w9OnFile")}</Label>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div><Label className="text-xs">{t("subcompliance.w9Date")}</Label><Input type="date" value={form.w9_date} onChange={e => setForm({
                ...form,
                w9_date: e.target.value
              })} className="bg-[#0F1218] border-[#222838]" /></div>
              <div><Label className="text-xs">{t("subcompliance.insuranceExpiry")}</Label><Input type="date" value={form.insurance_expiry} onChange={e => setForm({
                ...form,
                insurance_expiry: e.target.value
              })} className="bg-[#0F1218] border-[#222838]" /></div>
              <div><Label className="text-xs">{t("subcompliance.licenseExpiry")}</Label><Input type="date" value={form.license_expiry} onChange={e => setForm({
                ...form,
                license_expiry: e.target.value
              })} className="bg-[#0F1218] border-[#222838]" /></div>
            </div>
            <div><Label className="text-xs">{t("subcompliance.notes")}</Label><Input value={form.notes} onChange={e => setForm({
              ...form,
              notes: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <DialogFooter><Button variant="ghost" onClick={() => setEdit(null)}>{t("subcompliance.cancel")}</Button><Button onClick={save}>{t("subcompliance.save")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
