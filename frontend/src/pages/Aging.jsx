import { useEffect, useState } from "react";
import { api, errMsg, downloadCsv } from "@/lib/api";
import { dollars, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { toast } from "sonner";
import { Download, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";
const BUCKETS = ["0-30", "31-60", "61-90", "90+"];
const BUCKET_CLS = {
  "0-30": "text-emerald-400",
  "31-60": "text-amber-400",
  "61-90": "text-orange-400",
  "90+": "text-rose-400"
};
function BucketStrip({
  buckets
}) {
  const {
    t
  } = useTranslation();
  return <div className="grid grid-cols-4 gap-2 mb-4">
      {BUCKETS.map(b => <div key={b} className="rounded-lg bg-[#0F1218] border border-[#222838] px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-gray-500">{b} {t("aging.days")}</div>
          <div className={`font-mono text-sm mt-1 ${BUCKET_CLS[b]}`}>{dollars(buckets[b])}</div>
        </div>)}
    </div>;
}
export default function Aging() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const isOwner = user?.role === "owner";
  const [ar, setAr] = useState(null);
  const [ap, setAp] = useState(null);
  const [bills, setBills] = useState([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    vendor: "",
    amount: "",
    due_date: ""
  });
  const load = () => {
    api.get("/aging/ar").then(setAr);
    api.get("/aging/ap").then(setAp);
    api.get("/bills").then(setBills).catch(() => setBills([]));
  };
  useEffect(() => {
    load();
  }, []);
  const addBill = async () => {
    try {
      await api.post("/bills", {
        vendor: form.vendor,
        amount: parseFloat(form.amount) || 0,
        due_date: form.due_date || undefined
      });
      toast.success(t("aging.billRecordedToAP"));
      setOpen(false);
      setForm({
        vendor: "",
        amount: "",
        due_date: ""
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const payBill = async id => {
    try {
      await api.post(`/bills/${id}/pay`, {
        payment_method: "bank"
      });
      toast.success(t("aging.billPaid"));
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!ar || !ap) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("aging.arAPAging")} subtitle="Outstanding receivables & payables by age" testid="aging-page" />
      <div className="grid lg:grid-cols-2 gap-6">
        {/* AR */}
        <Card data-testid="ar-aging">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-display font-semibold">{t("aging.accountsReceivable")}</h3>
            <Button size="sm" variant="outline" className="h-8 border-[#2a3140]" data-testid="ar-csv" onClick={() => downloadCsv("/aging/ar.csv", "ar_aging.csv")}><Download size={14} className="me-1" />{t("aging.csv")}</Button>
          </div>
          <BucketStrip buckets={ar.buckets} />
          <table className="w-full text-sm">
            <thead><tr className="text-xs uppercase text-gray-500 border-b border-[#222838]">
              <th className="py-2 text-start">{t("aging.client")}</th><th className="py-2 text-start">{t("aging.due")}</th><th className="py-2 text-end">{t("aging.bucket")}</th><th className="py-2 text-end">{t("aging.amount")}</th></tr></thead>
            <tbody>
              {ar.items.map((it, i) => <tr key={i} className="border-b border-[#171c26]">
                  <td className="py-2 text-gray-200">{it.name}</td>
                  <td className="py-2 text-gray-400">{fmtDate(it.due_date)}</td>
                  <td className={`py-2 text-end ${BUCKET_CLS[it.bucket]}`}>{it.bucket}</td>
                  <td className="py-2 text-end font-mono text-gray-200">{dollars(it.amount)}</td>
                </tr>)}
              {ar.items.length === 0 && <tr><td colSpan={4} className="py-6 text-center text-gray-600">{t("aging.noOpenReceivables")}</td></tr>}
            </tbody>
          </table>
          <div className="flex justify-between pt-3 mt-2 border-t border-[#222838] font-mono font-semibold">
            <span className="text-gray-400">{t("aging.totalAR")}</span><span className="text-amber-400" data-testid="ar-total">{dollars(ar.total)}</span>
          </div>
        </Card>

        {/* AP */}
        <Card data-testid="ap-aging">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-display font-semibold">{t("aging.accountsPayable")}</h3>
            <div className="flex gap-2">
              {isOwner && <Button size="sm" className="h-8 bg-amber-600 hover:bg-amber-500 text-black" data-testid="btn-add-bill" onClick={() => setOpen(true)}><Plus size={14} className="me-1" />{t("aging.bill")}</Button>}
              <Button size="sm" variant="outline" className="h-8 border-[#2a3140]" data-testid="ap-csv" onClick={() => downloadCsv("/aging/ap.csv", "ap_aging.csv")}><Download size={14} className="me-1" />{t("aging.csv")}</Button>
            </div>
          </div>
          <BucketStrip buckets={ap.buckets} />
          <table className="w-full text-sm">
            <thead><tr className="text-xs uppercase text-gray-500 border-b border-[#222838]">
              <th className="py-2 text-start">{t("aging.vendor")}</th><th className="py-2 text-start">{t("aging.due")}</th><th className="py-2 text-end">{t("aging.bucket")}</th><th className="py-2 text-end">{t("aging.amount")}</th>{isOwner && <th className="py-2"></th>}</tr></thead>
            <tbody>
              {bills.filter(b => b.status === "open").map(b => {
              const it = ap.items.find(x => x.name === b.vendor && x.amount_cents === b.amount_cents) || {
                bucket: "0-30"
              };
              return <tr key={b.id} className="border-b border-[#171c26]">
                    <td className="py-2 text-gray-200">{b.vendor}</td>
                    <td className="py-2 text-gray-400">{fmtDate(b.due_date)}</td>
                    <td className={`py-2 text-end ${BUCKET_CLS[it.bucket]}`}>{it.bucket}</td>
                    <td className="py-2 text-end font-mono text-gray-200">{dollars(b.amount_cents / 100)}</td>
                    {isOwner && <td className="py-2 text-end"><Button size="sm" className="h-7 bg-emerald-600 hover:bg-emerald-500 text-black" onClick={() => payBill(b.id)} data-testid={`pay-bill-${b.id}`}>{t("aging.pay")}</Button></td>}
                  </tr>;
            })}
              {bills.filter(b => b.status === "open").length === 0 && <tr><td colSpan={isOwner ? 5 : 4} className="py-6 text-center text-gray-600">{t("aging.noOpenPayables")}</td></tr>}
            </tbody>
          </table>
          <div className="flex justify-between pt-3 mt-2 border-t border-[#222838] font-mono font-semibold">
            <span className="text-gray-400">{t("aging.totalAP")}</span><span className="text-amber-400" data-testid="ap-total">{dollars(ap.total)}</span>
          </div>
        </Card>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("aging.recordAVendorBill")}</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div><Label className="text-xs">{t("aging.vendor")}</Label><Input data-testid="bill-vendor" value={form.vendor} onChange={e => setForm({
              ...form,
              vendor: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div className="grid grid-cols-2 gap-3">
              <div><Label className="text-xs">{t("aging.amount2")}</Label><Input data-testid="bill-amount" value={form.amount} onChange={e => setForm({
                ...form,
                amount: e.target.value
              })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
              <div><Label className="text-xs">{t("aging.dueDate")}</Label><Input type="date" data-testid="bill-due" value={form.due_date} onChange={e => setForm({
                ...form,
                due_date: e.target.value
              })} className="bg-[#0F1218] border-[#222838]" /></div>
            </div>
          </div>
          <DialogFooter><Button data-testid="bill-save" onClick={addBill} className="bg-amber-600 hover:bg-amber-500 text-black">{t("aging.recordBill")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
