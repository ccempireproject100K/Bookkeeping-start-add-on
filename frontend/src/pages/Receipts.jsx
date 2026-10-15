import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Upload, ScanLine, CheckCircle2, Trash2, Link2, Camera } from "lucide-react";
import { useTranslation } from "react-i18next";
function Thumb({
  id
}) {
  const [url, setUrl] = useState(null);
  useEffect(() => {
    let live = true;
    api.get(`/receipts/${id}/image`, {
      responseType: "blob"
    }).then(b => {
      if (live) setUrl(URL.createObjectURL(b));
    }).catch(() => {});
    return () => {
      live = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [id]);
  if (!url) return <span className="text-xs text-gray-600">…</span>;
  return <img src={url} alt="" className="h-10 w-10 object-cover rounded" />;
}
const STATUS_STYLE = {
  uploaded: "bg-blue-500/15 text-blue-400",
  scanned: "bg-amber-500/15 text-amber-400",
  posted: "bg-emerald-500/15 text-emerald-400"
};
export default function Receipts() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const canManage = user?.role === "owner";
  const [receipts, setReceipts] = useState([]);
  const [coa, setCoa] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [scanning, setScanning] = useState(null);
  const [review, setReview] = useState(null); // receipt being reviewed
  const [matches, setMatches] = useState([]);
  const [form, setForm] = useState({
    vendor: "",
    category_code: "5700",
    amount: "",
    expense_date: "",
    description: "",
    payment_method: "bank",
    bank_transaction_id: ""
  });
  const fileRef = useRef(null);
  const reload = () => {
    api.get("/receipts").then(setReceipts).catch(() => {});
    api.get("/ledger/accounts").then(setCoa).catch(() => {});
  };
  useEffect(() => {
    reload();
    setLoaded(true);
  }, []);
  const upload = async files => {
    if (!files?.length) return;
    setUploading(true);
    try {
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        await api.post("/receipts/upload", fd);
      }
      toast.success(files.length === 1 ? "Receipt uploaded" : `${files.length} receipts uploaded`);
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
    setUploading(false);
  };
  const scan = async r => {
    setScanning(r.id);
    try {
      const ex = await api.post(`/receipts/${r.id}/scan`);
      toast.success(`Scanned: ${ex.vendor || "receipt"} — ${money(ex.amount * 100)}`);
      reload();
      openReview({
        ...r,
        extracted: ex,
        status: "scanned"
      });
    } catch (e) {
      toast.error(errMsg(e));
    }
    setScanning(null);
  };
  const openReview = async r => {
    const ex = r.extracted || {};
    setForm({
      vendor: ex.vendor || "",
      category_code: ex.category_code || "5700",
      amount: ex.amount ? String(ex.amount) : "",
      expense_date: ex.date || "",
      description: ex.description || "",
      payment_method: "bank",
      bank_transaction_id: ""
    });
    setReview(r);
    try {
      setMatches(await api.get(`/receipts/${r.id}/matches`));
    } catch {
      setMatches([]);
    }
  };
  const post = async () => {
    if (!form.amount || parseFloat(form.amount) <= 0) {
      toast.error(t("receipts.enterTheReceiptTotal"));
      return;
    }
    try {
      await api.post(`/receipts/${review.id}/post`, {
        ...form,
        amount: parseFloat(form.amount)
      });
      toast.success(t("receipts.postedAsExpense"));
      setReview(null);
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const del = async id => {
    if (!confirm("Delete this receipt?")) return;
    try {
      await api.del(`/receipts/${id}`);
      toast.success(t("receipts.deleted"));
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!loaded) return <Loading />;
  return <div className="space-y-6">
      <PageHeader title={t("receipts.receiptInbox")} subtitle="Snap receipts — AI reads them in your trade's language, matches the bank feed, and posts the expense." />
      {canManage && <Card className="p-6">
          <div className="border-2 border-dashed border-[#222838] rounded-xl p-8 text-center">
            <Camera size={28} className="mx-auto text-gray-500 mb-2" />
            <p className="text-sm text-gray-400 mb-3">{t("receipts.dropReceiptPhotosHereOrTap")}</p>
            <input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp,application/pdf" multiple className="hidden" onChange={e => {
          upload(e.target.files);
          e.target.value = "";
        }} />
            <Button onClick={() => fileRef.current?.click()} disabled={uploading} data-testid="receipt-upload">
              <Upload size={14} className="mr-1" />{uploading ? "Uploading…" : "Upload receipts"}
            </Button>
          </div>
        </Card>}
      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead><tr className="text-left text-gray-500 border-b border-[#222838]">
            <th className="px-4 py-3 font-medium">{t("receipts.receipt")}</th><th className="px-4 py-3 font-medium">{t("receipts.vendor")}</th>
            <th className="px-4 py-3 font-medium">{t("receipts.date")}</th><th className="px-4 py-3 font-medium text-right">{t("receipts.amount")}</th>
            <th className="px-4 py-3 font-medium">{t("receipts.status")}</th><th className="px-4 py-3"></th>
          </tr></thead>
          <tbody>
            {receipts.map(r => <tr key={r.id} className="border-b border-[#1a2030] hover:bg-[#141926]">
                <td className="px-4 py-2">
                  {r.content_type?.startsWith("image/") ? <Thumb id={r.id} /> : <span className="text-xs text-gray-500">{t("receipts.pdf")}</span>}
                </td>
                <td className="px-4 py-3">{r.extracted?.vendor || <span className="text-gray-600 text-xs">{r.filename}</span>}</td>
                <td className="px-4 py-3 text-gray-400 text-xs">{r.extracted?.date ? fmtDate(r.extracted.date) : fmtDate(r.created_at)}</td>
                <td className="px-4 py-3 text-right font-mono">{r.extracted?.amount ? money(r.extracted.amount * 100) : "—"}</td>
                <td className="px-4 py-3">
                  <span className={`text-xs px-2 py-0.5 rounded-full ${STATUS_STYLE[r.status] || ""}`}>{r.status}</span>
                </td>
                <td className="px-4 py-3 text-right whitespace-nowrap">
                  {canManage && r.status === "uploaded" && <Button size="sm" variant="outline" className="mr-1" disabled={scanning === r.id} onClick={() => scan(r)} data-testid={`scan-${r.id}`}>
                      <ScanLine size={13} className="mr-1" />{scanning === r.id ? "Reading…" : "AI scan"}
                    </Button>}
                  {canManage && r.status === "scanned" && <Button size="sm" className="mr-1" onClick={() => openReview(r)} data-testid={`review-${r.id}`}>
                      <CheckCircle2 size={13} className="mr-1" />{t("receipts.reviewPost")}</Button>}
                  {canManage && r.status !== "posted" && <Button size="sm" variant="ghost" onClick={() => del(r.id)}><Trash2 size={13} /></Button>}
                </td>
              </tr>)}
            {!receipts.length && <tr><td colSpan={6} className="px-4 py-8 text-center text-gray-500 text-sm">{t("receipts.noReceiptsYetUploadYourFirst")}</td></tr>}
          </tbody>
        </table>
      </Card>

      <Dialog open={!!review} onOpenChange={() => setReview(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader><DialogTitle>{t("receipts.reviewReceipt")}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("receipts.vendor")}</Label><Input value={form.vendor} onChange={e => setForm({
              ...form,
              vendor: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("receipts.date")}</Label><Input type="date" value={form.expense_date} onChange={e => setForm({
              ...form,
              expense_date: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("receipts.total")}</Label><Input type="number" step="0.01" value={form.amount} onChange={e => setForm({
              ...form,
              amount: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">{t("receipts.category")}</Label>
              <Select value={form.category_code} onValueChange={v => setForm({
              ...form,
              category_code: v
            })}>
                <SelectTrigger className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                <SelectContent>{coa.filter(a => a.code?.startsWith("5")).map(a => <SelectItem key={a.code} value={a.code}>{a.code} — {a.name}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div className="col-span-2"><Label className="text-xs">{t("receipts.description")}</Label><Input value={form.description} onChange={e => setForm({
              ...form,
              description: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("receipts.paidWith")}</Label>
              <Select value={form.payment_method} onValueChange={v => setForm({
              ...form,
              payment_method: v
            })}>
                <SelectTrigger className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="bank">{t("receipts.bank")}</SelectItem><SelectItem value="card">{t("receipts.card")}</SelectItem><SelectItem value="cash">{t("receipts.cash")}</SelectItem></SelectContent>
              </Select>
            </div>
            <div><Label className="text-xs">{t("receipts.bankMatch")}</Label>
              <Select value={form.bank_transaction_id || "none"} onValueChange={v => setForm({
              ...form,
              bank_transaction_id: v === "none" ? "" : v
            })}>
                <SelectTrigger className="bg-[#0F1218] border-[#222838]"><SelectValue placeholder={t("receipts.noMatch")} /></SelectTrigger>
                <SelectContent><SelectItem value="none">{t("receipts.noMatch")}</SelectItem>
                  {matches.map(m => <SelectItem key={m.id} value={m.id}>{fmtDate(m.txn_date)} — {money(m.amount_cents)} — {m.description.slice(0, 30)}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          </div>
          {!!matches.length && <p className="text-xs text-gray-500 flex items-center gap-1"><Link2 size={12} /> {matches.length} {t("receipts.bankTransaction")}{matches.length > 1 ? "s" : ""} {t("receipts.lookLikeThisReceiptPickOne")}</p>}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setReview(null)}>{t("receipts.cancel")}</Button>
            <Button onClick={post} data-testid="receipt-post">{t("receipts.postAsExpense")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
