import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, moneyFor, fmtDate } from "@/lib/format";
import CurrencyPicker from "@/components/CurrencyPicker";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { toast } from "sonner";
import { Plus, Trash2, FilePlus, Link2 } from "lucide-react";
import PortalLinksTab, { quickPaymentLink } from "@/components/PortalLinksTab";
import { useTranslation } from "react-i18next";
const STATUS_CLS = {
  issued: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  partial: "bg-sky-500/10 text-sky-400 border-sky-500/20",
  paid: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
};
export default function Invoices() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const isOwner = user?.role === "owner";
  const [invoices, setInvoices] = useState(null);
  const [credits, setCredits] = useState([]);
  const [open, setOpen] = useState(false);
  const [client, setClient] = useState("");
  const [tax, setTax] = useState("");
  const [lines, setLines] = useState([{
    name: "",
    revenue_code: "4020",
    amount: ""
  }]);
  const [payInv, setPayInv] = useState(null);
  const [pay, setPay] = useState({
    amount: "",
    payment_method: "bank",
    credit_id: ""
  });
  const [juris, setJuris] = useState([]);
  const [jurisId, setJurisId] = useState("");
  const [invCurrency, setInvCurrency] = useState("USD");
  const [creditOpen, setCreditOpen] = useState(false);
  const [creditForm, setCreditForm] = useState({
    client_name: "",
    amount: "",
    memo: ""
  });
  const [refundFor, setRefundFor] = useState(null);
  const [refundMethod, setRefundMethod] = useState("bank");
  const issueCredit = async () => {
    if (!creditForm.client_name.trim() || !(parseFloat(creditForm.amount) > 0)) {
      toast.error(t("invoices.customerAndAPositiveAmountAre"));
      return;
    }
    try {
      await api.post("/invoices/customer-credits", {
        client_name: creditForm.client_name.trim(),
        amount: parseFloat(creditForm.amount),
        memo: creditForm.memo.trim()
      });
      toast.success(t("invoices.creditIssued"));
      setCreditOpen(false);
      setCreditForm({
        client_name: "",
        amount: "",
        memo: ""
      });
      loadCredits();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const refundCredit = async () => {
    if (!refundFor) return;
    if (!window.confirm(`Refund ${money(refundFor.remaining_cents)} to ${refundFor.client_name} via ${refundMethod}?`)) return;
    try {
      const r = await api.post(`/invoices/customer-credits/${refundFor.id}/refund`, {
        payment_method: refundMethod
      });
      toast.success(`Refunded ${money(r.refunded_cents)}`);
      setRefundFor(null);
      loadCredits();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const load = () => api.get("/invoices").then(setInvoices).catch(() => setInvoices([]));
  const loadCredits = () => api.get("/invoices/customer-credits").then(setCredits).catch(() => {});
  useEffect(() => {
    load();
    loadCredits();
  }, []);
  const create = async () => {
    try {
      await api.post("/invoices", {
        client_name: client,
        tax: parseFloat(tax) || 0,
        tax_jurisdiction_id: jurisId || null,
        currency: invCurrency,
        lines: lines.filter(l => l.name && l.amount).map(l => ({
          kind: "service",
          name: l.name,
          revenue_code: l.revenue_code,
          amount: parseFloat(l.amount) || 0
        }))
      });
      toast.success(t("invoices.invoiceIssued"));
      setOpen(false);
      setClient("");
      setTax("");
      setJurisId("");
      setInvCurrency("USD");
      setLines([{
        name: "",
        revenue_code: "4020",
        amount: ""
      }]);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const openNewInvoice = () => {
    setOpen(true);
    setJurisId("");
    api.get("/sales-tax/jurisdictions").then(j => setJuris((j || []).filter(x => x.active))).catch(() => setJuris([]));
  };
  const pickJurisdiction = id => {
    setJurisId(id);
    if (!id) return;
    const j = juris.find(x => x.id === id);
    if (!j) return;
    const subtotal = lines.filter(l => l.name && l.amount).reduce((s, l) => s + (parseFloat(l.amount) || 0), 0);
    setTax((subtotal * j.rate_pct / 100).toFixed(2));
  };
  const doPay = async () => {
    try {
      const r = await api.post(`/invoices/${payInv.id}/pay`, {
        payment_method: pay.payment_method,
        amount: pay.amount ? parseFloat(pay.amount) : null,
        credit_id: pay.credit_id || null
      });
      toast.success(r.overpayment_cents > 0 ? `Paid — ${money(r.overpayment_cents)} saved as customer credit` : "Payment recorded");
      setPayInv(null);
      setPay({
        amount: "",
        payment_method: "bank",
        credit_id: ""
      });
      load();
      loadCredits();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!invoices) return <Loading />;
  const invCredits = payInv ? credits.filter(c => c.client_name === payInv.client_name && c.remaining_cents > 0) : [];
  return <div className="animate-rise">
      <PageHeader title={t("invoices.invoices")} subtitle="Receivables — partial payments, credits & statements" testid="invoices-page">
        {isOwner && <Button data-testid="btn-new-invoice" onClick={openNewInvoice} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" /> {t("invoices.newInvoice")}</Button>}
      </PageHeader>

      <Tabs defaultValue="invoices">
        <TabsList className="bg-[#14171E] border border-[#222838]">
          <TabsTrigger value="invoices" data-testid="tab-invoices">{t("invoices.invoices")}</TabsTrigger>
          <TabsTrigger value="credits" data-testid="tab-customer-credits">{t("invoices.customerCredits")}</TabsTrigger>
          <TabsTrigger value="reminders" data-testid="tab-reminders">{t("invoices.reminders")}</TabsTrigger>
          {isOwner && <TabsTrigger value="links" data-testid="tab-payment-links">{t("invoices.paymentLinks")}</TabsTrigger>}
        </TabsList>

        <TabsContent value="invoices" className="mt-4">
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-invoices">
              <thead>
                <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="px-5 py-3 text-left font-medium">{t("invoices.client")}</th>
                  <th className="px-5 py-3 text-left font-medium">{t("invoices.issued")}</th>
                  <th className="px-5 py-3 text-right font-medium">{t("invoices.total")}</th>
                  <th className="px-5 py-3 text-right font-medium">{t("invoices.balance")}</th>
                  <th className="px-5 py-3 text-left font-medium">{t("invoices.status")}</th>
                  {isOwner && <th className="px-5 py-3 text-right font-medium">{t("invoices.actions")}</th>}
                </tr>
              </thead>
              <tbody>
                {invoices.map(inv => <tr key={inv.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-5 py-3 text-gray-100">{inv.client_name}</td>
                    <td className="px-5 py-3 text-gray-400">{fmtDate(inv.created_at?.slice(0, 10))}</td>
                    <td className="px-5 py-3 text-right font-mono text-gray-200">{moneyFor(inv.total_cents, inv.currency)}</td>
                    <td className="px-5 py-3 text-right font-mono text-amber-400">{moneyFor(inv.balance_cents, inv.currency)}</td>
                    <td className="px-5 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[inv.status] || ""}`}>{inv.status}</span></td>
                    {isOwner && <td className="px-5 py-3 text-right">
                        <div className="flex justify-end gap-1">
                          {inv.balance_cents > 0 && <>
                              <Button size="sm" variant="ghost" title={t("invoices.copyA7DayPaymentLink")} onClick={async () => {
                        try {
                          await quickPaymentLink(inv.id);
                        } catch (e) {
                          toast.error(errMsg(e));
                        }
                      }} data-testid={`plink-${inv.id}`}>
                                <Link2 size={14} />
                              </Button>
                              <Button size="sm" className="h-7 bg-emerald-600 hover:bg-emerald-500 text-black" onClick={() => {
                        setPayInv(inv);
                        setPay({
                          amount: "",
                          payment_method: "bank",
                          credit_id: ""
                        });
                      }} data-testid={`pay-${inv.id}`}>{t("invoices.receivePayment")}</Button>
                            </>}
                        </div>
                      </td>}
                  </tr>)}
                {invoices.length === 0 && <tr><td colSpan={isOwner ? 6 : 5} className="px-5 py-12 text-center text-gray-500">{t("invoices.noInvoices")}</td></tr>}
              </tbody>
            </table>
          </Card>
        </TabsContent>

        <TabsContent value="credits" className="mt-4">
          {isOwner && <div className="flex justify-end mb-3">
              <Button onClick={() => setCreditOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="credit-issue-open">
                <Plus size={15} className="mr-1" /> {t("invoices.issueCredit")}</Button>
            </div>}
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-cust-credits">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-5 py-3 text-left font-medium">{t("invoices.customer")}</th><th className="px-5 py-3 text-left font-medium">{t("invoices.source")}</th>
                <th className="px-5 py-3 text-right font-medium">{t("invoices.amount")}</th><th className="px-5 py-3 text-right font-medium">{t("invoices.remaining")}</th>
                {isOwner && <th className="px-5 py-3 text-right font-medium">{t("invoices.actions")}</th>}</tr></thead>
              <tbody>
                {credits.map(c => <tr key={c.id} className="border-b border-[#171c26]">
                    <td className="px-5 py-3 text-gray-100">{c.client_name}</td><td className="px-5 py-3 text-gray-400 capitalize">{(c.source || "").replace("_", " ")}</td>
                    <td className="px-5 py-3 text-right font-mono text-gray-300">{money(c.amount_cents)}</td>
                    <td className="px-5 py-3 text-right font-mono text-emerald-400">{money(c.remaining_cents)}</td>
                    {isOwner && <td className="px-5 py-3 text-right">
                        {c.remaining_cents > 0 && c.status !== "refunded" && <Button size="sm" variant="outline" className="h-7" onClick={() => setRefundFor(c)} data-testid={`credit-refund-${c.id}`}>{t("invoices.refund")}</Button>}
                      </td>}
                  </tr>)}
                {credits.length === 0 && <tr><td colSpan={isOwner ? 5 : 4} className="px-5 py-12 text-center text-gray-500">{t("invoices.noCustomerCreditsOverpaymentsCreateCredits")}</td></tr>}
              </tbody>
            </table>
          </Card>
        </TabsContent>
        <TabsContent value="reminders" className="mt-4">
          <RemindersTab isOwner={isOwner} />
        </TabsContent>
        {isOwner && <TabsContent value="links" className="mt-4">
            <PortalLinksTab invoices={invoices} />
          </TabsContent>}
      </Tabs>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg">
          <DialogHeader><DialogTitle>{t("invoices.newInvoice")}</DialogTitle></DialogHeader>
          <div><Label className="text-xs">{t("invoices.client")}</Label><Input data-testid="invoice-client" value={client} onChange={e => setClient(e.target.value)} className="bg-[#0F1218] border-[#222838]" /></div>
          <CurrencyPicker value={invCurrency} onChange={setInvCurrency} testid="invoice-currency" />
          <div className="space-y-2">
            <Label className="text-xs">{t("invoices.lineItems")}</Label>
            {lines.map((l, i) => <div key={i} className="flex gap-2 items-center">
                <Input placeholder={t("invoices.description")} value={l.name} onChange={e => {
              const c = [...lines];
              c[i].name = e.target.value;
              setLines(c);
            }} className="bg-[#0F1218] border-[#222838] flex-1" data-testid={`invoice-line-name-${i}`} />
                <Input placeholder="0.00" value={l.amount} onChange={e => {
              const c = [...lines];
              c[i].amount = e.target.value;
              setLines(c);
            }} className="bg-[#0F1218] border-[#222838] w-28 font-mono text-right" data-testid={`invoice-line-amount-${i}`} />
                {lines.length > 1 && <button onClick={() => setLines(lines.filter((_, x) => x !== i))} className="text-gray-600 hover:text-rose-400"><Trash2 size={14} /></button>}
              </div>)}
            <Button variant="ghost" size="sm" onClick={() => setLines([...lines, {
            name: "",
            revenue_code: "4020",
            amount: ""
          }])} className="text-amber-400 hover:bg-amber-500/10"><FilePlus size={14} className="mr-1" />{t("invoices.addLine")}</Button>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("invoices.taxJurisdictionOptional")}</Label>
              <Select value={jurisId} onValueChange={pickJurisdiction}>
                <SelectTrigger data-testid="invoice-jurisdiction" className="bg-[#0F1218] border-[#222838]"><SelectValue placeholder={t("invoices.none")} /></SelectTrigger>
                <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
                  {juris.map(j => <SelectItem key={j.id} value={j.id}>{j.name} · {j.rate_pct}%</SelectItem>)}
                </SelectContent>
              </Select></div>
            <div><Label className="text-xs">{t("invoices.tax")}</Label><Input data-testid="invoice-tax" value={tax} onChange={e => setTax(e.target.value)} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          </div>
          <DialogFooter><Button data-testid="invoice-save" onClick={create} className="bg-amber-600 hover:bg-amber-500 text-black">{t("invoices.issueInvoice")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!payInv} onOpenChange={o => !o && setPayInv(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("invoices.receivePayment2")} {payInv?.client_name}</DialogTitle></DialogHeader>
          {payInv && <div className="text-sm text-gray-400">{t("invoices.balanceDue")} <span className="font-mono text-amber-400">{money(payInv.balance_cents)}</span></div>}
          <div><Label className="text-xs">{t("invoices.amountBlankFullBalance")}</Label><Input data-testid="payinv-amount" value={pay.amount} onChange={e => setPay({
            ...pay,
            amount: e.target.value
          })} className="bg-[#0F1218] border-[#222838] font-mono" placeholder={t("invoices.fullBalance")} /></div>
          <div><Label className="text-xs">{t("invoices.depositTo")}</Label>
            <Select value={pay.payment_method} onValueChange={v => setPay({
            ...pay,
            payment_method: v
          })}>
              <SelectTrigger data-testid="payinv-method" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100"><SelectItem value="bank">{t("invoices.bank")}</SelectItem><SelectItem value="card">{t("invoices.card")}</SelectItem><SelectItem value="cash">{t("invoices.cash")}</SelectItem></SelectContent>
            </Select></div>
          {invCredits.length > 0 && <div><Label className="text-xs">{t("invoices.applyCustomerCredit")}</Label>
            <Select value={pay.credit_id} onValueChange={v => setPay({
            ...pay,
            credit_id: v
          })}>
              <SelectTrigger data-testid="payinv-credit" className="bg-[#0F1218] border-[#222838]"><SelectValue placeholder={t("invoices.none")} /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">{invCredits.map(c => <SelectItem key={c.id} value={c.id}>{money(c.remaining_cents)} {t("invoices.available")}</SelectItem>)}</SelectContent>
            </Select></div>}
          <DialogFooter><Button data-testid="payinv-save" onClick={doPay} className="bg-emerald-600 hover:bg-emerald-500 text-black">{t("invoices.recordPayment")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={creditOpen} onOpenChange={setCreditOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]" data-testid="credit-issue-dialog">
          <DialogHeader><DialogTitle>{t("invoices.issueCustomerCredit")}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("invoices.customer2")}</Label><Input value={creditForm.client_name} onChange={e => setCreditForm({
              ...creditForm,
              client_name: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" data-testid="credit-client" /></div>
            <div><Label className="text-xs">{t("invoices.amount2")}</Label><Input type="number" step="0.01" min={0.01} value={creditForm.amount} onChange={e => setCreditForm({
              ...creditForm,
              amount: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" data-testid="credit-amount" /></div>
          </div>
          <div><Label className="text-xs">{t("invoices.memo")}</Label><Input value={creditForm.memo} onChange={e => setCreditForm({
            ...creditForm,
            memo: e.target.value
          })} placeholder={t("invoices.eGGoodwillCreditForLate")} className="bg-[#0F1218] border-[#222838]" /></div>
          <p className="text-[11px] text-gray-600">{t("invoices.postsDrRevenueCrCustomerCredits")}</p>
          <DialogFooter><Button onClick={issueCredit} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="credit-issue-save">{t("invoices.issueCredit")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!refundFor} onOpenChange={o => !o && setRefundFor(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838]" data-testid="credit-refund-dialog">
          <DialogHeader><DialogTitle>{t("invoices.refundCredit")} {refundFor?.client_name}</DialogTitle></DialogHeader>
          <p className="text-sm text-gray-300">{t("invoices.refundTheRemaining")} <strong className="font-mono">{money(refundFor?.remaining_cents)}</strong> {t("invoices.backToTheCustomerPostsDr")}</p>
          <div><Label className="text-xs">{t("invoices.payFrom")}</Label>
            <Select value={refundMethod} onValueChange={setRefundMethod}>
              <SelectTrigger className="bg-[#0F1218] border-[#222838]" data-testid="credit-refund-method"><SelectValue /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
                <SelectItem value="bank">{t("invoices.bankAccount")}</SelectItem>
                <SelectItem value="cash">{t("invoices.cash")}</SelectItem>
                <SelectItem value="card">{t("invoices.card")}</SelectItem>
              </SelectContent>
            </Select></div>
          <DialogFooter><Button onClick={refundCredit} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="credit-refund-confirm">{t("invoices.refund")} {money(refundFor?.remaining_cents)}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
function RemindersTab({
  isOwner
}) {
  const {
    t
  } = useTranslation();
  const [overdue, setOverdue] = useState(null);
  const [hist, setHist] = useState([]);
  const [draftInv, setDraftInv] = useState(null);
  const [msg, setMsg] = useState("");
  const load = () => {
    api.get("/reminders/overdue").then(setOverdue).catch(() => setOverdue([]));
    api.get("/reminders").then(setHist).catch(() => setHist([]));
  };
  useEffect(() => {
    load();
  }, []);
  const openDraft = async inv => {
    try {
      const d = await api.post("/reminders/draft", {
        invoice_id: inv.invoice_id
      });
      setDraftInv(inv);
      setMsg(d.message);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const send = async () => {
    try {
      await api.post("/reminders/send", {
        invoice_id: draftInv.invoice_id,
        message: msg
      });
      toast.success(t("invoices.reminderRecordedAsSent"));
      setDraftInv(null);
      setMsg("");
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (overdue === null) return <Loading />;
  return <div className="space-y-6">
      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm" data-testid="table-overdue">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="px-5 py-3 text-left font-medium">{t("invoices.client")}</th>
            <th className="px-5 py-3 text-left font-medium">{t("invoices.due")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("invoices.daysOverdue")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("invoices.balance")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("invoices.reminders")}</th>
            {isOwner && <th className="px-5 py-3 text-right font-medium">{t("invoices.actions")}</th>}</tr></thead>
          <tbody>
            {overdue.map(o => <tr key={o.invoice_id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                <td className="px-5 py-3 text-gray-100">{o.client_name}</td>
                <td className="px-5 py-3 text-gray-400">{fmtDate(o.due_date)}</td>
                <td className="px-5 py-3 text-right font-mono text-rose-400">{o.days_overdue}</td>
                <td className="px-5 py-3 text-right font-mono text-amber-400">{money(o.balance_cents)}</td>
                <td className="px-5 py-3 text-right text-gray-400">{o.reminders_count}</td>
                {isOwner && <td className="px-5 py-3 text-right">
                    <Button size="sm" variant="outline" className="h-7 border-[#222838]" onClick={() => openDraft(o)} data-testid={`remind-${o.invoice_id}`}>{t("invoices.draftReminder")}</Button>
                  </td>}
              </tr>)}
            {overdue.length === 0 && <tr><td colSpan={isOwner ? 6 : 5} className="px-5 py-12 text-center text-gray-500">{t("invoices.noOverdueInvoices")}</td></tr>}
          </tbody>
        </table>
      </Card>

      <div>
        <h3 className="text-sm font-semibold text-gray-200 mb-2">{t("invoices.reminderHistory")}</h3>
        <Card className="p-0 overflow-hidden">
          <table className="w-full text-sm" data-testid="table-reminders">
            <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
              <th className="px-5 py-3 text-left font-medium">{t("invoices.sent")}</th>
              <th className="px-5 py-3 text-left font-medium">{t("invoices.client")}</th>
              <th className="px-5 py-3 text-left font-medium">{t("invoices.by")}</th>
              <th className="px-5 py-3 text-left font-medium">{t("invoices.message")}</th></tr></thead>
            <tbody>
              {hist.map(r => <tr key={r.id} className="border-b border-[#171c26]">
                  <td className="px-5 py-3 text-gray-400 text-xs whitespace-nowrap">{fmtDate(r.sent_at?.slice(0, 10))}</td>
                  <td className="px-5 py-3 text-gray-100">{r.client_name}</td>
                  <td className="px-5 py-3 text-gray-400">{r.sent_by || "—"}</td>
                  <td className="px-5 py-3 text-gray-400 text-xs max-w-md truncate" title={r.message}>{r.message}</td>
                </tr>)}
              {hist.length === 0 && <tr><td colSpan={4} className="px-5 py-10 text-center text-gray-500">{t("invoices.noRemindersSentYet")}</td></tr>}
            </tbody>
          </table>
        </Card>
      </div>

      <Dialog open={!!draftInv} onOpenChange={o => !o && setDraftInv(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg">
          <DialogHeader><DialogTitle>{t("invoices.paymentReminder")} {draftInv?.client_name}</DialogTitle></DialogHeader>
          <p className="text-xs text-gray-500">{t("invoices.noEmailIsSentAutomaticallyCopy")}</p>
          <textarea value={msg} onChange={e => setMsg(e.target.value)} rows={8} className="w-full rounded-md bg-[#0F1218] border border-[#222838] p-3 text-sm text-gray-100" data-testid="reminder-message" />
          <DialogFooter><Button data-testid="reminder-send" onClick={send} className="bg-amber-600 hover:bg-amber-500 text-black">{t("invoices.markAsSent")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
