import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { ReceiptText } from "lucide-react";

/**
 * PUBLIC client portal — works WITHOUT login. Wire as a standalone route
 * OUTSIDE the <Protected> wrapper, e.g. <Route path="/p/:token" element={<PortalView />} />.
 */
import { useTranslation } from "react-i18next";
const IN = "bg-[#0F1218] border-[#222838]";
export default function PortalView() {
  const {
    t
  } = useTranslation();
  const {
    token = ""
  } = useParams();
  const [view, setView] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pay, setPay] = useState({
    amount: "",
    payment_method: "bank",
    reference: ""
  });
  const [cardBusy, setCardBusy] = useState(false);
  const [justPaid, setJustPaid] = useState(false);
  const payWithCard = async () => {
    setCardBusy(true);
    try {
      const r = await api.post(`/portal/checkout/${token}`, {
        origin_url: window.location.origin
      });
      window.location.href = r.checkout_url;
    } catch (e) {
      if (e?.response?.status === 503) {
        toast.error("Card payments aren't set up yet — please use another payment method below or contact the business.");
      } else {
        toast.error(errMsg(e));
      }
      setCardBusy(false);
    }
  };
  const load = () => {
    setError("");
    api.get(`/portal/view/${token}`).then(v => {
      setView(v);
      if (v?.invoice) setPay(p => ({
        ...p,
        amount: (v.invoice.balance_cents / 100).toFixed(2)
      }));
    }).catch(() => {
      setView(null);
      setError("This link didn't work. It may have expired — ask the business for a fresh link.");
    });
  };
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    if (q.get("paid") === "1") {
      setJustPaid(true);
      // clean the URL so a refresh doesn't re-show the banner
      window.history.replaceState({}, "", window.location.pathname);
    }
    load();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const doPay = async () => {
    setBusy(true);
    try {
      const r = await api.post(`/portal/pay/${token}`, {
        amount: parseFloat(pay.amount),
        payment_method: pay.payment_method,
        reference: pay.reference
      });
      toast.success(r.status === "paid" ? "Paid in full — thank you!" : "Payment recorded — thank you!");
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
    setBusy(false);
  };
  return <div className="app-bg min-h-screen">
      <div className="mx-auto max-w-2xl px-4 py-10">
        {!view && !error && <p className="text-center text-gray-500 py-16">{t("portalview.loading")}</p>}
        {error && <div className="card-surface p-8 text-center" data-testid="portal-error">
            <p className="font-semibold text-gray-100">{t("portalview.thisLinkDidnTWork")}</p>
            <p className="mt-1 text-sm text-gray-500">{error}</p>
          </div>}
        {view?.invoice && (() => {
        const inv = view.invoice;
        return <div className="card-surface p-6 sm:p-8" data-testid="portal-doc">
            <div className="mb-4 flex items-center gap-2 text-gray-500">
              <ReceiptText size={16} />
              <span className="text-sm">{view.business}{view.business ? " · " : ""}{t("portalview.invoice")}{inv.issue_entry ? ` ${inv.issue_entry}` : ""}</span>
            </div>
            <h1 className="font-display text-2xl font-bold text-gray-100">{inv.client_name}</h1>
            <p className="text-sm text-gray-500 mt-1">
              {inv.created_at && <>{t("portalview.issued")} {fmtDate(inv.created_at.slice(0, 10))} · </>}
              {inv.due_date ? <>{t("portalview.due")} {fmtDate(inv.due_date)}</> : inv.terms}
            </p>

            <table className="w-full text-sm mt-6">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="py-2 text-left font-medium">{t("portalview.description")}</th>
                <th className="py-2 text-right font-medium">{t("portalview.amount")}</th>
              </tr></thead>
              <tbody>
                {inv.lines.map((l, i) => <tr key={i} className="border-b border-[#171c26]">
                    <td className="py-2 text-gray-200">{l.name || l.kind}</td>
                    <td className="py-2 text-right font-mono text-gray-200">{money(l.amount_cents)}</td>
                  </tr>)}
              </tbody>
            </table>

            <div className="mt-3 space-y-1 text-sm">
              <div className="flex justify-between"><span className="text-gray-500">{t("portalview.subtotal")}</span><span className="font-mono text-gray-200">{money(inv.subtotal_cents)}</span></div>
              {inv.tax_cents > 0 && <div className="flex justify-between"><span className="text-gray-500">{t("portalview.tax")}</span><span className="font-mono text-gray-200">{money(inv.tax_cents)}</span></div>}
              <div className="flex justify-between border-t border-[#222838] pt-2 text-base font-semibold">
                <span className="text-gray-100">{t("portalview.total")}</span><span className="font-mono text-gray-100">{money(inv.total_cents)}</span>
              </div>
              <div className="flex justify-between"><span className="text-gray-500">{t("portalview.paid")}</span><span className="font-mono text-emerald-400">{money(inv.paid_cents)}</span></div>
              <div className="flex justify-between text-base font-semibold">
                <span className="text-amber-400">{t("portalview.balanceDue")}</span><span className="font-mono text-amber-400">{money(inv.balance_cents)}</span>
              </div>
            </div>

            {justPaid && <div className="mb-4 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-300" data-testid="portal-paid-banner">{t("portalview.cardPaymentReceivedThankYouIt")}</div>}
            {inv.balance_cents > 0 && inv.status !== "paid" && <div className="mt-6 rounded-lg bg-[#0F1218] border border-[#222838] p-4" data-testid="portal-pay">
                <div className="mb-2 text-sm font-semibold text-gray-100">{t("portalview.payThisInvoice")}</div>
                {view.card_payments_enabled === false ? <div className="mb-3 rounded-lg border border-[#222838] bg-[#0F1218] p-3 text-sm text-gray-400" data-testid="portal-card-unavailable">
                    Card payments aren't set up yet — please use another payment method below or contact the business.
                  </div> : <div className="flex flex-wrap items-center gap-2 mb-3">
                  <Button onClick={payWithCard} disabled={cardBusy} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="portal-pay-card">
                    {cardBusy ? "Starting secure checkout…" : `Pay ${money(inv.balance_cents)} with card`}
                  </Button>
                  <span className="text-[11px] text-gray-600">{t("portalview.secureCheckoutViaStripe")}</span>
                </div>}
                <div className="border-t border-[#222838] pt-3">
                  <div className="mb-2 text-xs font-semibold text-gray-400">{t("portalview.orRecordAnotherPaymentMethod")}</div>
                <div className="flex flex-wrap items-end gap-2">
                  <div><Label className="text-xs">{t("portalview.amount2")}</Label>
                    <Input type="number" step="0.01" min="0.01" value={pay.amount} onChange={e => setPay({
                    ...pay,
                    amount: e.target.value
                  })} className={`${IN} w-32 font-mono`} /></div>
                  <div><Label className="text-xs">{t("portalview.method")}</Label>
                    <Select value={pay.payment_method} onValueChange={v => setPay({
                    ...pay,
                    payment_method: v
                  })}>
                      <SelectTrigger className={`${IN} w-32`}><SelectValue /></SelectTrigger>
                      <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
                        <SelectItem value="bank">{t("portalview.bankTransfer")}</SelectItem>
                        <SelectItem value="card">{t("portalview.card")}</SelectItem>
                        <SelectItem value="cash">{t("portalview.cash")}</SelectItem>
                      </SelectContent>
                    </Select></div>
                  <div className="flex-1 min-w-[140px]"><Label className="text-xs">{t("portalview.referenceOptional")}</Label>
                    <Input value={pay.reference} onChange={e => setPay({
                    ...pay,
                    reference: e.target.value
                  })} className={IN} placeholder={t("portalview.checkEtc")} /></div>
                  <Button size="sm" disabled={busy || !parseFloat(pay.amount)} onClick={doPay} className="bg-emerald-600 hover:bg-emerald-500 text-black font-semibold">{t("portalview.payNow")}</Button>
                </div>
                <p className="mt-1 text-[11px] text-gray-600">{t("portalview.paymentsAreRecordedImmediatelyAgainstThe")}</p>
                </div>
              </div>}
            {inv.status === "paid" && <p className="mt-6 text-sm text-emerald-400 font-semibold">{t("portalview.thisInvoiceIsPaidInFull")}</p>}
          </div>;
      })()}
        <p className="mt-6 text-center text-xs text-gray-600">{t("portalview.secureLinkItExpiresAfterIts")}</p>
      </div>
    </div>;
}
