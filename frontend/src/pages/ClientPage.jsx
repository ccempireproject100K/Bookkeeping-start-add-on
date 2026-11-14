import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { getStoredClientTheme, storeClientTheme, nextClientTheme } from "@/lib/theme";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import InvoiceAssistantMascot from "@/components/InvoiceAssistantMascot";
import FreeHelpPanel from "@/components/FreeHelpPanel";
import { ReceiptText, Eye, EyeOff, CreditCard, Landmark, CheckCircle2, Zap, Sun, Moon, PenLine, Camera, RotateCw } from "lucide-react";
import { filterHistoryByDate, sortHistory, shouldMask, faceMoney } from "@/lib/historyCards";

const API_BASE = `${process.env.REACT_APP_BACKEND_URL}/api`;

/**
 * PUBLIC client page — ALL of one client's invoices in one place, no login.
 * Bookkeeping InvoiceEase module branding. Wired as a standalone route
 * OUTSIDE <Protected>:
 *   <Route path="/c/:token" element={<ClientPage />} />
 * Token is issued by the backend (invoice emails / pay links point here).
 *
 * Light/dark: dark is the default look; the toggle in the header switches
 * to a clean white light mode and the visitor's choice is persisted
 * (localStorage, see lib/theme.js).
 */

const STATUS_CLS_DARK = {
  issued: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  partial: "bg-sky-500/10 text-sky-400 border-sky-500/20",
  paid: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  split: "bg-violet-500/10 text-violet-400 border-violet-500/20",
};

const STATUS_CLS_LIGHT = {
  issued: "bg-amber-500/10 text-amber-700 border-amber-500/30",
  partial: "bg-sky-500/10 text-sky-700 border-sky-500/30",
  paid: "bg-emerald-500/10 text-emerald-700 border-emerald-500/30",
  split: "bg-violet-500/10 text-violet-700 border-violet-500/30",
};

export default function ClientPage() {
  const { token = "" } = useParams();
  const [theme, setTheme] = useState(() => getStoredClientTheme(window.localStorage));
  const light = theme === "light";
  const toggleTheme = () => {
    const next = nextClientTheme(theme);
    setTheme(next);
    storeClientTheme(window.localStorage, next);
  };

  // Theme palette — every surface/text color routes through here so light
  // mode has no leftover dark boxes.
  const C = {
    root: light ? "min-h-screen bg-[#F4F5F7] text-gray-900" : "app-bg min-h-screen",
    card: light
      ? "bg-white border border-[#E3E6ED] rounded-xl shadow-[0_1px_3px_rgba(16,24,40,0.06)]"
      : "card-surface",
    panel: light ? "bg-[#F8F9FB] border-[#E3E6ED]" : "bg-[#0F1218] border-[#222838]",
    input: light ? "bg-white border-[#D3D8E2] text-gray-900" : "bg-[#0F1218] border-[#222838]",
    area: light
      ? "bg-white border-[#D3D8E2] text-gray-900 placeholder:text-gray-400"
      : "bg-[#14171E] border-[#222838] text-gray-100",
    t1: light ? "text-gray-900" : "text-gray-100",
    t2: light ? "text-gray-700" : "text-gray-200",
    muted: "text-gray-500",
    faint: light ? "text-gray-400" : "text-gray-600",
    label: light ? "text-xs text-gray-600" : "text-xs",
    rowBorder: light ? "border-[#EDF0F4]" : "border-[#171c26]",
    headBorder: light ? "border-[#E3E6ED]" : "border-[#222838]",
    optIdle: light
      ? "border-[#E3E6ED] bg-white hover:border-[#B7BECB]"
      : "border-[#222838] bg-[#14171E] hover:border-[#333c52]",
    optSel: light ? "border-amber-500/70 bg-amber-50" : "border-amber-500/60 bg-amber-500/5",
    amber: light ? "text-amber-600" : "text-amber-400",
    emerald: light ? "text-emerald-600" : "text-emerald-400",
    emeraldNote: light ? "text-emerald-700" : "text-emerald-300",
    skyLink: light ? "text-sky-700 hover:text-sky-800" : "text-sky-300 hover:text-sky-200",
    skyNote: light ? "text-sky-700" : "text-sky-300",
    splitBadge: light
      ? "bg-violet-500/10 text-violet-700 border-violet-500/30"
      : "bg-violet-500/10 text-violet-300 border-violet-500/20",
    paidBanner: light
      ? "border-emerald-500/40 bg-emerald-50 text-emerald-800"
      : "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
    autopayBanner: light
      ? "border-sky-500/40 bg-sky-50 text-sky-800"
      : "border-sky-500/30 bg-sky-500/10 text-sky-200",
    toggleBtn: light
      ? "border-[#D3D8E2] bg-white text-gray-700 hover:bg-gray-50"
      : "border-[#222838] bg-[#14171E] text-gray-200 hover:bg-[#1a1f2a]",
    outlineBtn: light ? "border-[#D3D8E2] text-gray-700 bg-white hover:bg-gray-50" : "border-[#222838]",
  };
  const STATUS_CLS = light ? STATUS_CLS_LIGHT : STATUS_CLS_DARK;

  const [page, setPage] = useState(null);
  const [error, setError] = useState("");
  const [justPaid, setJustPaid] = useState(false);
  const [detail, setDetail] = useState(null); // invoice detail from /view
  const [detailFor, setDetailFor] = useState("");
  const [payFor, setPayFor] = useState(null);
  const [method, setMethod] = useState("card");
  const [amount, setAmount] = useState("");
  const [payBusy, setPayBusy] = useState(false);
  const [suggestion, setSuggestion] = useState("");
  const [suggestionSent, setSuggestionSent] = useState(false);
  const [suggestionBusy, setSuggestionBusy] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [helpMsg, setHelpMsg] = useState("");
  const [helpNote, setHelpNote] = useState("");
  const [helpDeskUrl, setHelpDeskUrl] = useState("");
  const [helpBusy, setHelpBusy] = useState(false);
  // Invoice-history tiles: pick-by-date, sort, privacy (hide) mode, flip.
  const [pickDate, setPickDate] = useState("");
  const [sortDir, setSortDir] = useState("desc");
  const [hideMode, setHideMode] = useState(false);
  const [revealedIds, setRevealedIds] = useState([]);
  const [flippedId, setFlippedId] = useState("");

  // Invoice Design Shop branding (from page.branding, loaded with the page)
  const br = page?.branding || {};
  const accent = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(br.accent_color || "")
    ? br.accent_color : null;
  const layout = ["classic", "modern", "bold"].includes(br.layout) ? br.layout : "classic";
  const accentStyle = accent ? { color: accent } : undefined;

  const submitHelp = async () => {
    setHelpBusy(true);
    try {
      const firstOpen = (page?.open || [])[0];
      const r = await api.post(`/c/${token}/payment-help`, {
        invoice_id: payFor?.id || firstOpen?.id || undefined,
        message: helpMsg.trim(),
      });
      setHelpNote(r.note || "Support request recorded.");
      setHelpDeskUrl(r.desk_url || "");
    } catch (e) { toast.error(errMsg(e)); }
    setHelpBusy(false);
  };

  const submitSuggestion = async () => {
    if (!suggestion.trim()) { toast.error("Write a suggestion first"); return; }
    setSuggestionBusy(true);
    try {
      await api.post(`/c/${token}/fee-suggestion`, { text: suggestion.trim() });
      setSuggestionSent(true);
      setSuggestion("");
    } catch (e) { toast.error(errMsg(e)); }
    setSuggestionBusy(false);
  };

  const load = () => {
    setError("");
    api.get(`/c/${token}`)
      .then(setPage)
      .catch(() => { setPage(null); setError("This link didn't work. It may have been replaced — ask the business for a fresh link."); });
  };
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    if (q.get("paid") === "1") {
      setJustPaid(true);
      window.history.replaceState({}, "", window.location.pathname);
    }
    load();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const viewInvoice = async (inv) => {
    if (detailFor === inv.id) { setDetailFor(""); setDetail(null); return; }
    setDetailFor(inv.id);
    try {
      const r = await api.get(`/c/${token}/view/${inv.id}`);
      setDetail(r.invoice);
    } catch (e) { toast.error(errMsg(e)); setDetail(null); }
  };

  const openPay = (inv) => {
    setPayFor(inv);
    setMethod("card");
    setAmount((inv.balance_cents / 100).toFixed(2));
    setSuggestionSent(false);
  };

  // Card vs bank (ACH) options for an invoice, from the backend payload
  // (charge includes any pass-through). Fallback: balance either way.
  const payOptionsFor = (inv) => {
    if (inv.pay_options && inv.pay_options.length)
      return inv.pay_options.map((o) => ({ ...o, fee_cents: o.platform_fee_cents ?? o.fee_cents ?? 0 }));
    return [
      { method: "card", charge_cents: inv.balance_cents, fee_cents: 0, label: "Card" },
      { method: "ach", charge_cents: inv.balance_cents, fee_cents: 0, label: "Bank (ACH)" },
    ];
  };

  const doPay = async () => {
    if (!payFor) return;
    const amt = parseFloat(amount);
    if (!(amt > 0)) { toast.error("Enter an amount"); return; }
    setPayBusy(true);
    try {
      const r = await api.post(`/c/${token}/pay/${payFor.id}`, {
        method, origin_url: window.location.origin,
        amount_cents: Math.round(amt * 100),
      });
      window.location.href = r.checkout_url;
    } catch (e) { toast.error(errMsg(e)); setPayBusy(false); }
  };

  const renderDetail = (inv) => (
    <div className={`mt-3 rounded-lg border p-4 ${C.panel}`} data-testid={`client-detail-${inv.id}`}>
      <table className="w-full text-sm">
        <thead><tr className={`text-xs uppercase tracking-wider ${C.muted} border-b ${C.headBorder}`}>
          <th className="py-2 text-left font-medium">Description</th>
          <th className="py-2 text-right font-medium">Amount</th>
        </tr></thead>
        <tbody>
          {(detail.lines || []).map((l, i) => (
            <tr key={i} className={`border-b ${C.rowBorder}`}>
              <td className={`py-2 ${C.t2}`}>{l.name || l.kind}</td>
              <td className={`py-2 text-right font-mono ${C.t2}`}>{money(l.amount_cents)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-3 space-y-1 text-sm">
        <div className="flex justify-between"><span className={C.muted}>Subtotal</span><span className={`font-mono ${C.t2}`}>{money(detail.subtotal_cents)}</span></div>
        {detail.tax_cents > 0 && <div className="flex justify-between"><span className={C.muted}>Tax</span><span className={`font-mono ${C.t2}`}>{money(detail.tax_cents)}</span></div>}
        <div className={`flex justify-between border-t ${C.headBorder} pt-2 font-semibold`}><span className={C.t1}>Total</span><span className={`font-mono ${C.t1}`}>{money(detail.total_cents)}</span></div>
        <div className="flex justify-between"><span className={C.muted}>Paid</span><span className={`font-mono ${C.emerald}`}>{money(detail.paid_cents)}</span></div>
        <div className="flex justify-between font-semibold"><span className={C.amber}>Balance due</span><span className={`font-mono ${C.amber}`}>{money(detail.balance_cents)}</span></div>
      </div>
    </div>
  );

  const renderInvoice = (inv, { paidSection = false } = {}) => (
    <div key={inv.id} className={`border-b ${C.rowBorder} last:border-0 py-4`} data-testid={`client-invoice-${inv.id}`}>
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex-1 min-w-[180px]">
          <div className={`${C.t1} font-medium`}>
            Invoice {inv.ref}
            {inv.split_label && <span className={`ml-2 px-2 py-0.5 rounded-full text-[11px] border ${C.splitBadge}`}>{inv.split_label}</span>}
          </div>
          <div className={`text-xs ${C.muted}`}>
            {inv.created_at && <>Issued {fmtDate(inv.created_at.slice(0, 10))}</>}
            {inv.due_date && <> · Due {fmtDate(inv.due_date)}</>}
            {!inv.due_date && inv.terms && <> · {inv.terms}</>}
          </div>
        </div>
        <span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[inv.status] || ""}`}>{inv.status}</span>
        <div className="text-right">
          <div className={`font-mono ${C.t2}`}>{money(inv.total_cents)}</div>
          {!paidSection && <div className={`font-mono text-xs ${C.amber}`}>{money(inv.balance_cents)} due</div>}
          {paidSection && <div className={`font-mono text-xs ${C.emerald}`}>paid</div>}
        </div>
        <div className="flex gap-1">
          <Button size="sm" variant="ghost" onClick={() => viewInvoice(inv)} data-testid={`client-view-${inv.id}`}>
            <Eye size={14} className="mr-1" /> {detailFor === inv.id ? "Hide" : "View"}
          </Button>
          {!paidSection && inv.balance_cents > 0 && (
            <Button size="sm" className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" onClick={() => openPay(inv)} data-testid={`client-pay-${inv.id}`}>
              Pay
            </Button>
          )}
        </div>
      </div>

      {inv.photos && inv.photos.length > 0 && (
        <div className="mt-3" data-testid={`client-photos-${inv.id}`}>
          <div className={`mb-1.5 flex items-center gap-1.5 text-xs font-medium ${C.muted}`}>
            <Camera size={13} /> Job photos from {page?.business || "the business"}
          </div>
          <div className="flex flex-wrap gap-2">
            {inv.photos.map((p) => (
              <a key={p.id} href={`${API_BASE}/c/${token}/photos/${inv.id}/${p.id}`}
                target="_blank" rel="noreferrer" data-testid={`client-photo-${p.id}`}
                className="group relative block overflow-hidden rounded-lg border border-black/10">
                <img src={`${API_BASE}/c/${token}/photos/${inv.id}/${p.id}`}
                  alt={p.caption || "Job photo"} loading="lazy"
                  className="h-24 w-32 object-cover" />
                {(p.kind === "before" || p.kind === "after") && (
                  <span className="absolute left-1 top-1 rounded-full bg-black/70 px-1.5 py-0.5 text-[10px] font-semibold capitalize text-white">{p.kind}</span>
                )}
                {p.caption && (
                  <span className="absolute inset-x-0 bottom-0 bg-black/60 px-1.5 py-0.5 text-[10px] text-white">{p.caption}</span>
                )}
              </a>
            ))}
          </div>
        </div>
      )}

      {inv.agreement && !paidSection && (
        inv.agreement.signed ? (
          <p className={`mt-3 flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${C.paidBanner}`} data-testid={`client-agreement-signed-${inv.id}`}>
            <CheckCircle2 size={15} />
            Agreement signed by {inv.agreement.signer_name}
            {inv.agreement.signed_at ? ` on ${fmtDate(String(inv.agreement.signed_at).slice(0, 10))}` : ""}.
          </p>
        ) : (
          <SignBox token={token} inv={inv} C={C} light={light} onSigned={load} />
        )
      )}

      {detailFor === inv.id && detail && renderDetail(inv)}

      {payFor?.id === inv.id && (() => {
        const opts = payOptionsFor(inv);
        const cardOpt = opts.find((o) => o.method === "card");
        const achOpt = opts.find((o) => o.method === "ach");
        const bankCheaper = cardOpt && achOpt && (achOpt.charge_cents < cardOpt.charge_cents
          || (cardOpt.processing_cents > 0 && achOpt.fee_cents < cardOpt.processing_cents));
        const fullBalance = Math.abs((parseFloat(amount) || 0) - inv.balance_cents / 100) < 0.005;
        return (
        <div className={`mt-3 rounded-lg border p-4 ${C.panel}`} data-testid={`client-paybox-${inv.id}`}>
          <div className={`mb-2 text-sm font-semibold ${C.t1}`}>Pay invoice {inv.ref}</div>
          {inv.agreement?.required && (
            <p className={`mb-2 rounded-md border px-3 py-2 text-sm ${C.autopayBanner}`} data-testid="client-pay-sign-first">
              Please sign the agreement above first — then this payment can go through.
            </p>
          )}
          {inv.early_pay?.active && inv.early_pay.discount_cents > 0 && (
            <div className={`mb-3 rounded-lg border px-4 py-3 ${C.paidBanner}`} data-testid={`client-early-pay-${inv.id}`}>
              <div className="flex items-center gap-2 text-sm font-semibold">
                <Zap size={15} />
                {br.firm_name || page.business} offers you {inv.early_pay.percent}% off for paying early
              </div>
              <div className="mt-2 grid grid-cols-2 gap-2 text-center sm:grid-cols-4">
                <div className={`rounded-md border px-2 py-2 ${C.panel}`}>
                  <div className={`text-[11px] uppercase tracking-wide ${C.muted}`}>Full price</div>
                  <div className={`font-mono text-base font-semibold ${C.t1}`}>{money(inv.balance_cents)}</div>
                </div>
                <div className={`rounded-md border px-2 py-2 ${C.panel}`}>
                  <div className={`text-[11px] uppercase tracking-wide ${C.muted}`}>Early-pay price</div>
                  <div className="font-mono text-base font-semibold text-emerald-500">{money(inv.early_pay.discounted_balance_cents)}</div>
                </div>
                <div className={`rounded-md border px-2 py-2 ${C.panel}`}>
                  <div className={`text-[11px] uppercase tracking-wide ${C.muted}`}>You save</div>
                  <div className="font-mono text-base font-semibold text-emerald-500">{money(inv.early_pay.discount_cents)}</div>
                </div>
                <div className={`rounded-md border px-2 py-2 ${C.panel}`}>
                  <div className={`text-[11px] uppercase tracking-wide ${C.muted}`}>Pay in full by</div>
                  <div className={`text-base font-semibold ${C.t1}`}>{fmtDate(inv.early_pay.deadline)}</div>
                </div>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Button size="sm" className="bg-emerald-600 hover:bg-emerald-500 text-black font-semibold"
                  onClick={() => setAmount((inv.early_pay.discounted_balance_cents / 100).toFixed(2))}
                  data-testid="client-early-pay-use">
                  Pay {money(inv.early_pay.discounted_balance_cents)} — use the early-pay price
                </Button>
                <span className={`text-xs ${C.muted}`}>
                  This discount comes out of {br.firm_name || page.business}'s own invoice total — it's their offer, on their invoice.
                </span>
              </div>
            </div>
          )}
          <div className="mb-3 max-w-[180px]"><Label className={C.label}>Amount ($)</Label>
            <Input type="number" step="0.01" min="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} className={`${C.input} w-full font-mono`} data-testid="client-pay-amount" /></div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2" role="radiogroup" aria-label="Payment method">
            {opts.map((o) => {
              const isBank = o.method === "ach";
              const selected = method === o.method;
              return (
                <button key={o.method} type="button" role="radio" aria-checked={selected} onClick={() => setMethod(o.method)}
                  data-testid={`client-pay-option-${o.method}`}
                  className={`relative text-left rounded-lg border p-3 transition-colors ${selected ? C.optSel : C.optIdle} ${isBank && bankCheaper ? "ring-1 ring-emerald-500/40" : ""}`}>
                  {isBank && bankCheaper && (
                    <span className="absolute -top-2 right-2 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-emerald-500 text-black" data-testid="client-pay-bank-cheaper">Lower fees: pay by bank</span>
                  )}
                  <span className={`flex items-center gap-2 text-sm font-medium ${C.t1}`}>
                    {isBank ? <Landmark size={15} /> : <CreditCard size={15} />}
                    {o.label || (isBank ? "Bank (ACH)" : "Card")}
                  </span>
                  <span className={`block mt-1 font-mono text-lg ${C.t1}`}>{money(o.charge_cents)}</span>
                  <span className={`block text-[11px] ${C.muted}`}>
                    total you pay{fullBalance ? "" : " for the full balance"}
                    {o.fee_cents > 0 && isBank ? ` · our price on bank payments: ${money(o.fee_cents)}` : ""}
                    {o.fee_cents > 0 && !isBank ? ` · includes ${money(o.fee_cents)} in fees` : ""}
                    {!isBank && o.processing_cents > 0 ? ` · card processing ≈ ${money(o.processing_cents)}` : ""}
                  </span>
                </button>
              );
            })}
          </div>
          {!fullBalance && <p className={`mt-1 text-[11px] ${C.faint}`}>Option totals above are for the full balance of {money(inv.balance_cents)}; your checkout will use the amount entered.</p>}

          {page?.fee_note && <p className={`mt-3 text-sm italic ${light ? "text-gray-600" : "text-gray-400"}`} data-testid="client-fee-note">{page.fee_note}</p>}

          <div className={`mt-3 border-t ${C.headBorder} pt-3`}>
            {suggestionSent ? (
              <p className={`text-sm ${C.emeraldNote} flex items-center gap-2`} data-testid="client-suggestion-thanks"><CheckCircle2 size={15} /> Thank you — your suggestion was sent to the business.</p>
            ) : (
              <div className="flex flex-wrap items-end gap-2">
                <div className="flex-1 min-w-[220px]"><Label className={C.label}>Have a cheaper-payment idea? Suggest it</Label>
                  <textarea value={suggestion} onChange={(e) => setSuggestion(e.target.value)} rows={2} placeholder="e.g. I can pay by Zelle / check / cash app…"
                    className={`w-full rounded-md border p-2 text-sm ${C.area}`} data-testid="client-suggestion-text" /></div>
                <Button size="sm" variant="outline" className={C.outlineBtn} disabled={suggestionBusy || !suggestion.trim()} onClick={submitSuggestion} data-testid="client-suggestion-submit">
                  {suggestionBusy ? "Sending…" : "Send suggestion"}
                </Button>
              </div>
            )}
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button disabled={payBusy} onClick={doPay} className="bg-emerald-600 hover:bg-emerald-500 text-black font-semibold" data-testid="client-pay-go">
              {payBusy ? "Starting secure checkout…" : `Pay by ${method === "ach" ? "bank" : "card"} — continue to secure checkout`}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setPayFor(null)}>Cancel</Button>
          </div>
          <p className={`mt-1 text-[11px] ${C.faint}`}>You'll finish on Stripe's secure checkout page. Your payment is recorded against this invoice automatically.</p>
        </div>
        );
      })()}
    </div>
  );

  return (
    <div className={C.root} data-testid="client-page" data-theme={theme}>
      <div className="mx-auto max-w-2xl px-4 py-10">
        {!page && !error && <p className={`text-center ${C.muted} py-16`}>Loading…</p>}
        {error && (
          <div className={`${C.card} p-8 text-center`} data-testid="client-error">
            <p className={`font-semibold ${C.t1}`}>This link didn't work</p>
            <p className={`mt-1 text-sm ${C.muted}`}>{error}</p>
          </div>
        )}
        {page && (
          <>
            {br.banner_url && (
              <img src={br.banner_url} alt="" data-testid="client-banner"
                className="mb-4 h-32 w-full rounded-xl border border-black/10 object-cover" />
            )}
            <div className={`mb-4 flex items-center gap-2 ${C.muted}`}>
              {layout !== "bold" && (
                <>
                  {br.logo_url
                    ? <img src={br.logo_url} alt={br.firm_name || page.business || "Business logo"} data-testid="client-logo" className="max-h-9 max-w-[160px] rounded bg-white object-contain p-0.5" />
                    : <ReceiptText size={16} />}
                  <span>
                    <span className="block text-sm">{br.firm_name || page.business || "Your provider"}</span>
                    {br.tagline && <span className="block text-xs" data-testid="client-tagline">{br.tagline}</span>}
                  </span>
                </>
              )}
              <button type="button" onClick={toggleTheme} data-testid="client-theme-toggle"
                aria-label={light ? "Switch to dark mode" : "Switch to light mode"}
                aria-pressed={light}
                className={`ml-auto inline-flex min-h-[44px] items-center gap-2 rounded-full border px-4 py-2 text-sm font-medium transition-colors ${C.toggleBtn}`}>
                {light ? <Moon size={16} /> : <Sun size={16} />}
                {light ? "Dark mode" : "Light mode"}
              </button>
            </div>

            {layout === "bold" ? (
              <div className="mb-2 rounded-xl p-5 text-white shadow" style={{ background: accent || "#92400E" }} data-testid="client-header-bold">
                <div className="flex items-center gap-3">
                  {br.logo_url && <img src={br.logo_url} alt="" className="max-h-11 max-w-[170px] rounded bg-white object-contain p-1" />}
                  <InvoiceAssistantMascot mood="idle" theme={theme} size={52} className="shrink-0" />
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-white/80" data-testid="client-brand">Bookkeeping InvoiceEase</p>
                    <h1 className="font-display text-2xl font-bold text-white mt-1">{page.client_name ? `${page.client_name}'s invoices` : "Your invoices"}</h1>
                  </div>
                </div>
                <p className="text-sm text-white/85 mt-2">{br.firm_name || page.business}{br.tagline ? ` — ${br.tagline}` : ""}</p>
                <p className="text-sm text-white/85">View and pay your invoices — no account or login needed.</p>
              </div>
            ) : layout === "modern" ? (
              <div className="mb-2 flex flex-col items-center text-center" data-testid="client-header-modern">
                {br.logo_url && <img src={br.logo_url} alt="" className="mb-2 max-h-12 max-w-[200px] rounded bg-white object-contain p-0.5" />}
                <InvoiceAssistantMascot mood="idle" theme={theme} size={52} className="shrink-0" />
                <p className={`mt-1 text-[11px] font-semibold uppercase tracking-[0.18em] ${C.amber}`} style={accentStyle} data-testid="client-brand">Bookkeeping InvoiceEase</p>
                <h1 className={`font-display text-2xl font-bold ${C.t1} mt-1`}>{page.client_name ? `${page.client_name}'s invoices` : "Your invoices"}</h1>
                <p className={`text-sm ${C.muted} mt-1`}>View and pay your invoices — no account or login needed.</p>
              </div>
            ) : (
              <>
                <div className="flex items-center gap-3">
                  <InvoiceAssistantMascot mood="idle" theme={theme} size={52} className="shrink-0" />
                  <div>
                    <p className={`text-[11px] font-semibold uppercase tracking-[0.18em] ${C.amber}`} style={accentStyle} data-testid="client-brand">Bookkeeping InvoiceEase</p>
                    <h1 className={`font-display text-2xl font-bold ${C.t1} mt-1`}>{page.client_name ? `${page.client_name}'s invoices` : "Your invoices"}</h1>
                  </div>
                </div>
                <p className={`text-sm ${C.muted} mt-1`}>View and pay your invoices — no account or login needed.</p>
              </>
            )}
            {br.default_message && (
              <p className={`mt-2 text-sm italic ${light ? "text-gray-600" : "text-gray-400"}`} data-testid="client-business-message">{br.default_message}</p>
            )}

            {justPaid && (
              <div className={`mt-4 rounded-lg border p-3 text-sm flex items-center gap-2 ${C.paidBanner}`} data-testid="client-paid-banner">
                <CheckCircle2 size={16} /> Payment received — thank you! It can take a moment to post to the invoice.
              </div>
            )}

            {page.autopay?.enrolled && (
              <div className={`mt-4 rounded-lg border p-3 text-sm flex items-center gap-2 ${C.autopayBanner}`} data-testid="client-autopay-state">
                <Zap size={16} className={light ? "text-sky-600" : "text-sky-300"} />
                Autopay is on — new invoices are charged automatically to your saved {page.autopay.brand || "card"}{page.autopay.last4 ? ` ending in ${page.autopay.last4}` : ""}.
              </div>
            )}

            <div className={`${C.card} p-6 sm:p-8 mt-6`} data-testid="client-open">
              <div className="flex items-end justify-between mb-2">
                <h2 className={`font-semibold ${C.t1}`}>Open invoices</h2>
                <div className="text-right">
                  <div className={`text-xs ${C.muted}`}>Total due</div>
                  <div className={`font-mono text-xl ${C.amber}`} style={accentStyle} data-testid="client-total-due">{money(page.total_open_cents)}</div>
                </div>
              </div>
              {(page.open || []).map((inv) => renderInvoice(inv))}
              {(page.open || []).length === 0 && <p className={`py-8 text-center ${C.muted} text-sm`}>Nothing due — you're all caught up. 🎉</p>}
            </div>

            {(page.history || []).length > 0 && (() => {
              const shown = sortHistory(filterHistoryByDate(page.history, pickDate), sortDir);
              return (
              <div className="relative left-1/2 mt-6 w-[min(96vw,64rem)] -translate-x-1/2" data-testid="client-history">
                <div className={`${C.card} p-6 sm:p-8`}>
                  <div className="mb-4 flex flex-wrap items-center gap-3">
                    <h2 className={`font-semibold ${C.t1}`}>Paid history</h2>
                    <span className={`text-xs ${C.muted}`}>{shown.length} of {page.history.length} invoices</span>
                    <div className="ml-auto flex flex-wrap items-center gap-2">
                      <input type="date" value={pickDate} onChange={(e) => setPickDate(e.target.value)}
                        aria-label="Pick invoices by date"
                        className={`rounded-md border px-2 py-1.5 text-sm ${C.input}`} data-testid="history-date-pick" />
                      {pickDate && (
                        <button type="button" onClick={() => setPickDate("")} className={`text-xs ${C.muted} underline`} data-testid="history-date-clear">All dates</button>
                      )}
                      <select value={sortDir} onChange={(e) => setSortDir(e.target.value)} aria-label="Sort history"
                        className={`rounded-md border px-2 py-1.5 text-sm ${C.input}`} data-testid="history-sort">
                        <option value="desc">Newest first</option>
                        <option value="asc">Oldest first</option>
                      </select>
                      <button type="button" onClick={() => { setHideMode((h) => !h); setRevealedIds([]); }}
                        aria-pressed={hideMode} data-testid="history-hide-toggle"
                        className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium ${hideMode ? C.optSel : C.optIdle}`}>
                        {hideMode ? <Eye size={13} /> : <EyeOff size={13} />}
                        {hideMode ? "Amounts hidden" : "Hide amounts"}
                      </button>
                    </div>
                  </div>
                  {hideMode && (
                    <p className={`mb-3 text-xs ${C.muted}`} data-testid="history-hide-note">
                      Privacy mode: amounts are hidden. Tap a card to reveal just that one.
                    </p>
                  )}
                  {shown.length === 0 && (
                    <p className={`text-sm ${C.muted}`}>No invoices on that date — pick another date or clear the filter.</p>
                  )}
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                    {shown.map((inv, idx) => (
                      <HistoryCard key={inv.id} inv={inv} idx={idx} C={C} light={light}
                        business={br.firm_name || page.business || ""}
                        bannerUrl={br.banner_url} logoUrl={br.logo_url} accent={accent}
                        masked={shouldMask(hideMode, revealedIds, inv.id)}
                        flipped={flippedId === inv.id}
                        onCardClick={() => {
                          if (shouldMask(hideMode, revealedIds, inv.id)) {
                            setRevealedIds((r) => [...r, inv.id]);
                          } else {
                            setFlippedId((f) => (f === inv.id ? "" : inv.id));
                          }
                        }}
                        onFlip={() => setFlippedId((f) => (f === inv.id ? "" : inv.id))}
                        onOpen={() => viewInvoice(inv)}
                      />
                    ))}
                  </div>
                  {detailFor && shown.some((i) => i.id === detailFor) && detail && (
                    renderDetail(shown.find((i) => i.id === detailFor))
                  )}
                </div>
              </div>
              );
            })()}
            <div className={`${C.card} p-6 mt-6`} data-testid="client-help">
              {!helpOpen && !helpNote && (
                <button type="button" onClick={() => setHelpOpen(true)} className={`text-sm font-medium ${C.skyLink}`} data-testid="client-help-open">
                  {page.help?.label || "Payment problem or question?"}
                </button>
              )}
              {helpOpen && !helpNote && (
                <div>
                  <p className={`text-sm font-medium ${C.t1}`}>{page.help?.label || "Payment problem or question?"}</p>
                  <p className={`mt-1 text-xs ${C.muted}`}>Your invoice details are attached automatically — no need to re-explain.</p>
                  <textarea value={helpMsg} onChange={(e) => setHelpMsg(e.target.value)} rows={3} placeholder="What's happening with your payment?"
                    className={`mt-2 w-full rounded-md border p-2 text-sm ${C.area}`} data-testid="client-help-text" />
                  <div className="mt-2 flex gap-2">
                    <Button size="sm" disabled={helpBusy} onClick={submitHelp} data-testid="client-help-submit">{helpBusy ? "Sending…" : "Get help"}</Button>
                    <Button size="sm" variant="ghost" onClick={() => setHelpOpen(false)}>Cancel</Button>
                  </div>
                </div>
              )}
              {helpNote && (
                <p className={`text-sm ${C.emeraldNote} flex items-center gap-2`} data-testid="client-help-done">
                  <CheckCircle2 size={15} /> {helpNote}
                  {helpDeskUrl && <a className={`underline ${C.skyNote}`} href={helpDeskUrl}>Continue to support</a>}
                </p>
              )}
              <div className={`mt-5 border-t ${C.headBorder} pt-4`}>
                <FreeHelpPanel tone={light ? "light" : "dark"} />
              </div>
            </div>
            {br.footer_note && (
              <p className={`mt-6 text-center text-xs ${C.muted}`} data-testid="client-footer-note">{br.footer_note}</p>
            )}
            <p className={`mt-6 text-center text-xs ${C.faint}`}>Secure link — only someone with this link can see these invoices. Ask the business for a new link if this one stops working.</p>
            <p className={`mt-2 text-center text-[11px] ${C.faint}`} data-testid="client-design-note">
              Your invoice, your look — {br.firm_name || page.business || "this business"} designed this page in Bookkeeping InvoiceEase.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

/**
 * Self-built simple e-sign box: review the agreement, sign by typing your
 * name or drawing it, tick explicit consent, submit. The backend stores an
 * audit trail (name, time, IP, device). Honest label shown to the signer:
 * a simple e-sign with an audit trail — not a third-party certified
 * e-signature service. Payment stays blocked until this succeeds.
 */
function SignBox({ token, inv, C, light, onSigned }) {
  const [name, setName] = useState("");
  const [mode, setMode] = useState("typed");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [drew, setDrew] = useState(false);
  const canvasRef = useRef(null);
  const drawing = useRef(false);

  const pos = (e) => {
    const r = canvasRef.current.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const startDraw = (e) => {
    drawing.current = true;
    const ctx = canvasRef.current.getContext("2d");
    const p = pos(e);
    ctx.beginPath(); ctx.moveTo(p.x, p.y);
  };
  const moveDraw = (e) => {
    if (!drawing.current) return;
    const ctx = canvasRef.current.getContext("2d");
    const p = pos(e);
    ctx.lineWidth = 2; ctx.lineCap = "round";
    ctx.strokeStyle = light ? "#111827" : "#F9FAFB";
    ctx.lineTo(p.x, p.y); ctx.stroke();
    setDrew(true);
  };
  const clearDraw = () => {
    const c = canvasRef.current;
    c.getContext("2d").clearRect(0, 0, c.width, c.height);
    setDrew(false);
  };

  const submit = async () => {
    if (!name.trim()) { toast.error("Type your full name to sign"); return; }
    if (!consent) { toast.error("Please tick the consent box to sign"); return; }
    let data;
    if (mode === "typed") data = name.trim();
    else {
      if (!drew) { toast.error("Draw your signature in the box"); return; }
      data = canvasRef.current.toDataURL("image/png");
    }
    setBusy(true);
    try {
      await api.post(`/c/${token}/sign/${inv.id}`, {
        signer_name: name.trim(), signature_type: mode,
        signature_data: data, consent: true,
      });
      toast.success("Agreement signed — thank you");
      onSigned && onSigned();
    } catch (e) { toast.error(errMsg(e)); }
    setBusy(false);
  };

  return (
    <div className={`mt-3 rounded-lg border p-4 ${C.panel}`} data-testid={`client-agreement-${inv.id}`}>
      <div className={`flex items-center gap-2 text-sm font-semibold ${C.t1}`}>
        <PenLine size={15} /> Please review and sign the agreement
        {inv.split_label ? ` — required before paying the ${inv.split_label.toLowerCase()}` : ""}
      </div>
      <div className={`mt-2 max-h-40 overflow-y-auto whitespace-pre-wrap rounded-md border p-3 text-sm ${C.area}`} data-testid="client-agreement-text">
        {inv.agreement.text}
      </div>
      <div className="mt-3 flex gap-2">
        {["typed", "drawn"].map((m) => (
          <button key={m} type="button" onClick={() => setMode(m)}
            data-testid={`client-sign-mode-${m}`}
            className={`rounded-full border px-3 py-1 text-xs capitalize ${mode === m ? C.optSel : C.optIdle}`}>
            {m === "typed" ? "Type my name" : "Draw it"}
          </button>
        ))}
      </div>
      {mode === "typed" ? (
        <div className="mt-2 max-w-sm">
          <Label className={C.label}>Your full name (this is your signature)</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)}
            className={`${C.input} w-full`} data-testid="client-sign-name"
            placeholder="e.g. Amy Acme" />
        </div>
      ) : (
        <div className="mt-2">
          <Label className={C.label}>Draw your signature</Label>
          <canvas ref={canvasRef} width={320} height={90}
            data-testid="client-sign-canvas"
            className={`block w-full max-w-[320px] touch-none rounded-md border ${light ? "border-[#D3D8E2] bg-white" : "border-[#222838] bg-[#0F1218]"}`}
            onPointerDown={startDraw} onPointerMove={moveDraw}
            onPointerUp={() => { drawing.current = false; }}
            onPointerLeave={() => { drawing.current = false; }} />
          <button type="button" onClick={clearDraw} className={`mt-1 text-xs ${C.muted}`}>Clear</button>
          <div className="mt-1 max-w-sm">
            <Label className={C.label}>Your full name (printed)</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)}
              className={`${C.input} w-full`} data-testid="client-sign-name"
              placeholder="e.g. Amy Acme" />
          </div>
        </div>
      )}
      <label className={`mt-3 flex cursor-pointer items-start gap-2 text-sm ${C.t2}`}>
        <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)}
          className="mt-0.5 h-4 w-4" data-testid="client-sign-consent" />
        I agree to sign this agreement electronically and to its terms.
      </label>
      <Button onClick={submit} disabled={busy}
        className="mt-3 bg-amber-600 hover:bg-amber-500 text-black font-semibold"
        data-testid="client-sign-submit">
        {busy ? "Signing…" : "Sign agreement"}
      </Button>
      <p className={`mt-2 text-[11px] ${C.faint}`}>
        This is a simple electronic signature with an audit trail — your name,
        the time, and your device are recorded with the agreement. It is not a
        third-party certified e-signature service.
      </p>
    </div>
  );
}

/**
 * One paid-invoice history tile (owner design order 2026-11-14): cards
 * drop in with a cascade, the grid keeps visible edges, and each card
 * FLIPS — front is the simple face (date, status, amount; NO account or
 * payment details ever), the back carries the business's banner/branding
 * plus the invoice's fuller story. In privacy (hide) mode amounts stay
 * masked until the viewer taps that one card to reveal it.
 */
function HistoryCard({ inv, idx, C, light, business, bannerUrl, logoUrl, accent,
                       masked, flipped, onCardClick, onFlip, onOpen }) {
  const faceCls = `absolute inset-0 flex flex-col rounded-xl border p-4 [backface-visibility:hidden] ${C.card}`;
  return (
    <div className="hist-card-in [perspective:1200px]" style={{ animationDelay: `${Math.min(idx, 12) * 70}ms` }}
      data-testid={`history-card-${inv.id}`}>
      <div className="relative h-64 w-full transition-transform duration-500 [transform-style:preserve-3d]"
        style={{ transform: flipped ? "rotateY(180deg)" : "rotateY(0deg)" }}>
        {/* FRONT — the simple face */}
        <div className={faceCls}>
          <div className={`flex items-center gap-2 text-xs ${C.muted}`}>
            {logoUrl
              ? <img src={logoUrl} alt="" className="max-h-6 max-w-[90px] rounded bg-white object-contain p-0.5" />
              : <ReceiptText size={13} />}
            <span className="truncate">{business}</span>
            <span className={`ml-auto px-2 py-0.5 rounded-full text-[11px] border capitalize ${light ? "bg-emerald-500/10 text-emerald-700 border-emerald-500/30" : "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"}`}>{inv.status}</span>
          </div>
          <button type="button" onClick={onCardClick} data-testid={`history-card-face-${inv.id}`}
            className="mt-3 flex flex-1 flex-col items-start text-left" aria-label={`Invoice ${inv.ref}`}>
            <span className={`text-sm font-medium ${C.t1}`}>Invoice {inv.ref}</span>
            <span className={`text-xs ${C.muted}`}>
              {inv.created_at ? `Issued ${fmtDate(inv.created_at.slice(0, 10))}` : ""}
              {inv.split_label ? ` · ${inv.split_label}` : ""}
            </span>
            <span className={`mt-auto font-mono text-2xl ${C.t1}`} data-testid={`history-card-amount-${inv.id}`}>
              {faceMoney(inv.total_cents, masked)}
            </span>
            <span className={`text-xs ${C.emerald}`}>{masked ? "Tap to reveal" : "Paid in full"}</span>
          </button>
          <div className="mt-2 flex items-center gap-2">
            <button type="button" onClick={onOpen} data-testid={`history-card-open-${inv.id}`}
              className={`inline-flex items-center gap-1 rounded-md border px-2.5 py-1.5 text-xs ${C.outlineBtn}`}>
              <Eye size={12} /> Open
            </button>
            <button type="button" onClick={onFlip} data-testid={`history-card-flip-${inv.id}`}
              className={`ml-auto inline-flex items-center gap-1 rounded-md border px-2.5 py-1.5 text-xs ${C.outlineBtn}`}>
              <RotateCw size={12} /> Flip for its story
            </button>
          </div>
        </div>
        {/* BACK — branding + the fuller story */}
        <div className={`${faceCls} [transform:rotateY(180deg)] overflow-hidden`}>
          {bannerUrl ? (
            <img src={bannerUrl} alt="" className="-mx-4 -mt-4 mb-3 h-16 w-[calc(100%+2rem)] max-w-none object-cover" />
          ) : (
            <div className="-mx-4 -mt-4 mb-3 flex h-16 items-center gap-2 px-4 text-white" style={{ background: accent || "#92400E" }}>
              {logoUrl && <img src={logoUrl} alt="" className="max-h-9 max-w-[110px] rounded bg-white object-contain p-0.5" />}
              <span className="font-display font-semibold">{business}</span>
            </div>
          )}
          <button type="button" onClick={onCardClick} data-testid={`history-card-back-${inv.id}`}
            className="flex flex-1 flex-col items-start overflow-y-auto text-left" aria-label={`Invoice ${inv.ref} story`}>
            <span className={`text-sm font-medium ${C.t1}`}>Invoice {inv.ref} — the story</span>
            <span className={`text-xs ${C.muted}`}>
              {inv.created_at ? `Issued ${fmtDate(inv.created_at.slice(0, 10))}` : ""}
              {inv.due_date ? ` · Due ${fmtDate(inv.due_date)}` : ""}
              {inv.terms ? ` · ${inv.terms}` : ""}
            </span>
            <span className="mt-2 w-full space-y-1">
              {(inv.lines || []).slice(0, 3).map((l, i) => (
                <span key={i} className="flex justify-between gap-2 text-xs">
                  <span className={`${C.t2} truncate`}>{l.name || l.kind}</span>
                  <span className={`font-mono ${C.t2}`}>{faceMoney(l.amount_cents, masked)}</span>
                </span>
              ))}
              {(inv.lines || []).length > 3 && (
                <span className={`block text-[11px] ${C.faint}`}>+ {inv.lines.length - 3} more lines — tap Open for the full invoice</span>
              )}
            </span>
            <span className="mt-auto w-full space-y-0.5 text-xs">
              <span className="flex justify-between"><span className={C.muted}>Total</span><span className={`font-mono ${C.t1}`}>{faceMoney(inv.total_cents, masked)}</span></span>
              <span className="flex justify-between"><span className={C.muted}>Paid</span><span className={`font-mono ${C.emerald}`}>{faceMoney(inv.paid_cents, masked)}</span></span>
              {inv.agreement?.signed && (
                <span className={`block pt-1 ${C.muted}`}>Agreement signed by {inv.agreement.signer_name}.</span>
              )}
            </span>
          </button>
          <div className="mt-2 flex items-center gap-2">
            <button type="button" onClick={onOpen} className={`inline-flex items-center gap-1 rounded-md border px-2.5 py-1.5 text-xs ${C.outlineBtn}`}>
              <Eye size={12} /> Open full invoice
            </button>
            <button type="button" onClick={onFlip} className={`ml-auto inline-flex items-center gap-1 rounded-md border px-2.5 py-1.5 text-xs ${C.outlineBtn}`}>
              <RotateCw size={12} /> Back to front
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
