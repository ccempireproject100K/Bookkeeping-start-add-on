import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { Wallet } from "lucide-react";
import { useTranslation } from "react-i18next";
export default function CashClose() {
  const {
    t
  } = useTranslation();
  const [history, setHistory] = useState(null);
  const [preview, setPreview] = useState(null);
  const [form, setForm] = useState({
    opening_float: "",
    counted_cash: "",
    paid_in: "",
    paid_out: "",
    notes: ""
  });
  const [busy, setBusy] = useState(false);
  const load = () => {
    api.get("/cash-close").then(setHistory).catch(() => setHistory([]));
    api.get("/cash-close/preview").then(setPreview).catch(() => setPreview(null));
  };
  useEffect(() => {
    load();
  }, []);
  const opening = parseFloat(form.opening_float) || 0;
  const paidIn = parseFloat(form.paid_in) || 0;
  const paidOut = parseFloat(form.paid_out) || 0;
  const counted = parseFloat(form.counted_cash) || 0;
  const cashSales = (preview?.cash_sales_cents || 0) / 100;
  const expected = opening + cashSales + paidIn - paidOut;
  const variance = counted - expected;
  const submit = async () => {
    setBusy(true);
    try {
      await api.post("/cash-close", {
        opening_float: opening,
        counted_cash: counted,
        paid_in: paidIn,
        paid_out: paidOut,
        notes: form.notes
      });
      toast.success(t("cashclose.cashCloseRecorded"));
      setForm({
        opening_float: "",
        counted_cash: "",
        paid_in: "",
        paid_out: "",
        notes: ""
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  if (!history) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("cashclose.dailyCashClose")} subtitle="Reconcile the drawer; variance posts to the ledger" testid="cash-close-page" />
      <div className="grid lg:grid-cols-2 gap-6">
        <Card>
          <h3 className="font-display font-semibold mb-4 flex items-center gap-2"><Wallet size={16} className="text-amber-400" /> {t("cashclose.todaySCount")}</h3>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("cashclose.openingFloat")}</Label><Input data-testid="cash-close-opening-balance" value={form.opening_float} onChange={e => setForm({
              ...form,
              opening_float: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono mt-1" /></div>
            <div><Label className="text-xs">{t("cashclose.cashCounted")}</Label><Input data-testid="input-actual-cash-counted" value={form.counted_cash} onChange={e => setForm({
              ...form,
              counted_cash: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono mt-1" /></div>
            <div><Label className="text-xs">{t("cashclose.paidIn")}</Label><Input data-testid="cash-close-paid-in" value={form.paid_in} onChange={e => setForm({
              ...form,
              paid_in: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono mt-1" /></div>
            <div><Label className="text-xs">{t("cashclose.paidOut")}</Label><Input data-testid="cash-close-paid-out" value={form.paid_out} onChange={e => setForm({
              ...form,
              paid_out: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono mt-1" /></div>
          </div>
          <div className="mt-3"><Label className="text-xs">{t("cashclose.notes")}</Label><Input value={form.notes} onChange={e => setForm({
            ...form,
            notes: e.target.value
          })} className="bg-[#0F1218] border-[#222838] mt-1" placeholder={t("cashclose.explanationForAnyVariance")} /></div>

          <div className="mt-4 space-y-2 text-sm font-mono border-t border-[#222838] pt-4">
            <div className="flex justify-between text-gray-400"><span>{t("cashclose.cashSalesToday")}</span><span className="text-gray-200">{money(preview?.cash_sales_cents || 0)}</span></div>
            <div className="flex justify-between text-gray-400"><span>{t("cashclose.expectedInDrawer")}</span><span data-testid="cash-close-expected-cash" className="text-gray-200">${expected.toFixed(2)}</span></div>
            <div className="flex justify-between text-base font-semibold">
              <span>{t("cashclose.variance")}</span>
              <span data-testid="cash-close-variance" className={Math.abs(variance) < 0.005 ? "text-gray-300" : variance > 0 ? "text-emerald-400" : "text-rose-400"}>
                {variance >= 0 ? "+" : ""}${variance.toFixed(2)}
              </span>
            </div>
          </div>
          <Button data-testid="btn-submit-cash-close" disabled={busy || !form.counted_cash} onClick={submit} className="w-full mt-4 bg-amber-600 hover:bg-amber-500 text-black font-semibold">
            {busy ? "Submitting…" : "Submit Cash Close"}
          </Button>
        </Card>

        <Card className="p-0 overflow-hidden">
          <div className="px-5 py-4 border-b border-[#222838]"><h3 className="font-display font-semibold">{t("cashclose.history")}</h3></div>
          <table className="w-full text-sm" data-testid="table-cash-closes">
            <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
              <th className="px-5 py-3 text-left font-medium">{t("cashclose.date")}</th>
              <th className="px-5 py-3 text-right font-medium">{t("cashclose.expected")}</th>
              <th className="px-5 py-3 text-right font-medium">{t("cashclose.counted")}</th>
              <th className="px-5 py-3 text-right font-medium">{t("cashclose.variance")}</th>
            </tr></thead>
            <tbody>
              {history.map(c => <tr key={c.id} className="border-b border-[#171c26]">
                  <td className="px-5 py-3 text-gray-300">{fmtDate(c.date)}</td>
                  <td className="px-5 py-3 text-right font-mono text-gray-400">{money(c.expected_cents)}</td>
                  <td className="px-5 py-3 text-right font-mono text-gray-200">{money(c.counted_cents)}</td>
                  <td className={`px-5 py-3 text-right font-mono ${c.variance_cents === 0 ? "text-gray-400" : c.variance_cents > 0 ? "text-emerald-400" : "text-rose-400"}`}>{money(c.variance_cents)}</td>
                </tr>)}
              {history.length === 0 && <tr><td colSpan={4} className="px-5 py-12 text-center text-gray-500">{t("cashclose.noClosesYet")}</td></tr>}
            </tbody>
          </table>
        </Card>
      </div>
    </div>;
}
