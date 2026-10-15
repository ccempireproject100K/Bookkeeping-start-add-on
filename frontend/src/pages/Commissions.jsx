import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, dollars, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { toast } from "sonner";
import { Coins, Receipt } from "lucide-react";
import { useTranslation } from "react-i18next";
export default function Commissions() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const isOwner = user?.role === "owner";
  const [summary, setSummary] = useState(null);
  const [payouts, setPayouts] = useState([]);
  const [statement, setStatement] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = () => {
    api.get("/commissions/summary").then(setSummary);
    api.get("/commissions/payouts").then(setPayouts).catch(() => setPayouts([]));
  };
  useEffect(() => {
    load();
  }, []);
  const viewStatement = (barberId, name) => api.get(`/commissions/statement/${barberId}`).then(d => setStatement({
    ...d,
    name
  }));
  const payOut = async barberId => {
    setBusy(true);
    try {
      const r = await api.post("/commissions/payout", {
        barber_id: barberId,
        payment_method: "cash"
      });
      toast.success(`Paid ${dollars(r.amount)} — ${r.entry_number}`);
      setStatement(null);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  if (!summary) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("commissions.commissionPayouts")} subtitle="Accrued barber commission (2300 payable) → pay out" testid="commissions-page" />

      <div className="grid sm:grid-cols-2 gap-4 mb-6">
        {summary.map(b => <Card key={b.barber_id} data-testid={`commission-card-${b.barber_id}`} className="hover:border-[#34404f] transition-colors">
            <div className="flex items-start justify-between">
              <div>
                <div className="font-display font-semibold text-lg">{b.name}</div>
                <div className="text-xs text-gray-500">{Math.round((b.commission_rate || 0) * 100)}{t("commissions.commissionRate")}</div>
              </div>
              <Coins size={20} className="text-amber-400" />
            </div>
            <div className="grid grid-cols-2 gap-3 mt-4">
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] px-3 py-2">
                <div className="text-[10px] uppercase tracking-wider text-gray-500">{t("commissions.accruedUnpaid")}</div>
                <div className="font-mono text-xl text-emerald-400 mt-1" data-testid={`accrued-${b.barber_id}`}>{dollars(b.accrued)}</div>
                <div className="text-[10px] text-gray-500">{b.unpaid_count} {t("commissions.sales")}</div>
              </div>
              <div className="rounded-lg bg-[#0F1218] border border-[#222838] px-3 py-2">
                <div className="text-[10px] uppercase tracking-wider text-gray-500">{t("commissions.paidToDate")}</div>
                <div className="font-mono text-xl text-gray-300 mt-1">{dollars(b.paid)}</div>
              </div>
            </div>
            <div className="flex gap-2 mt-4">
              <Button size="sm" variant="outline" className="flex-1 border-[#2a3140]" onClick={() => viewStatement(b.barber_id, b.name)} data-testid={`statement-${b.barber_id}`}>
                <Receipt size={14} className="mr-1" />{t("commissions.statement")}</Button>
              {isOwner && <Button size="sm" disabled={busy || b.accrued_cents <= 0} className="flex-1 bg-amber-600 hover:bg-amber-500 text-black disabled:opacity-40" onClick={() => payOut(b.barber_id)} data-testid={`payout-${b.barber_id}`}>{t("commissions.payOut")}</Button>}
            </div>
          </Card>)}
      </div>

      <Card className="p-0 overflow-hidden">
        <div className="px-5 py-4 border-b border-[#222838]"><h3 className="font-display font-semibold">{t("commissions.payoutHistory")}</h3></div>
        <table className="w-full text-sm" data-testid="table-payouts">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="px-5 py-3 text-left font-medium">{t("commissions.date")}</th>
            <th className="px-5 py-3 text-left font-medium">{t("commissions.barber")}</th>
            <th className="px-5 py-3 text-left font-medium">{t("commissions.entry")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("commissions.sales2")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("commissions.amount")}</th>
          </tr></thead>
          <tbody>
            {payouts.map(p => <tr key={p.id} className="border-b border-[#171c26]">
                <td className="px-5 py-3 text-gray-400">{fmtDate(p.created_at?.slice(0, 10))}</td>
                <td className="px-5 py-3 text-gray-200">{p.barber_name}</td>
                <td className="px-5 py-3 font-mono text-amber-400">{p.entry_number}</td>
                <td className="px-5 py-3 text-right text-gray-400">{p.sale_count}</td>
                <td className="px-5 py-3 text-right font-mono text-gray-100">{dollars(p.amount)}</td>
              </tr>)}
            {payouts.length === 0 && <tr><td colSpan={5} className="px-5 py-10 text-center text-gray-500">{t("commissions.noPayoutsYet")}</td></tr>}
          </tbody>
        </table>
      </Card>

      <Dialog open={!!statement} onOpenChange={o => !o && setStatement(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-xl">
          {statement && <>
              <DialogHeader><DialogTitle>{t("commissions.commissionStatement")} {statement.name}</DialogTitle></DialogHeader>
              <div className="max-h-[320px] overflow-y-auto">
                <table className="w-full text-sm">
                  <thead><tr className="text-xs uppercase text-gray-500 border-b border-[#222838]">
                    <th className="py-2 text-left">{t("commissions.date")}</th><th className="py-2 text-left">{t("commissions.entry")}</th><th className="py-2 text-right">{t("commissions.sale")}</th><th className="py-2 text-right">{t("commissions.commission")}</th></tr></thead>
                  <tbody>
                    {statement.lines.map((l, i) => <tr key={i} className="border-b border-[#171c26]">
                        <td className="py-2 text-gray-400">{fmtDate(l.date)}</td>
                        <td className="py-2 font-mono text-amber-400">{l.entry}</td>
                        <td className="py-2 text-right font-mono text-gray-400">{dollars(l.sale_total)}</td>
                        <td className="py-2 text-right font-mono text-emerald-400">{dollars(l.commission)}</td>
                      </tr>)}
                    {statement.lines.length === 0 && <tr><td colSpan={4} className="py-6 text-center text-gray-600">{t("commissions.nothingAccrued")}</td></tr>}
                  </tbody>
                </table>
              </div>
              <div className="flex items-center justify-between border-t border-[#222838] pt-3 font-mono font-semibold">
                <span>{t("commissions.totalDue")}</span><span className="text-amber-400">{dollars(statement.total)}</span>
              </div>
              {isOwner && statement.total_cents > 0 && <Button disabled={busy} onClick={() => payOut(statement.barber_id)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold">{t("commissions.payOut")} {dollars(statement.total)}</Button>}
            </>}
        </DialogContent>
      </Dialog>
    </div>;
}
