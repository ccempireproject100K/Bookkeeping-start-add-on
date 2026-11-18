import { Fragment, useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, moneyFor, fmtDate } from "@/lib/format";
import CurrencyPicker from "@/components/CurrencyPicker";
import { Link } from "react-router-dom";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { toast } from "sonner";
import {
  Plus, Trash2, FilePlus, Link2, Send, MessageSquare, MessageCircle, MessagesSquare, CreditCard, Scissors,
  FileDown, Zap, ChevronDown, ChevronRight, Copy, ExternalLink, AlertTriangle,
  Bell, Sparkles, CheckCheck, Eye, PhoneCall, GraduationCap,
  TrendingUp, Camera, PenLine, LifeBuoy,
} from "lucide-react";
import { Switch } from "@/components/ui/switch";
import PortalLinksTab, { quickPaymentLink } from "@/components/PortalLinksTab";
import { useTranslation } from "react-i18next";
import InvoiceAssistantMascot from "@/components/InvoiceAssistantMascot";
import { useMonsterTour } from "@/components/MonsterTour";
import FreeHelpPanel from "@/components/FreeHelpPanel";
const STATUS_CLS = {
  issued: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  partial: "bg-sky-500/10 text-sky-400 border-sky-500/20",
  paid: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  split: "bg-violet-500/10 text-violet-400 border-violet-500/20"
};

const EVENT_LABEL = {
  issued: "Issued", sent: "Sent", viewed: "Viewed", paid: "Paid",
  reminder_sent: "Reminder sent", autopay_failed: "Autopay failed", split: "Split into parts",
};

const SEND_CLS = {
  sent: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  skipped: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  failed: "bg-rose-500/10 text-rose-400 border-rose-500/20",
};

async function copyText(text, what) {
  try { await navigator.clipboard.writeText(text); toast.success(`${what} copied`); }
  catch { toast.error("Couldn't copy — copy it manually"); }
}

function fmtWhen(s) {
  if (!s) return "—";
  try { return new Date(s).toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }); }
  catch { return s; }
}
export default function Invoices() {
  const {
    t
  } = useTranslation();
  const tour = useMonsterTour({
    onFullWalkthroughComplete: () => {
      // Records completion server-side; the assistant then stops
      // proactively offering the full walkthrough for a few weeks.
      api.post("/invoice-assistant/walkthrough-completed").catch(() => {});
    },
  });
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
  const [epPercent, setEpPercent] = useState("");
  const [epDays, setEpDays] = useState("");
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
  // InvoiceEase fold-in: delivery / pay-link / split / timeline / autopay
  const [busyId, setBusyId] = useState("");
  const [lastSends, setLastSends] = useState({}); // invoiceId -> sends[]
  const [expandedId, setExpandedId] = useState("");
  const [timelines, setTimelines] = useState({}); // invoiceId -> timeline payload
  const [payLinkFor, setPayLinkFor] = useState(null);
  const [payLinkMethod, setPayLinkMethod] = useState("card");
  const [payLinkResult, setPayLinkResult] = useState(null);
  const [splitFor, setSplitFor] = useState(null);
  const [splitMode, setSplitMode] = useState("deposit");
  const [depositPct, setDepositPct] = useState("50");
  const [splitParts, setSplitParts] = useState([
    { label: "Deposit", amount: "", due_date: "" },
    { label: "Balance", amount: "", due_date: "" },
  ]);

  const doSend = async (inv, channel) => {
    setBusyId(`${inv.id}:${channel}`);
    try {
      const r = await api.post(`/invoices/${inv.id}/send`, { channel });
      const sends = r.sends || [];
      setLastSends((m) => ({ ...m, [inv.id]: sends }));
      setExpandedId(inv.id);
      setTimelines((m) => { const c = { ...m }; delete c[inv.id]; return c; });
      loadTimeline(inv.id);
      const CH_LABEL = { email: "Email", sms: "Text", whatsapp: "WhatsApp" };
      sends.forEach((s) => {
        const label = CH_LABEL[s.channel] || s.channel;
        if (s.status === "sent") toast.success(`${label} sent`);
        else toast[s.status === "failed" ? "error" : "warning"](
          `${label} ${s.status}${s.error ? `: ${s.error}` : ""}`);
      });
      if (!sends.length) toast.warning("Nothing was sent");
    } catch (e) { toast.error(errMsg(e)); }
    setBusyId("");
  };

  const loadTimeline = async (id) => {
    try { const t = await api.get(`/invoices/${id}/timeline`); setTimelines((m) => ({ ...m, [id]: t })); }
    catch (e) { toast.error(errMsg(e)); }
  };
  const toggleTimeline = (inv) => {
    if (expandedId === inv.id) { setExpandedId(""); return; }
    setExpandedId(inv.id);
    if (!timelines[inv.id]) loadTimeline(inv.id);
  };

  const doPayLink = async () => {
    if (!payLinkFor) return;
    setBusyId(`${payLinkFor.id}:paylink`);
    try {
      const r = await api.post(`/invoices/${payLinkFor.id}/pay-link`, {
        method: payLinkMethod, origin_url: window.location.origin,
      });
      setPayLinkResult(r);
    } catch (e) { toast.error(errMsg(e)); }
    setBusyId("");
  };

  const openSplit = (inv) => {
    const total = (inv.total_cents || 0) / 100;
    const dep = Math.round(total * 0.5 * 100) / 100;
    setSplitFor(inv); setSplitMode("deposit"); setDepositPct("50");
    setSplitParts([
      { label: "Deposit", amount: dep.toFixed(2), due_date: "" },
      { label: "Balance", amount: (total - dep).toFixed(2), due_date: "" },
    ]);
  };
  const applyDepositPct = (pctStr) => {
    setDepositPct(pctStr);
    if (!splitFor) return;
    const pct = parseFloat(pctStr);
    if (!(pct > 0 && pct < 100)) return;
    const total = (splitFor.total_cents || 0) / 100;
    const dep = Math.round(total * pct) / 100;
    setSplitParts([
      { label: "Deposit", amount: dep.toFixed(2), due_date: splitParts[0]?.due_date || "" },
      { label: "Balance", amount: (total - dep).toFixed(2), due_date: splitParts[1]?.due_date || "" },
    ]);
  };
  const doSplit = async () => {
    if (!splitFor) return;
    const parts = splitParts.filter((p) => p.label.trim() && parseFloat(p.amount) > 0)
      .map((p) => ({ label: p.label.trim(), amount: parseFloat(p.amount), due_date: p.due_date || null }));
    if (parts.length < 2) { toast.error("A split needs at least two parts"); return; }
    const sum = parts.reduce((s, p) => s + p.amount, 0);
    if (Math.abs(sum - splitFor.total_cents / 100) > 0.01) {
      toast.error(`Parts add up to $${sum.toFixed(2)} — they must equal the invoice total of ${money(splitFor.total_cents)}`); return;
    }
    setBusyId(`${splitFor.id}:split`);
    try {
      const r = await api.post(`/invoices/${splitFor.id}/split`, { parts });
      toast.success(`Split into ${r.parts.length} parts`);
      setSplitFor(null); load();
    } catch (e) { toast.error(errMsg(e)); }
    setBusyId("");
  };

  const doAutopayCharge = async (inv) => {
    if (!window.confirm(`Charge ${money(inv.balance_cents)} to ${inv.client_name}'s saved payment method now?`)) return;
    setBusyId(`${inv.id}:autopay`);
    try {
      const r = await api.post(`/invoices/${inv.id}/autopay/charge`);
      toast.success(r.balance_cents > 0 ? "Autopay charged part of the balance" : "Autopay charged — invoice paid");
      load();
    } catch (e) { toast.error(errMsg(e)); load(); }
    setBusyId("");
  };

  const doDisputePacket = async (inv) => {
    setBusyId(`${inv.id}:packet`);
    try {
      const res = await client.get(`/invoices/${inv.id}/dispute-packet`, { responseType: "blob" });
      const url = window.URL.createObjectURL(new Blob([res.data], { type: "application/zip" }));
      const a = document.createElement("a");
      a.href = url; a.download = `dispute-packet-${inv.issue_entry || inv.id.slice(0, 8)}.zip`;
      document.body.appendChild(a); a.click(); a.remove();
      window.URL.revokeObjectURL(url);
      toast.success("Dispute packet downloaded");
    } catch (e) { toast.error(errMsg(e)); }
    setBusyId("");
  };

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
        // Early-pay override for this invoice only; blank = tenant default
        ...(epPercent !== "" || epDays !== "" ? {
          early_pay_percent: parseFloat(epPercent) || 0,
          early_pay_days: parseInt(epDays) || 0,
        } : {}),
        lines: lines.filter((l) => l.name && l.amount).map((l) => ({ kind: "service", name: l.name, revenue_code: l.revenue_code, amount: parseFloat(l.amount) || 0 })),
      });
      toast.success("Invoice issued");
      setOpen(false); setClient(""); setTax(""); setJurisId(""); setEpPercent(""); setEpDays(""); setLines([{ name: "", revenue_code: "4020", amount: "" }]); load();
    } catch (e) { toast.error(errMsg(e)); }
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
  // Split children render grouped directly under their parent.
  const byId = Object.fromEntries(invoices.map(i => [i.id, i]));
  const ordered = [];
  invoices.filter(i => !i.split_parent_id).forEach(inv => {
    ordered.push({ inv, child: false });
    const kids = (inv.split_children && inv.split_children.length
      ? inv.split_children.map(id => byId[id]).filter(Boolean)
      : invoices.filter(c => c.split_parent_id === inv.id));
    kids.forEach(k => ordered.push({ inv: k, child: true }));
  });
  invoices.filter(i => i.split_parent_id && !byId[i.split_parent_id]).forEach(inv => ordered.push({ inv, child: true }));
  return <div className="animate-rise">
      <PageHeader title={t("invoices.invoices")} subtitle="Receivables — partial payments, credits & statements" testid="invoices-page">
        {isOwner && <Button data-testid="btn-new-invoice" onClick={openNewInvoice} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="me-1" /> {t("invoices.newInvoice")}</Button>}
      </PageHeader>

      <InvoiceAssistantPanel onAskTour={tour.ask} />
      {tour.element}

      <Tabs defaultValue="invoices">
        <TabsList className="bg-[#14171E] border border-[#222838]">
          <TabsTrigger value="invoices" data-testid="tab-invoices">{t("invoices.invoices")}</TabsTrigger>
          <TabsTrigger value="credits" data-testid="tab-customer-credits">{t("invoices.customerCredits")}</TabsTrigger>
          <TabsTrigger value="reminders" data-testid="tab-reminders">{t("invoices.reminders")}</TabsTrigger>
          {isOwner && <TabsTrigger value="links" data-testid="tab-payment-links">{t("invoices.paymentLinks")}</TabsTrigger>}
          {isOwner && <TabsTrigger value="settings" data-testid="tab-invoice-settings">Invoice Settings</TabsTrigger>}
        </TabsList>

        <TabsContent value="invoices" className="mt-4">
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-invoices">
              <thead>
                <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="px-5 py-3 text-start font-medium">{t("invoices.client")}</th>
                  <th className="px-5 py-3 text-start font-medium">{t("invoices.issued")}</th>
                  <th className="px-5 py-3 text-end font-medium">{t("invoices.total")}</th>
                  <th className="px-5 py-3 text-end font-medium">{t("invoices.balance")}</th>
                  <th className="px-5 py-3 text-start font-medium">{t("invoices.status")}</th>
                  {isOwner && <th className="px-5 py-3 text-end font-medium">{t("invoices.actions")}</th>}
                </tr>
              </thead>
              <tbody>
                {ordered.map(({ inv, child }) => {
                  const isSplitParent = inv.status === "split" || (inv.split_children && inv.split_children.length > 0);
                  const sends = lastSends[inv.id] || inv.sends || [];
                  const expanded = expandedId === inv.id;
                  const tl = timelines[inv.id];
                  return (
                  <Fragment key={inv.id}>
                  <tr className={`border-b border-[#171c26] hover:bg-[#161b25] ${child ? "bg-[#10131b]" : ""}`}>
                    <td className="px-5 py-3 text-gray-100">
                      <div className={`flex items-center gap-2 ${child ? "pl-6" : ""}`}>
                        <button onClick={() => toggleTimeline(inv)} title="Delivery & payment timeline" data-testid={`timeline-toggle-${inv.id}`}
                          className="inline-flex items-center justify-center w-6 h-6 rounded text-gray-500 hover:text-amber-400 hover:bg-amber-500/10">
                          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        </button>
                        <span>{inv.client_name}</span>
                        {child && inv.split_label && <span className="px-2 py-0.5 rounded-full text-[11px] border bg-violet-500/10 text-violet-300 border-violet-500/20">{inv.split_label}</span>}
                        {isSplitParent && <span className="text-[11px] text-violet-300">split into {(inv.split_children || []).length} parts</span>}
                      </div>
                      {inv.retainage_held_cents > 0 && (
                        <div className="text-[11px] text-gray-400 mt-0.5" data-testid={`retainage-line-${inv.id}`}>
                          Billed {money(inv.total_cents)} · Retainage held ({inv.retainage_pct}%): {money(inv.retainage_held_cents)} · <span className="text-amber-400">Net due: {money(inv.net_due_cents)}</span>
                        </div>
                      )}
                      {inv.autopay_last_error && (
                        <div className="mt-1 ms-8 inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] border bg-rose-500/10 text-rose-300 border-rose-500/20" data-testid={`autopay-error-${inv.id}`}>
                          <AlertTriangle size={11} /> Autopay failed: {inv.autopay_last_error}
                        </div>
                      )}
                      {sends.length > 0 && (
                        <div className="mt-1 ms-8 flex flex-wrap gap-1" data-testid={`sends-${inv.id}`}>
                          {sends.slice(-2).map((s, i) => (
                            <span key={i} className={`px-2 py-0.5 rounded-full text-[11px] border ${SEND_CLS[s.status] || ""}`} title={s.error || ""}>
                              {s.channel} {s.status}{s.status !== "sent" && s.error ? ` — ${s.error}` : ""}
                            </span>
                          ))}
                        </div>
                      )}
                    </td>
                    <td className="px-5 py-3 text-gray-400">{fmtDate(inv.created_at?.slice(0, 10))}</td>
                    <td className="px-5 py-3 text-end font-mono text-gray-200">{moneyFor(inv.total_cents, inv.currency)}</td>
                    <td className="px-5 py-3 text-end font-mono text-amber-400">{moneyFor(inv.balance_cents, inv.currency)}</td>
                    <td className="px-5 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[inv.status] || ""}`}>{inv.status}</span></td>
                    {isOwner && (
                      <td className="px-5 py-3 text-end">
                        <div className="flex justify-end gap-1 flex-wrap">
                          {inv.balance_cents > 0 && !isSplitParent && (
                            <>
                              <Button size="sm" variant="ghost" title="Send this invoice by email" disabled={busyId === `${inv.id}:email`} onClick={() => doSend(inv, "email")} data-testid={`send-email-${inv.id}`}>
                                <Send size={14} />
                              </Button>
                              <Button size="sm" variant="ghost" title="Text this invoice (SMS activates when 10DLC registration completes)" disabled={busyId === `${inv.id}:sms`} onClick={() => doSend(inv, "sms")} data-testid={`send-sms-${inv.id}`}>
                                <MessageSquare size={14} />
                              </Button>
                              <Button size="sm" variant="ghost" title="Send this invoice by WhatsApp (activates when the WhatsApp provider lands)" disabled={busyId === `${inv.id}:whatsapp`} onClick={() => doSend(inv, "whatsapp")} data-testid={`send-whatsapp-${inv.id}`}>
                                <MessageCircle size={14} />
                              </Button>
                              <Button size="sm" variant="ghost" title="Send on ALL channels at once — email + text + WhatsApp" disabled={busyId === `${inv.id}:all`} onClick={() => doSend(inv, "all")} data-testid={`send-all-${inv.id}`}>
                                <MessagesSquare size={14} />
                              </Button>
                              <Button size="sm" variant="ghost" title="Create an online pay link (card or ACH)" onClick={() => { setPayLinkFor(inv); setPayLinkMethod("card"); setPayLinkResult(null); }} data-testid={`paylink-${inv.id}`}>
                                <CreditCard size={14} />
                              </Button>
                              <Button size="sm" variant="ghost" title="Copy a 7-day payment link for this invoice" onClick={async () => { try { await quickPaymentLink(inv.id); } catch (e) { toast.error(errMsg(e)); } }} data-testid={`plink-${inv.id}`}>
                                <Link2 size={14} />
                              </Button>
                              {!(inv.paid_cents > 0) && (
                                <Button size="sm" variant="ghost" title="Split into deposit + balance or custom parts" onClick={() => openSplit(inv)} data-testid={`split-${inv.id}`}>
                                  <Scissors size={14} />
                                </Button>
                              )}
                              {inv.customer_id && (
                                <Button size="sm" variant="ghost" title="Charge the client's saved payment method (autopay)" disabled={busyId === `${inv.id}:autopay`} onClick={() => doAutopayCharge(inv)} data-testid={`autopay-charge-${inv.id}`}>
                                  <Zap size={14} />
                                </Button>
                              )}
                              <Button size="sm" className="h-7 bg-emerald-600 hover:bg-emerald-500 text-black" onClick={() => { setPayInv(inv); setPay({ amount: "", payment_method: "bank", credit_id: "" }); }} data-testid={`pay-${inv.id}`}>{t("invoices.receivePayment")}</Button>
                            </>
                          )}
                          <Button size="sm" variant="ghost" title="Download dispute evidence packet (zip)" disabled={busyId === `${inv.id}:packet`} onClick={() => doDisputePacket(inv)} data-testid={`packet-${inv.id}`}>
                            <FileDown size={14} />
                          </Button>
                        </div>
                      </td>
                    )}
                  </tr>
                  {expanded && (
                    <tr className="border-b border-[#171c26] bg-[#10131b]">
                      <td colSpan={isOwner ? 6 : 5} className="px-5 py-4">
                        <InvoiceTimeline tl={tl} fallbackSends={sends} />
                        {isOwner && <InvoiceExtras inv={inv} onChanged={load} />}
                      </td>
                    </tr>
                  )}
                  </Fragment>
                  );
                })}
                {invoices.length === 0 && <tr><td colSpan={isOwner ? 6 : 5} className="px-5 py-12 text-center text-gray-500">No invoices</td></tr>}
              </tbody>
            </table>
          </Card>
        </TabsContent>

        <TabsContent value="credits" className="mt-4">
          {isOwner && <div className="flex justify-end mb-3">
              <Button onClick={() => setCreditOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="credit-issue-open">
                <Plus size={15} className="me-1" /> {t("invoices.issueCredit")}</Button>
            </div>}
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-cust-credits">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-5 py-3 text-start font-medium">{t("invoices.customer")}</th><th className="px-5 py-3 text-start font-medium">{t("invoices.source")}</th>
                <th className="px-5 py-3 text-end font-medium">{t("invoices.amount")}</th><th className="px-5 py-3 text-end font-medium">{t("invoices.remaining")}</th>
                {isOwner && <th className="px-5 py-3 text-end font-medium">{t("invoices.actions")}</th>}</tr></thead>
              <tbody>
                {credits.map(c => <tr key={c.id} className="border-b border-[#171c26]">
                    <td className="px-5 py-3 text-gray-100">{c.client_name}</td><td className="px-5 py-3 text-gray-400 capitalize">{(c.source || "").replace("_", " ")}</td>
                    <td className="px-5 py-3 text-end font-mono text-gray-300">{money(c.amount_cents)}</td>
                    <td className="px-5 py-3 text-end font-mono text-emerald-400">{money(c.remaining_cents)}</td>
                    {isOwner && <td className="px-5 py-3 text-end">
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
          </TabsContent>
        )}
        {isOwner && (
          <TabsContent value="settings" className="mt-4">
            <div className="space-y-4">
              <InvoicePaySettings />
              <DesignShop onAskTour={tour.ask} />
              <RundownSettings onAskTour={tour.ask} />
              <ChannelStudy />
            </div>
          </TabsContent>
        )}
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
            }} className="bg-[#0F1218] border-[#222838] w-28 font-mono text-end" data-testid={`invoice-line-amount-${i}`} />
                {lines.length > 1 && <button onClick={() => setLines(lines.filter((_, x) => x !== i))} className="text-gray-600 hover:text-rose-400"><Trash2 size={14} /></button>}
              </div>)}
            <Button variant="ghost" size="sm" onClick={() => setLines([...lines, {
            name: "",
            revenue_code: "4020",
            amount: ""
          }])} className="text-amber-400 hover:bg-amber-500/10"><FilePlus size={14} className="me-1" />{t("invoices.addLine")}</Button>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("invoices.taxJurisdictionOptional")}</Label>
              <Select value={jurisId} onValueChange={pickJurisdiction}>
                <SelectTrigger data-testid="invoice-jurisdiction" className="bg-[#0F1218] border-[#222838]"><SelectValue placeholder={t("invoices.none")} /></SelectTrigger>
                <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
                  {juris.map(j => <SelectItem key={j.id} value={j.id}>{j.name} · {j.rate_pct}%</SelectItem>)}
                </SelectContent>
              </Select></div>
            <div><Label className="text-xs">{t("invoices.tax")}</Label><Input data-testid="invoice-tax" value={tax} onChange={e => setTax(e.target.value)} className="bg-[#0F1218] border-[#222838] font-mono" />
              <p className="text-[11px] text-gray-500 mt-1">Estimate only — when a jurisdiction applies, the backend calculates the final tax at issue and stores the full breakdown on the invoice.</p></div>
            <div className="grid grid-cols-2 gap-3">
              <div><Label className="text-xs">Early-pay discount % (optional)</Label><Input type="number" step="0.1" min="0" max="100" data-testid="invoice-early-pay-percent" value={epPercent} onChange={e => setEpPercent(e.target.value)} className="bg-[#0F1218] border-[#222838] font-mono" placeholder="Tenant default" /></div>
              <div><Label className="text-xs">…if paid within (days)</Label><Input type="number" step="1" min="0" max="365" data-testid="invoice-early-pay-days" value={epDays} onChange={e => setEpDays(e.target.value)} className="bg-[#0F1218] border-[#222838] font-mono" placeholder="Tenant default" /></div>
            </div>
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

      <Dialog open={!!payLinkFor} onOpenChange={(o) => !o && setPayLinkFor(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838]" data-testid="paylink-dialog">
          <DialogHeader><DialogTitle>Online pay link — {payLinkFor?.client_name}</DialogTitle></DialogHeader>
          <p className="text-sm text-gray-400">Balance due: <span className="font-mono text-amber-400">{money(payLinkFor?.balance_cents)}</span>. The client pays on a secure Stripe checkout page; the payment records itself against this invoice.</p>
          <div><Label className="text-xs">Client pays by</Label>
            <Select value={payLinkMethod} onValueChange={(v) => { setPayLinkMethod(v); setPayLinkResult(null); }}>
              <SelectTrigger data-testid="paylink-method" className="bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
                <SelectItem value="card">Card</SelectItem>
                <SelectItem value="ach">ACH / bank account (lower fees)</SelectItem>
              </SelectContent>
            </Select></div>
          {!payLinkResult && (
            <DialogFooter><Button data-testid="paylink-create" disabled={busyId === `${payLinkFor?.id}:paylink`} onClick={doPayLink} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold">{busyId === `${payLinkFor?.id}:paylink` ? "Creating…" : "Create pay link"}</Button></DialogFooter>
          )}
          {payLinkResult && (
            <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3 space-y-2" data-testid="paylink-result">
              <div className="text-xs text-gray-400">
                Checkout for {money(payLinkResult.amount_cents)}
                {payLinkResult.charge_cents !== payLinkResult.amount_cents && <> — client is charged {money(payLinkResult.charge_cents)} (includes pass-through)</>}
                {payLinkResult.platform_fee_cents > 0 && <> · platform fee {money(payLinkResult.platform_fee_cents)}</>}
              </div>
              <div className="text-sm text-gray-100 font-mono break-all">{payLinkResult.checkout_url}</div>
              <div className="flex gap-2">
                <Button size="sm" variant="outline" className="border-[#222838]" onClick={() => copyText(payLinkResult.checkout_url, "Pay link")} data-testid="paylink-copy"><Copy size={14} className="mr-1" /> Copy link</Button>
                <Button size="sm" variant="outline" className="border-[#222838]" onClick={() => window.open(payLinkResult.checkout_url, "_blank", "noopener")} data-testid="paylink-open"><ExternalLink size={14} className="mr-1" /> Open checkout</Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={!!splitFor} onOpenChange={(o) => !o && setSplitFor(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-lg" data-testid="split-dialog">
          <DialogHeader><DialogTitle>Split invoice — {splitFor?.client_name}</DialogTitle></DialogHeader>
          <p className="text-sm text-gray-400">Total <span className="font-mono text-gray-200">{money(splitFor?.total_cents)}</span> becomes separate invoices the client can pay one at a time. The original is marked “split” and can no longer be paid directly.</p>
          <div className="flex gap-2">
            <Button size="sm" variant={splitMode === "deposit" ? "default" : "outline"} className={splitMode === "deposit" ? "bg-amber-600 hover:bg-amber-500 text-black" : "border-[#222838]"} onClick={() => { setSplitMode("deposit"); applyDepositPct(depositPct); }} data-testid="split-mode-deposit">Deposit + balance</Button>
            <Button size="sm" variant={splitMode === "custom" ? "default" : "outline"} className={splitMode === "custom" ? "bg-amber-600 hover:bg-amber-500 text-black" : "border-[#222838]"} onClick={() => setSplitMode("custom")} data-testid="split-mode-custom">Custom parts</Button>
          </div>
          {splitMode === "deposit" && (
            <div className="flex items-end gap-2">
              <div><Label className="text-xs">Deposit %</Label>
                <Input type="number" min="1" max="99" step="1" value={depositPct} onChange={(e) => applyDepositPct(e.target.value)} className="bg-[#0F1218] border-[#222838] font-mono w-24" data-testid="split-deposit-pct" /></div>
              <p className="text-xs text-gray-500 pb-2">Deposit {money(Math.round((splitFor?.total_cents || 0) * (parseFloat(depositPct) || 0)) / 100)} · Balance {money((splitFor?.total_cents || 0) - Math.round((splitFor?.total_cents || 0) * (parseFloat(depositPct) || 0)) / 100)}</p>
            </div>
          )}
          <div className="space-y-2">
            {splitParts.map((p, i) => (
              <div key={i} className="flex gap-2 items-center">
                <Input placeholder="Label (e.g. Deposit)" value={p.label} disabled={splitMode === "deposit"} onChange={(e) => { const c = [...splitParts]; c[i].label = e.target.value; setSplitParts(c); }} className="bg-[#0F1218] border-[#222838] flex-1" data-testid={`split-part-label-${i}`} />
                <Input placeholder="0.00" type="number" step="0.01" min="0.01" value={p.amount} disabled={splitMode === "deposit"} onChange={(e) => { const c = [...splitParts]; c[i].amount = e.target.value; setSplitParts(c); }} className="bg-[#0F1218] border-[#222838] w-28 font-mono text-right" data-testid={`split-part-amount-${i}`} />
                <Input type="date" value={p.due_date} onChange={(e) => { const c = [...splitParts]; c[i].due_date = e.target.value; setSplitParts(c); }} className="bg-[#0F1218] border-[#222838] w-36" data-testid={`split-part-due-${i}`} />
                {splitMode === "custom" && splitParts.length > 2 && <button onClick={() => setSplitParts(splitParts.filter((_, x) => x !== i))} className="text-gray-600 hover:text-rose-400"><Trash2 size={14} /></button>}
              </div>
            ))}
            {splitMode === "custom" && <Button variant="ghost" size="sm" onClick={() => setSplitParts([...splitParts, { label: "", amount: "", due_date: "" }])} className="text-amber-400 hover:bg-amber-500/10"><FilePlus size={14} className="mr-1" />Add part</Button>}
          </div>
          <p className="text-[11px] text-gray-600">Parts must add up to the invoice total exactly.</p>
          <DialogFooter><Button data-testid="split-save" disabled={busyId === `${splitFor?.id}:split`} onClick={doSplit} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold">Split invoice</Button></DialogFooter>
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
            <th className="px-5 py-3 text-start font-medium">{t("invoices.client")}</th>
            <th className="px-5 py-3 text-start font-medium">{t("invoices.due")}</th>
            <th className="px-5 py-3 text-end font-medium">{t("invoices.daysOverdue")}</th>
            <th className="px-5 py-3 text-end font-medium">{t("invoices.balance")}</th>
            <th className="px-5 py-3 text-end font-medium">{t("invoices.reminders")}</th>
            {isOwner && <th className="px-5 py-3 text-end font-medium">{t("invoices.actions")}</th>}</tr></thead>
          <tbody>
            {overdue.map(o => <tr key={o.invoice_id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                <td className="px-5 py-3 text-gray-100">{o.client_name}</td>
                <td className="px-5 py-3 text-gray-400">{fmtDate(o.due_date)}</td>
                <td className="px-5 py-3 text-end font-mono text-rose-400">{o.days_overdue}</td>
                <td className="px-5 py-3 text-end font-mono text-amber-400">{money(o.balance_cents)}</td>
                <td className="px-5 py-3 text-end text-gray-400">{o.reminders_count}</td>
                {isOwner && <td className="px-5 py-3 text-end">
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
              <th className="px-5 py-3 text-start font-medium">{t("invoices.sent")}</th>
              <th className="px-5 py-3 text-start font-medium">{t("invoices.client")}</th>
              <th className="px-5 py-3 text-start font-medium">{t("invoices.by")}</th>
              <th className="px-5 py-3 text-start font-medium">{t("invoices.message")}</th></tr></thead>
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

function eventDetail(ev) {
  const d = ev.detail || {};
  const bits = [];
  if (d.amount_cents != null) bits.push(money(d.amount_cents));
  if (d.method) bits.push(d.method);
  if (d.channel) bits.push(d.channel);
  if (d.via) bits.push(`via ${d.via}`);
  if (d.parts != null) bits.push(`${d.parts} parts`);
  if (d.error) bits.push(d.error);
  return bits.join(" · ");
}

function InvoiceTimeline({ tl, fallbackSends }) {
  if (!tl) return <p className="text-sm text-gray-500">Loading timeline…</p>;
  const events = [...(tl.events || [])].sort((a, b) => String(a.at || "").localeCompare(String(b.at || "")));
  const sends = tl.sends && tl.sends.length ? tl.sends : (fallbackSends || []);
  return (
    <div className="grid gap-4 md:grid-cols-2" data-testid="invoice-timeline">
      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-2">Timeline — issued → sent → viewed → paid</h4>
        {events.length === 0 && <p className="text-sm text-gray-500">No events yet.</p>}
        <ol className="space-y-2">
          {events.map((ev, i) => (
            <li key={i} className="flex items-start gap-3 text-sm">
              <span className={`mt-0.5 w-2 h-2 rounded-full shrink-0 ${ev.event === "paid" ? "bg-emerald-400" : ev.event === "autopay_failed" ? "bg-rose-400" : "bg-amber-400"}`} />
              <div>
                <span className="text-gray-100 font-medium">{EVENT_LABEL[ev.event] || ev.event}</span>
                {eventDetail(ev) && <span className="text-gray-400"> — {eventDetail(ev)}</span>}
                <div className="text-xs text-gray-500">{fmtWhen(ev.at)}</div>
              </div>
            </li>
          ))}
        </ol>
      </div>
      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-2">Send log</h4>
        {(!sends || sends.length === 0) && <p className="text-sm text-gray-500">Not sent yet — use the Send or Text buttons.</p>}
        <ul className="space-y-2">
          {(sends || []).map((s, i) => (
            <li key={i} className="text-sm flex flex-wrap items-center gap-2">
              <span className={`px-2 py-0.5 rounded-full text-[11px] border ${SEND_CLS[s.status] || ""}`}>{s.channel} {s.status}</span>
              <span className="text-gray-400 text-xs">{fmtWhen(s.at)}</span>
              {s.to && <span className="text-gray-500 text-xs">to {s.to}</span>}
              {s.error && <span className="text-amber-300/80 text-xs">{s.error}</span>}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function InvoicePaySettings() {
  const [cfg, setCfg] = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.get("/invoice-payments/config").then(setCfg).catch((e) => { toast.error(errMsg(e)); setCfg({}); });
  }, []);

  if (!cfg) return <Loading />;
  const set = (k, v) => setCfg((c) => ({ ...c, [k]: v }));
  const toggleMethod = (m) => {
    const cur = cfg.fee_methods || [];
    set("fee_methods", cur.includes(m) ? cur.filter((x) => x !== m) : [...cur, m]);
  };
  const save = async () => {
    setSaving(true);
    try {
      const saved = await api.put("/invoice-payments/config", {
        fee_percent: parseFloat(cfg.fee_percent) || 0,
        fee_fixed_cents: Math.round(parseFloat(cfg.fee_fixed_cents) || 0),
        fee_methods: cfg.fee_methods || [],
        surcharge_enabled: !!cfg.surcharge_enabled,
        surcharge_percent: parseFloat(cfg.surcharge_percent) || 0,
        surcharge_fixed_cents: Math.round(parseFloat(cfg.surcharge_fixed_cents) || 0),
        sms_enabled: !!cfg.sms_enabled,
        whatsapp_enabled: !!cfg.whatsapp_enabled,
        nudges_paused: !!cfg.nudges_paused,
        from_name: cfg.from_name || "",
        early_pay_percent: parseFloat(cfg.early_pay_percent) || 0,
        early_pay_days: parseInt(cfg.early_pay_days) || 0,
      });
      setCfg(saved);
      toast.success("Invoice payment settings saved");
    } catch (e) { toast.error(errMsg(e)); }
    setSaving(false);
  };

  const IN = "bg-[#0F1218] border-[#222838]";
  return (
    <div className="space-y-4 max-w-2xl" data-testid="invoice-pay-settings">
      <Card>
        <h3 className="font-semibold text-gray-100 mb-1">Online payment fees</h3>
        <p className="text-sm text-gray-500 mb-4">The platform fee is our slice of an online payment, taken only on the methods selected. It defaults to zero — nothing is charged until you set it here.</p>
        <div className="grid grid-cols-2 gap-3">
          <div><Label className="text-xs">Platform fee %</Label>
            <Input type="number" step="0.01" min="0" max="100" value={cfg.fee_percent ?? ""} onChange={(e) => set("fee_percent", e.target.value)} className={`${IN} font-mono`} data-testid="cfg-fee-percent" /></div>
          <div><Label className="text-xs">Platform fee fixed (cents)</Label>
            <Input type="number" step="1" min="0" value={cfg.fee_fixed_cents ?? ""} onChange={(e) => set("fee_fixed_cents", e.target.value)} className={`${IN} font-mono`} data-testid="cfg-fee-fixed" /></div>
        </div>
        <div className="mt-3">
          <Label className="text-xs">Fee applies to</Label>
          <div className="flex gap-4 mt-1 text-sm text-gray-300">
            {["card", "ach"].map((m) => (
              <label key={m} className="flex items-center gap-2">
                <input type="checkbox" checked={(cfg.fee_methods || []).includes(m)} onChange={() => toggleMethod(m)} className="accent-amber-500" data-testid={`cfg-fee-method-${m}`} />
                {m === "card" ? "Card payments" : "ACH / bank payments"}
              </label>
            ))}
          </div>
        </div>
      </Card>

      <Card>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="font-semibold text-gray-100 mb-1">Pass processing costs to the client (surcharge)</h3>
            <p className="text-sm text-gray-500">When ON, the amount below is <strong className="text-gray-300">added to what the client pays</strong> on card payments, on top of the invoice total — the client, not you, covers the card processing cost. It is shown in the checkout total. Default is OFF; check your state rules before enabling.</p>
          </div>
          <Switch checked={!!cfg.surcharge_enabled} onCheckedChange={(v) => set("surcharge_enabled", v)} data-testid="cfg-surcharge-enabled" />
        </div>
        {cfg.surcharge_enabled && (
          <div className="grid grid-cols-2 gap-3 mt-4">
            <div><Label className="text-xs">Surcharge % (card only)</Label>
              <Input type="number" step="0.01" min="0" value={cfg.surcharge_percent ?? ""} onChange={(e) => set("surcharge_percent", e.target.value)} className={`${IN} font-mono`} data-testid="cfg-surcharge-percent" /></div>
            <div><Label className="text-xs">Surcharge fixed (cents)</Label>
              <Input type="number" step="1" min="0" value={cfg.surcharge_fixed_cents ?? ""} onChange={(e) => set("surcharge_fixed_cents", e.target.value)} className={`${IN} font-mono`} data-testid="cfg-surcharge-fixed" /></div>
          </div>
        )}
      </Card>

      <Card>
        <h3 className="font-semibold text-gray-100 mb-4">Delivery</h3>
        <div className="space-y-4">
          <div className="flex items-start justify-between gap-4">
            <div>
              <div className="text-sm text-gray-200 font-medium">Text (SMS) invoice delivery</div>
              <p className="text-sm text-gray-500">Texting activates when 10DLC registration completes. Until then, Text sends honestly report “not yet enabled” instead of pretending to go out.</p>
            </div>
            <Switch checked={!!cfg.sms_enabled} onCheckedChange={(v) => set("sms_enabled", v)} data-testid="cfg-sms-enabled" />
          </div>
          <div className="flex items-start justify-between gap-4 border-t border-[#222838] pt-4">
            <div>
              <div className="text-sm text-gray-200 font-medium">WhatsApp invoice delivery</div>
              <p className="text-sm text-gray-500">Sends invoice links over WhatsApp when the WhatsApp provider is connected. Until then, WhatsApp sends honestly report “not yet enabled”.</p>
            </div>
            <Switch checked={!!cfg.whatsapp_enabled} onCheckedChange={(v) => set("whatsapp_enabled", v)} data-testid="cfg-whatsapp-enabled" />
          </div>
          <div className="flex items-start justify-between gap-4 border-t border-[#222838] pt-4">
            <div>
              <div className="text-sm text-gray-200 font-medium">Pause all payment nudges</div>
              <p className="text-sm text-gray-500">Kill switch: stops every automatic reminder/nudge for all clients until you turn it back off.</p>
            </div>
            <Switch checked={!!cfg.nudges_paused} onCheckedChange={(v) => set("nudges_paused", v)} data-testid="cfg-nudges-paused" />
          </div>
          <div className="border-t border-[#222838] pt-4">
            <Label className="text-xs">Sender name on invoice emails (from_name)</Label>
            <Input value={cfg.from_name || ""} onChange={(e) => set("from_name", e.target.value)} placeholder="Your business name" className={`${IN} max-w-sm`} data-testid="cfg-from-name" />
            <p className="text-[11px] text-gray-600 mt-1">Shown as the business name in invoice emails. Blank uses your business name from Settings.</p>
          </div>
        </div>
      </Card>

      <div className="flex justify-end">
      <Card>
        <h3 className="font-semibold text-gray-100 mb-1">Early-pay discount (default)</h3>
        <p className="text-sm text-gray-500 mb-4">Offer customers a discount for paying in full within a few days. The discount comes out of YOUR invoice total — it's your money and your offer, shown on the customer's page as your offer. It does not come out of, and does not change, anything the platform charges. Defaults to off; each invoice can override it. The discount is recorded in the books as a discount, never hidden.</p>
        <div className="grid grid-cols-2 gap-3">
          <div><Label className="text-xs">Discount % (0 = off)</Label>
            <Input type="number" step="0.1" min="0" max="100" value={cfg.early_pay_percent ?? ""} onChange={(e) => set("early_pay_percent", e.target.value)} className={`${IN} font-mono`} data-testid="cfg-early-pay-percent" /></div>
          <div><Label className="text-xs">Pay in full within (days)</Label>
            <Input type="number" step="1" min="0" max="365" value={cfg.early_pay_days ?? ""} onChange={(e) => set("early_pay_days", e.target.value)} className={`${IN} font-mono`} data-testid="cfg-early-pay-days" /></div>
        </div>
      </Card>

        <Button onClick={save} disabled={saving} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="cfg-save">{saving ? "Saving…" : "Save settings"}</Button>
      </div>
    </div>
  );
}

const CHANNEL_LABEL = { email: "Email", sms: "Text (SMS)", whatsapp: "WhatsApp" };

function ratePct(r) {
  if (r == null) return "—";
  const n = Number(r);
  if (Number.isNaN(n)) return "—";
  return `${(n <= 1 ? n * 100 : n).toFixed(0)}%`;
}

function ChannelStudy() {
  const [study, setStudy] = useState(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    api.get("/invoice-payments/channel-study")
      .then(setStudy)
      .catch(() => { setFailed(true); setStudy({ aggregate: [], clients: [] }); });
  }, []);

  if (!study) return <Loading />;
  const aggregate = study.aggregate || [];
  const clients = study.clients || [];
  return (
    <div className="max-w-2xl" data-testid="channel-study">
      <Card>
        <h3 className="font-semibold text-gray-100 mb-1">Channel study — what actually gets a response</h3>
        <p className="text-sm text-gray-500 mb-4">Which delivery channel each client responds to, from real sends, views, and payments. Use it to pick the channel (or All channels) that works for each client.</p>
        {failed && <p className="text-sm text-amber-300 mb-3">Couldn't load the channel study — the rest of this page still works.</p>}
        <table className="w-full text-sm" data-testid="channel-study-aggregate">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="py-2 text-left font-medium">Channel</th>
            <th className="py-2 text-right font-medium">Sends</th>
            <th className="py-2 text-right font-medium">Views</th>
            <th className="py-2 text-right font-medium">Payments</th>
            <th className="py-2 text-right font-medium">Response rate</th>
          </tr></thead>
          <tbody>
            {aggregate.map((a) => (
              <tr key={a.channel} className="border-b border-[#171c26]">
                <td className="py-2 text-gray-100">{CHANNEL_LABEL[a.channel] || a.channel}</td>
                <td className="py-2 text-right font-mono text-gray-300">{a.sends ?? 0}</td>
                <td className="py-2 text-right font-mono text-gray-300">{a.views ?? 0}</td>
                <td className="py-2 text-right font-mono text-gray-300">{a.payments ?? 0}</td>
                <td className="py-2 text-right font-mono text-emerald-400">{ratePct(a.response_rate)}</td>
              </tr>
            ))}
            {aggregate.length === 0 && <tr><td colSpan={5} className="py-6 text-center text-gray-500">No sends recorded yet — send an invoice and the study starts filling in.</td></tr>}
          </tbody>
        </table>

        {clients.length > 0 && (
          <>
            <h4 className="text-sm font-semibold text-gray-200 mt-5 mb-2">Best channel per client</h4>
            <table className="w-full text-sm" data-testid="channel-study-clients">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="py-2 text-left font-medium">Client</th>
                <th className="py-2 text-left font-medium">Best channel</th>
                <th className="py-2 text-left font-medium">Per-channel (sends / views / payments)</th>
              </tr></thead>
              <tbody>
                {clients.map((c) => (
                  <tr key={c.client_name} className="border-b border-[#171c26]">
                    <td className="py-2 text-gray-100">{c.client_name}</td>
                    <td className="py-2"><span className="px-2 py-0.5 rounded-full text-xs border bg-sky-500/10 text-sky-400 border-sky-500/20">{CHANNEL_LABEL[c.best_channel] || c.best_channel || "—"}</span></td>
                    <td className="py-2 text-gray-400 text-xs">
                      {Object.entries(c.channels || {}).map(([ch, s]) => (
                        <span key={ch} className="mr-3">{CHANNEL_LABEL[ch] || ch}: {s.sends ?? 0}/{s.views ?? 0}/{s.payments ?? 0}</span>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </Card>
    </div>
  );
}

/**
 * Invoice Assistant — login greeting rundown + in-app rundown notifications.
 * Rundown is computed live by GET /invoice-assistant/rundown; notifications
 * come from the in-app store (GET /invoice-assistant/notifications).
 */
function InvoiceAssistantPanel({ onAskTour }) {
  const [rundown, setRundown] = useState(null);
  const [notifs, setNotifs] = useState([]);
  const [unread, setUnread] = useState(0);
  // Proactive full-walkthrough offer: shown until the user completes the
  // walkthrough, then suppressed server-side for a few weeks. The
  // on-demand "Show me how" button below is ALWAYS available.
  const [offerFull, setOfferFull] = useState(false);
  const [benchmark, setBenchmark] = useState(null);
  const [showFreeHelp, setShowFreeHelp] = useState(false);

  const loadNotifs = () => {
    api.get("/invoice-assistant/notifications")
      .then((r) => { setNotifs(r.notifications || []); setUnread(r.unread || 0); })
      .catch(() => {});
  };
  useEffect(() => {
    api.get("/invoice-assistant/rundown").then(setRundown).catch(() => setRundown(null));
    api.get("/invoice-assistant/settings")
      .then((s) => setOfferFull(!!s.offer_full_walkthrough))
      .catch(() => {});
    api.get("/benchmarks/trade-line")
      .then((b) => { if (b && b.available) setBenchmark(b); })
      .catch(() => {});
    loadNotifs();
  }, []);

  const markRead = async (n) => {
    if (n.read) return;
    try { await api.post(`/invoice-assistant/notifications/${n.id}/read`); loadNotifs(); }
    catch { /* non-fatal */ }
  };

  const downloadPacket = async (invoiceId) => {
    try {
      const res = await client.get(`/invoices/${invoiceId}/dispute-packet`, { responseType: "blob" });
      const url = window.URL.createObjectURL(new Blob([res.data]));
      const a = document.createElement("a");
      a.href = url; a.download = `dispute-packet-${invoiceId}.zip`;
      document.body.appendChild(a); a.click(); a.remove();
      window.URL.revokeObjectURL(url);
    } catch (e) { toast.error(errMsg(e)); }
  };

  if (!rundown) return null;
  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const mascotMood = rundown.paid_today_cents > 0 ? "happy"
    : rundown.issued_today > 0 ? "eating" : "idle";
  const stats = [
    ["Open", rundown.open],
    ["Sent & waiting", rundown.sent_waiting],
    ["Pending (not sent)", rundown.pending],
    ["Viewed", rundown.viewed],
    ["Overdue", rundown.overdue],
    ["Issued today", rundown.issued_today],
  ];
  return (
    <Card className="mb-4 p-5" data-testid="invoice-assistant-panel">
      <div className="flex flex-wrap items-start gap-3">
        <InvoiceAssistantMascot mood={mascotMood} theme="dark" size={92} className="shrink-0 -my-1" />
        <div className="flex items-center gap-2 text-amber-400">
          <Sparkles size={18} />
          <span className="font-display font-semibold text-gray-100">Invoice Assistant</span>
        </div>
        <div className="ml-auto text-right">
          <div className="text-xs text-gray-500">Outstanding</div>
          <div className="font-mono text-xl text-amber-400" data-testid="assistant-outstanding">{money(rundown.outstanding_cents)}</div>
          <div className="text-xs text-emerald-400">{money(rundown.paid_today_cents)} paid today</div>
        </div>
      </div>
      <p className="mt-1 text-sm text-gray-300" data-testid="assistant-rundown-text">
        {greeting} — {rundown.text}
      </p>
      {benchmark && (
        <p className="mt-1 flex items-center gap-2 text-sm text-sky-300" data-testid="assistant-benchmark">
          <TrendingUp size={14} className="shrink-0" />
          Trade check: {benchmark.line}
        </p>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        {stats.map(([label, v]) => (
          <span key={label} className="rounded-full border border-[#222838] bg-[#14171E] px-3 py-1 text-xs text-gray-300">
            {label}: <span className="font-mono font-semibold text-gray-100">{v}</span>
          </span>
        ))}
      </div>

      {(rundown.follow_up?.length > 0 || rundown.autopay_alerts?.length > 0 ||
        rundown.installments?.length > 0 || rundown.money_flags?.length > 0 ||
        rundown.disputes?.length > 0 || rundown.bank_paid_cents_30d > 0 ||
        rundown.duplicates_flagged > 0) && (
        <div className="mt-4 space-y-1.5 border-t border-[#222838] pt-3 text-sm" data-testid="assistant-helps">
          {rundown.bank_paid_cents_30d > 0 && (
            <p className="text-emerald-300" data-testid="assistant-fee-savings" title={rundown.fee_savings_basis}>
              Bank payments saved you an estimated {money(rundown.est_fee_savings_cents_30d)} in fees over the last 30 days
              ({money(rundown.bank_paid_cents_30d)} collected by bank).
            </p>
          )}
          {rundown.duplicates_flagged > 0 && (
            <p className="flex items-center gap-2 text-gray-300" data-testid="assistant-duplicates">
              <Copy size={14} className="shrink-0 text-violet-300" />
              {rundown.duplicates_flagged} possible duplicate transaction{rundown.duplicates_flagged === 1 ? "" : "s"} flagged —
              <Link to={rundown.duplicates_link || "/cleanup"} className="underline text-sky-300 hover:text-sky-200">review in Cleanup (DataClean)</Link>
            </p>
          )}
          {(rundown.follow_up || []).map((f) => (
            <p key={f.invoice_id} className="flex items-center gap-2 text-gray-300" data-testid="assistant-follow-up">
              <PhoneCall size={14} className="shrink-0 text-sky-300" />
              {f.client_name} opened their invoice {f.views} times and it's still unpaid ({money(f.balance_cents)} due) — good time to call.
            </p>
          ))}
          {(rundown.autopay_alerts || []).map((a) => (
            <p key={a.invoice_id} className="flex items-center gap-2 text-rose-300" data-testid="assistant-autopay-alert">
              <AlertTriangle size={14} className="shrink-0" />
              Autopay failed for {a.client_name}: {a.error}
            </p>
          ))}
          {(rundown.installments || []).map((p) => (
            <p key={p.invoice_id} className={`flex items-center gap-2 ${p.overdue ? "text-rose-300" : "text-gray-300"}`} data-testid="assistant-installment">
              <Eye size={14} className="shrink-0 text-gray-500" />
              Installment {p.overdue ? "overdue" : "due"}: {p.label} — {p.client_name}, {money(p.balance_cents)}
              {p.due_date ? `, due ${fmtDate(p.due_date)}` : ""}.
            </p>
          ))}
          {(rundown.money_flags || []).map((f, i) => (
            <p key={`${f.invoice_id}-${i}`} className="flex items-center gap-2 text-amber-300" data-testid="assistant-money-flag">
              <AlertTriangle size={14} className="shrink-0" />
              Payment failed for {f.client_name}{f.detail ? `: ${f.detail}` : ""}. You can see the state here — the help door on their invoice page routes them to support.
            </p>
          ))}
          {(rundown.disputes || []).map((d) => (
            <p key={d.invoice_id} className="flex flex-wrap items-center gap-2 text-rose-300" data-testid="assistant-dispute">
              <AlertTriangle size={14} className="shrink-0" />
              Dispute open for {d.client_name}{d.reason ? ` (${d.reason})` : ""} — your evidence packet is ready.
              <Button size="sm" variant="outline" className="border-[#222838]" onClick={() => downloadPacket(d.invoice_id)} data-testid="assistant-dispute-packet">
                <FileDown size={13} className="mr-1" /> Download packet
              </Button>
            </p>
          ))}
        </div>
      )}

      <div className="mt-4 border-t border-[#222838] pt-3" data-testid="assistant-notifications">
        <div className="flex items-center gap-2 text-sm font-medium text-gray-200">
          <Bell size={15} className="text-gray-400" /> Rundown notifications
          {unread > 0 && (
            <span className="rounded-full bg-amber-500 px-2 py-0.5 text-[11px] font-semibold text-black" data-testid="assistant-unread-badge">
              {unread} new
            </span>
          )}
        </div>
        {notifs.length === 0 && (
          <p className="mt-1 text-xs text-gray-500">
            No rundown notifications yet. Scheduled rundowns are off by default — turn them on under Invoice Settings.
          </p>
        )}
        <div className="mt-3 flex flex-wrap items-center gap-2" data-testid="assistant-tours">
          <button type="button" onClick={() => onAskTour && onAskTour()}
            data-testid="tour-ask"
            className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/40 px-3 py-1.5 text-xs font-medium text-amber-200 hover:bg-amber-500/10">
            <GraduationCap size={13} /> Show me how
          </button>
          {offerFull && (
            <span className="text-xs text-gray-500" data-testid="assistant-walkthrough-offer">
              New here? Ask me and I'll walk you through the whole thing, step by step.
            </span>
          )}
          <button type="button" onClick={() => setShowFreeHelp((s) => !s)}
            data-testid="free-help-toggle" aria-expanded={showFreeHelp}
            className="inline-flex items-center gap-1.5 rounded-full border border-[#2A3040] px-3 py-1.5 text-xs font-medium text-gray-300 hover:border-sky-500/50 hover:text-sky-200">
            <LifeBuoy size={13} /> Free help &amp; learning
          </button>
        </div>
        {showFreeHelp && (
          <div className="mt-3 rounded-lg border border-[#222838] bg-[#10131b] p-4">
            <FreeHelpPanel tone="dark" />
          </div>
        )}
        <div className="mt-2 space-y-1">
          {notifs.slice(0, 5).map((n) => (
            <button key={n.id} type="button" onClick={() => markRead(n)}
              className="flex w-full items-start gap-2 rounded-lg border border-[#222838] bg-[#14171E] p-3 text-left hover:border-[#333c52]"
              data-testid={`assistant-notification-${n.id}`}>
              <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${n.read ? "bg-gray-600" : "bg-amber-400"}`} />
              <span>
                <span className="block text-sm font-medium text-gray-100">{n.title}</span>
                <span className="block text-xs text-gray-400">{n.body}</span>
                <span className="block text-[11px] text-gray-600">{fmtWhen(n.created_at)}</span>
              </span>
            </button>
          ))}
        </div>
      </div>
    </Card>
  );
}

/**
 * Rundown frequency settings — off / once daily / twice daily, with times.
 * Persisted per user via PUT /invoice-assistant/settings. In-app only.
 */
function RundownSettings({ onAskTour }) {
  const [freq, setFreq] = useState("off");
  const [times, setTimes] = useState(["09:00", "16:00"]);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api.get("/invoice-assistant/settings")
      .then((s) => {
        setFreq(s.frequency || "off");
        if (s.times && s.times.length) {
          setTimes([s.times[0] || "09:00", s.times[1] || "16:00"]);
        }
        setLoaded(true);
      })
      .catch(() => setLoaded(true));
  }, []);

  const save = async () => {
    const n = freq === "daily" ? 1 : freq === "twice_daily" ? 2 : 0;
    setBusy(true);
    try {
      const s = await api.put("/invoice-assistant/settings", {
        frequency: freq, times: times.slice(0, n),
      });
      setFreq(s.frequency);
      toast.success("Rundown settings saved");
    } catch (e) { toast.error(errMsg(e)); }
    setBusy(false);
  };

  const runDue = async () => {
    setBusy(true);
    try {
      const r = await api.post("/invoice-assistant/run-due", {});
      toast.success(r.created && r.created.length
        ? `Rundown notification created (${r.created.length})`
        : "No rundown due right now");
    } catch (e) { toast.error(errMsg(e)); }
    setBusy(false);
  };

  if (!loaded) return <Loading />;
  const slots = freq === "daily" ? 1 : freq === "twice_daily" ? 2 : 0;
  return (
    <Card className="p-5" data-testid="rundown-settings">
      <div className="flex items-center gap-2">
        <Bell size={16} className="text-amber-400" />
        <h3 className="font-display font-semibold text-gray-100">Invoice rundown notifications</h3>
        <button type="button" onClick={() => onAskTour && onAskTour("rundown-frequency")}
          data-testid="tour-ask-rundown"
          className="ml-auto inline-flex items-center gap-1 rounded-full border border-[#2A3040] px-2.5 py-1 text-xs text-gray-300 hover:border-amber-500/50 hover:text-amber-200">
          <GraduationCap size={12} /> Show me how
        </button>
      </div>
      <p className="mt-1 text-sm text-gray-400">
        Get this rundown as a notification inside the app — free, no email or texts.
        Off by default so nothing spams you; the assistant still greets you here on the Invoices page.
      </p>
      <div className="mt-3 flex flex-wrap items-end gap-3">
        <div>
          <Label className="text-xs">How often</Label>
          <Select value={freq} onValueChange={setFreq}>
            <SelectTrigger className="w-[200px] bg-[#0F1218] border-[#222838]" data-testid="rundown-frequency">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="off">Off</SelectItem>
              <SelectItem value="daily">Once a day</SelectItem>
              <SelectItem value="twice_daily">Twice a day</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {Array.from({ length: slots }).map((_, i) => (
          <div key={i}>
            <Label className="text-xs">{slots > 1 ? `Rundown ${i + 1} at` : "At"}</Label>
            <Input type="time" value={times[i] || ""} data-testid={`rundown-time-${i}`}
              onChange={(e) => setTimes((t) => { const c = [...t]; c[i] = e.target.value; return c; })}
              className="w-[140px] bg-[#0F1218] border-[#222838]" />
          </div>
        ))}
        <Button onClick={save} disabled={busy} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="rundown-save">
          Save
        </Button>
        <Button variant="outline" onClick={runDue} disabled={busy} className="border-[#222838]" data-testid="rundown-run-due">
          <CheckCheck size={14} className="mr-1" /> Check for due rundowns now
        </Button>
      </div>
    </Card>
  );
}

/**
 * Invoice Design Shop — the business designs its invoice face on top of the
 * existing whitelabel branding (GET/PATCH /whitelabel/branding). Logo +
 * banner are URL-based (the existing mechanism); file upload is a seam —
 * there is no asset storage in the app yet. Live preview updates as you type.
 */
const DESIGN_LAYOUTS = [
  ["classic", "Classic — logo and name up top, left-aligned"],
  ["modern", "Modern — centered logo and title"],
  ["bold", "Bold — full-width accent header band"],
];

function DesignShop({ onAskTour }) {
  const [f, setF] = useState({
    firm_name: "", logo_url: "", accent_color: "", support_email: "",
    tagline: "", layout: "classic", banner_url: "",
    default_message: "", footer_note: "",
  });
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF((s) => ({ ...s, [k]: e.target.value }));

  useEffect(() => {
    api.get("/whitelabel/branding")
      .then((r) => {
        setF((s) => ({
          ...s,
          firm_name: r.firm_name || "", logo_url: r.logo_url || "",
          accent_color: r.accent_color || "", support_email: r.support_email || "",
          tagline: r.tagline || "", layout: r.layout || "classic",
          banner_url: r.banner_url || "",
          default_message: r.default_message || "",
          footer_note: r.footer_note || "",
        }));
        setLoaded(true);
      })
      .catch(() => setLoaded(true));
  }, []);

  const save = async () => {
    setBusy(true);
    try {
      await api.patch("/whitelabel/branding", { ...f });
      toast.success("Invoice design saved — your face is on the client page");
    } catch (e) { toast.error(errMsg(e)); }
    setBusy(false);
  };

  if (!loaded) return <Loading />;
  const accent = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(f.accent_color) ? f.accent_color : "#92400E";
  const inputCls = "bg-[#0F1218] border-[#222838]";
  return (
    <Card className="p-5" data-testid="design-shop">
      <div className="flex items-center gap-2">
        <h3 className="font-display font-semibold text-gray-100">Invoice Design Shop</h3>
        <button type="button" onClick={() => onAskTour && onAskTour("design-shop")}
          data-testid="tour-ask-design"
          className="ml-auto inline-flex items-center gap-1 rounded-full border border-[#2A3040] px-2.5 py-1 text-xs text-gray-300 hover:border-amber-500/50 hover:text-amber-200">
          <GraduationCap size={12} /> Show me how
        </button>
      </div>
      <p className="mt-1 text-sm text-gray-400">
        Your invoice, your look. Design the page your customers see when they open an invoice —
        it updates on their client page and in the invoice email. Logo and photo are web links
        (paste a URL); uploading files comes later.
      </p>
      <div className="mt-4 grid gap-5 md:grid-cols-2">
        <div className="space-y-3">
          <div><Label className="text-xs">Business name</Label>
            <Input value={f.firm_name} onChange={set("firm_name")} className={inputCls} data-testid="design-firm-name" placeholder="Proof Plumbing LLC" /></div>
          <div><Label className="text-xs">Tagline</Label>
            <Input value={f.tagline} onChange={set("tagline")} className={inputCls} data-testid="design-tagline" placeholder="Done right, first time" /></div>
          <div><Label className="text-xs">Logo URL</Label>
            <Input value={f.logo_url} onChange={set("logo_url")} className={inputCls} data-testid="design-logo-url" placeholder="https://…/logo.png" /></div>
          <div><Label className="text-xs">Company photo / banner URL</Label>
            <Input value={f.banner_url} onChange={set("banner_url")} className={inputCls} data-testid="design-banner-url" placeholder="https://…/your-crew.jpg" /></div>
          <div className="flex items-end gap-2">
            <div className="flex-1"><Label className="text-xs">Accent color</Label>
              <Input value={f.accent_color} onChange={set("accent_color")} className={`${inputCls} font-mono`} data-testid="design-accent" placeholder="#0f766e" /></div>
            <input type="color" aria-label="Pick accent color"
              value={/^#[0-9a-fA-F]{6}$/.test(f.accent_color) ? f.accent_color : "#0f766e"}
              onChange={(e) => setF((s) => ({ ...s, accent_color: e.target.value }))}
              className="h-9 w-12 cursor-pointer rounded border border-[#222838] bg-[#0F1218]"
              data-testid="design-accent-picker" />
          </div>
          <div><Label className="text-xs">Layout</Label>
            <Select value={f.layout} onValueChange={(v) => setF((s) => ({ ...s, layout: v }))}>
              <SelectTrigger className="bg-[#0F1218] border-[#222838]" data-testid="design-layout"><SelectValue /></SelectTrigger>
              <SelectContent>
                {DESIGN_LAYOUTS.map(([v, label]) => (
                  <SelectItem key={v} value={v}>{label}</SelectItem>
                ))}
              </SelectContent>
            </Select></div>
          <div><Label className="text-xs">Default invoice message</Label>
            <textarea value={f.default_message} onChange={set("default_message")} rows={2}
              className="w-full rounded-md bg-[#0F1218] border border-[#222838] p-2 text-sm text-gray-100"
              data-testid="design-default-message" placeholder="Thanks for your business! Payment is due on receipt." /></div>
          <div><Label className="text-xs">Footer note (PO / disclaimer / job notes)</Label>
            <Input value={f.footer_note} onChange={set("footer_note")} className={inputCls} data-testid="design-footer-note" placeholder="PO required on all jobs over $500." /></div>
          <Button onClick={save} disabled={busy} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="design-save">
            Save design
          </Button>
        </div>

        <div>
          <Label className="text-xs">Live preview — your client page</Label>
          <div className="mt-1 overflow-hidden rounded-xl border border-[#222838] bg-[#0B0D10]" data-testid="design-preview" data-layout={f.layout}>
            {f.banner_url && <img src={f.banner_url} alt="" className="h-16 w-full object-cover" />}
            <div className="p-4">
              {f.layout === "bold" ? (
                <div className="rounded-lg p-3 text-white" style={{ background: accent }}>
                  <div className="flex items-center gap-2">
                    {f.logo_url && <img src={f.logo_url} alt="" className="max-h-8 max-w-[120px] rounded bg-white object-contain p-0.5" />}
                    <span className="font-display font-bold">{f.firm_name || "Your Business"}</span>
                  </div>
                  {f.tagline && <div className="text-xs text-white/85">{f.tagline}</div>}
                  <div className="mt-1 text-sm font-semibold">Your invoices</div>
                </div>
              ) : (
                <div className={f.layout === "modern" ? "flex flex-col items-center text-center" : ""}>
                  {f.logo_url && <img src={f.logo_url} alt="" className="max-h-10 max-w-[150px] rounded bg-white object-contain p-0.5" />}
                  <div className="font-display text-lg font-bold text-gray-100">{f.firm_name || "Your Business"}</div>
                  {f.tagline && <div className="text-xs text-gray-500">{f.tagline}</div>}
                  <div className="mt-1 text-sm font-semibold" style={{ color: accent }}>Your invoices</div>
                </div>
              )}
              <div className="mt-3 rounded-lg bg-[#14171E] border border-[#222838] p-3">
                <div className="flex justify-between text-sm">
                  <span className="text-gray-200">Invoice #1042</span>
                  <span className="font-mono" style={{ color: accent }}>$1,500.00 due</span>
                </div>
                {f.default_message && <p className="mt-1 text-xs italic text-gray-400">{f.default_message}</p>}
                <button type="button" className="mt-2 rounded-md px-3 py-1.5 text-sm font-semibold text-white" style={{ background: accent }}>
                  Pay invoice
                </button>
              </div>
              {f.footer_note && <p className="mt-2 text-center text-[11px] text-gray-500">{f.footer_note}</p>}
            </div>
          </div>
          <p className="mt-1 text-[11px] text-gray-600">Preview uses your unsaved changes. Save to put it on the live client page.</p>
        </div>
      </div>
    </Card>
  );
}

/** Authed photo thumbnail — same blob-fetch pattern as Receipts. */
function PhotoThumb({ invoiceId, photoId, alt }) {
  const [url, setUrl] = useState(null);
  useEffect(() => {
    let live = true;
    api.get(`/invoices/${invoiceId}/photos/${photoId}`, { responseType: "blob" })
      .then((b) => { if (live) setUrl(URL.createObjectURL(b)); })
      .catch(() => {});
    return () => { live = false; };
  }, [invoiceId, photoId]);
  if (!url) return <span className="flex h-16 w-20 items-center justify-center rounded bg-[#14171E] text-xs text-gray-600">…</span>;
  return <img src={url} alt={alt || ""} className="h-16 w-20 rounded object-cover" />;
}

/**
 * Per-invoice extras (owner-approved 2026-11-14): the agreement text the
 * client signs before paying, job photos that ride to the client page,
 * and the early-pay offer on this invoice.
 */
function InvoiceExtras({ inv, onChanged }) {
  const [agreement, setAgreement] = useState(inv.agreement_text || "");
  const [savingAgr, setSavingAgr] = useState(false);
  const [caption, setCaption] = useState("");
  const [kind, setKind] = useState("job");
  const [uploading, setUploading] = useState(false);
  const photos = inv.photos || [];
  const sig = inv.signature || null;

  const saveAgreement = async () => {
    setSavingAgr(true);
    try {
      await api.put(`/invoices/${inv.id}/agreement`, { agreement_text: agreement });
      toast.success("Agreement saved — the client signs it before paying");
      onChanged && onChanged();
    } catch (e) { toast.error(errMsg(e)); }
    setSavingAgr(false);
  };

  const upload = async (e) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    setUploading(true);
    try {
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        fd.append("caption", caption);
        fd.append("kind", kind);
        await api.post(`/invoices/${inv.id}/photos`, fd);
      }
      toast.success("Photo(s) added — they show on the client's page");
      setCaption("");
      onChanged && onChanged();
    } catch (err) { toast.error(errMsg(err)); }
    setUploading(false);
    e.target.value = "";
  };

  const removePhoto = async (pid) => {
    try {
      await api.del(`/invoices/${inv.id}/photos/${pid}`);
      onChanged && onChanged();
    } catch (e) { toast.error(errMsg(e)); }
  };

  return (
    <div className="mt-4 grid gap-4 md:grid-cols-2" data-testid={`invoice-extras-${inv.id}`}>
      <div className="rounded-lg border border-[#222838] bg-[#0F1218] p-3">
        <div className="flex items-center gap-2 text-sm font-medium text-gray-200">
          <PenLine size={14} className="text-amber-400" /> Agreement & signature
        </div>
        {sig ? (
          <p className="mt-2 text-sm text-emerald-300" data-testid={`agreement-signed-${inv.id}`}>
            Signed by {sig.signer_name} on {String(sig.signed_at || "").slice(0, 10)} — audit trail recorded (name, time, IP, device).
          </p>
        ) : (
          <>
            <textarea value={agreement} onChange={(e) => setAgreement(e.target.value)} rows={3}
              placeholder="Your agreement terms — the client reviews and signs this on their page before paying. Leave empty for no agreement."
              className="mt-2 w-full rounded-md border border-[#222838] bg-[#0B0D10] p-2 text-sm text-gray-100"
              data-testid={`agreement-text-${inv.id}`} />
            <Button size="sm" onClick={saveAgreement} disabled={savingAgr}
              className="mt-2 bg-amber-600 hover:bg-amber-500 text-black font-semibold"
              data-testid={`agreement-save-${inv.id}`}>
              {savingAgr ? "Saving…" : "Save agreement"}
            </Button>
            <p className="mt-1 text-[11px] text-gray-600">Simple e-sign with an audit trail, built in — no outside e-sign service.</p>
          </>
        )}
        {inv.early_pay && inv.early_pay.percent > 0 && (
          <p className="mt-2 text-xs text-emerald-300" data-testid={`early-pay-note-${inv.id}`}>
            Early-pay: {inv.early_pay.percent}% off if paid in full within {inv.early_pay.days} days.
            {inv.discount_applied_cents > 0 && <> Discount given: {money(inv.discount_applied_cents)}.</>}
          </p>
        )}
      </div>

      <div className="rounded-lg border border-[#222838] bg-[#0F1218] p-3">
        <div className="flex items-center gap-2 text-sm font-medium text-gray-200">
          <Camera size={14} className="text-amber-400" /> Job photos
          <span className="text-xs text-gray-500">({photos.length}/12 — shown on the client's page)</span>
        </div>
        {photos.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {photos.map((p) => (
              <div key={p.id} className="relative" data-testid={`photo-${p.id}`}>
                <PhotoThumb invoiceId={inv.id} photoId={p.id} alt={p.caption} />
                <span className="absolute left-1 top-1 rounded-full bg-black/70 px-1.5 py-0.5 text-[10px] capitalize text-white">{p.kind}</span>
                <button type="button" onClick={() => removePhoto(p.id)} title="Remove photo"
                  className="absolute right-1 top-1 rounded-full bg-black/70 p-1 text-white hover:bg-rose-900">
                  <Trash2 size={11} />
                </button>
                {p.caption && <div className="mt-0.5 max-w-[80px] truncate text-[10px] text-gray-500">{p.caption}</div>}
              </div>
            ))}
          </div>
        )}
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <div>
            <Label className="text-xs">Kind</Label>
            <Select value={kind} onValueChange={setKind}>
              <SelectTrigger className="w-28 bg-[#0B0D10] border-[#222838]" data-testid={`photo-kind-${inv.id}`}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="job">Job</SelectItem>
                <SelectItem value="before">Before</SelectItem>
                <SelectItem value="after">After</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="min-w-[140px] flex-1">
            <Label className="text-xs">Caption</Label>
            <Input value={caption} onChange={(e) => setCaption(e.target.value)}
              className="bg-[#0B0D10] border-[#222838]" placeholder="e.g. New panel installed"
              data-testid={`photo-caption-${inv.id}`} />
          </div>
          <label className={`inline-flex cursor-pointer items-center gap-1.5 rounded-md bg-amber-600 px-3 py-2 text-sm font-semibold text-black hover:bg-amber-500 ${uploading ? "opacity-50" : ""}`}>
            <Camera size={14} /> {uploading ? "Adding…" : "Add photos"}
            <input type="file" accept="image/jpeg,image/png,image/webp" multiple
              className="hidden" onChange={upload} data-testid={`photo-upload-${inv.id}`} />
          </label>
        </div>
        <p className="mt-1 text-[11px] text-gray-600">JPG, PNG or WEBP, up to 8 MB each.</p>
      </div>
    </div>
  );
}
