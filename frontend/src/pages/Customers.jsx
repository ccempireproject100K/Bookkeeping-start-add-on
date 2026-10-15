import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { toast } from "sonner";
import { Plus, MapPin, Phone, Mail, FileText } from "lucide-react";
import { money, dollars, fmtDate } from "@/lib/format";
import { useTranslation } from "react-i18next";
export default function Customers() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const isOwner = user?.role === "owner";
  const [rows, setRows] = useState(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    name: "",
    email: "",
    phone: "",
    address: ""
  });
  const [stmt, setStmt] = useState(null);
  const load = () => api.get("/customers").then(setRows).catch(() => setRows([]));
  useEffect(() => {
    load();
  }, []);
  const openStatement = async id => {
    try {
      setStmt(await api.get(`/customers/${id}/statement`));
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const create = async () => {
    try {
      await api.post("/customers", {
        name: form.name,
        email: form.email || null,
        phone: form.phone || null,
        locations: form.address ? [{
          label: "Primary",
          address: form.address
        }] : []
      });
      toast.success(t("customers.customerAdded"));
      setOpen(false);
      setForm({
        name: "",
        email: "",
        phone: "",
        address: ""
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!rows) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("customers.customers")} subtitle={`${rows.length} customers & service locations`} testid="customers-page">
        {isOwner && <Button data-testid="btn-add-customer" onClick={() => setOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" />{t("customers.addCustomer")}</Button>}
      </PageHeader>

      <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {rows.map(c => <Card key={c.id} data-testid={`customer-${c.id}`} className="hover:border-[#34404f] transition-colors">
            <div className="font-display font-semibold text-lg">{c.name}</div>
            <div className="mt-3 space-y-1.5 text-sm text-gray-400">
              {c.locations?.map((l, i) => <div key={i} className="flex items-start gap-2"><MapPin size={14} className="mt-0.5 text-amber-400" /><span>{l.address}</span></div>)}
              {c.phone && <div className="flex items-center gap-2"><Phone size={14} className="text-gray-500" />{c.phone}</div>}
              {c.email && <div className="flex items-center gap-2"><Mail size={14} className="text-gray-500" />{c.email}</div>}
            </div>
            <Button size="sm" variant="outline" data-testid={`statement-${c.id}`} onClick={() => openStatement(c.id)} className="mt-4 border-[#222838] h-8"><FileText size={13} className="mr-1" />{t("customers.statement")}</Button>
          </Card>)}
        {rows.length === 0 && <Card><div className="text-gray-500 text-sm text-center py-6">{t("customers.noCustomersYet")}</div></Card>}
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("customers.addCustomer")}</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div><Label className="text-xs">{t("customers.name")}</Label><Input data-testid="customer-name" value={form.name} onChange={e => setForm({
              ...form,
              name: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("customers.serviceAddress")}</Label><Input data-testid="customer-address" value={form.address} onChange={e => setForm({
              ...form,
              address: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div className="grid grid-cols-2 gap-3">
              <div><Label className="text-xs">{t("customers.phone")}</Label><Input data-testid="customer-phone" value={form.phone} onChange={e => setForm({
                ...form,
                phone: e.target.value
              })} className="bg-[#0F1218] border-[#222838]" /></div>
              <div><Label className="text-xs">{t("customers.email")}</Label><Input data-testid="customer-email" value={form.email} onChange={e => setForm({
                ...form,
                email: e.target.value
              })} className="bg-[#0F1218] border-[#222838]" /></div>
            </div>
          </div>
          <DialogFooter><Button data-testid="customer-save" onClick={create} className="bg-amber-600 hover:bg-amber-500 text-black">{t("customers.add")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!stmt} onOpenChange={o => !o && setStmt(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-2xl">
          <DialogHeader><DialogTitle>{t("customers.statement2")} {stmt?.customer?.name}</DialogTitle></DialogHeader>
          <div className="text-sm text-gray-400 mb-2">{t("customers.outstandingBalance")} <span className="font-mono text-amber-400" data-testid="stmt-outstanding">{dollars(stmt?.outstanding || 0)}</span></div>
          <div className="max-h-96 overflow-y-auto">
            <table className="w-full text-sm" data-testid="table-statement">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-3 py-2 text-left font-medium">{t("customers.date")}</th><th className="px-3 py-2 text-left font-medium">{t("customers.type")}</th>
                <th className="px-3 py-2 text-right font-medium">{t("customers.charge")}</th><th className="px-3 py-2 text-right font-medium">{t("customers.payment")}</th>
                <th className="px-3 py-2 text-right font-medium">{t("customers.balance")}</th></tr></thead>
              <tbody>
                {stmt?.events?.map((e, i) => <tr key={i} className="border-b border-[#171c26]">
                    <td className="px-3 py-2 text-gray-400">{fmtDate(e.date)}</td><td className="px-3 py-2 text-gray-200">{e.type}</td>
                    <td className="px-3 py-2 text-right font-mono text-gray-300">{e.charge_cents ? dollars(e.charge) : "—"}</td>
                    <td className="px-3 py-2 text-right font-mono text-emerald-400">{e.payment_cents ? dollars(e.payment) : "—"}</td>
                    <td className="px-3 py-2 text-right font-mono text-gray-100">{dollars(e.balance)}</td>
                  </tr>)}
                {stmt?.events?.length === 0 && <tr><td colSpan={5} className="px-3 py-8 text-center text-gray-500">{t("customers.noActivity")}</td></tr>}
              </tbody>
            </table>
          </div>
        </DialogContent>
      </Dialog>
    </div>;
}
