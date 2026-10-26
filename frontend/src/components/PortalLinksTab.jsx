import { useEffect, useState } from "react";
import { Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { toast } from "sonner";
import { Link2, Copy, RefreshCw, Ban, CheckCircle2 } from "lucide-react";
import { useTranslation } from "react-i18next";
const EXPIRIES = [{
  hours: 24,
  labelKey: "portalLinks.24Hours"
}, {
  hours: 24 * 7,
  labelKey: "portalLinks.7Days"
}, {
  hours: 24 * 30,
  labelKey: "portalLinks.30Days"
}];
const STATUS_CLS = {
  active: "text-emerald-400 border-emerald-500/30 bg-emerald-500/10",
  expired: "text-gray-500 border-gray-600/40 bg-gray-600/10",
  used: "text-sky-400 border-sky-500/30 bg-sky-500/10",
  revoked: "text-rose-400 border-rose-500/30 bg-rose-500/10"
};
function portalUrl(token) {
  return `${window.location.origin}/p/${token}`;
}
async function copyText(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(`${what} copied`);
  } catch {
    toast.error(t("portallinkstab.couldnTCopyCopyItManually"));
  }
}

/** Owner UI for magic-link payment links. The raw token exists only at
 *  creation time (only its hash is stored), so the full URL is shown with a
 *  copy button right after generation; later, "New link" regenerates. */
export default function PortalLinksTab({
  invoices
}) {
  const {
    t
  } = useTranslation();
  const [links, setLinks] = useState(null);
  const [reports, setReports] = useState([]);
  const [invoiceId, setInvoiceId] = useState("");
  const [expiry, setExpiry] = useState(String(24 * 7));
  const [fresh, setFresh] = useState(null); // {url, client_name, expires_at}
  const [busy, setBusy] = useState(false);
  const loadReports = () => api.get("/portal/payment-reports?status=pending").then(setReports).catch(() => setReports([]));
  const load = () => {
    api.get("/portal/links").then(setLinks).catch(() => setLinks([]));
    loadReports();
  };
  const decideReport = async (id, action) => {
    try {
      await api.post(`/portal/payment-reports/${id}/${action}`, {});
      toast.success(action === "confirm" ? "Payment confirmed — it's posted to the invoice now." : "Report rejected — nothing was posted.");
      loadReports();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  useEffect(() => {
    load();
  }, []);
  const unpaid = (invoices || []).filter(i => i.balance_cents > 0);
  const generate = async forInvoiceId => {
    const id = forInvoiceId || invoiceId;
    if (!id) {
      toast.error(t("portallinkstab.pickAnInvoiceFirst"));
      return;
    }
    setBusy(true);
    try {
      const r = await api.post("/portal/links", {
        invoice_id: id,
        expires_in_hours: parseInt(expiry, 10)
      });
      setFresh({
        url: portalUrl(r.token),
        client_name: r.client_name,
        expires_at: r.expires_at
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const revoke = async id => {
    try {
      await api.post(`/portal/links/${id}/revoke`, {});
      toast.success(t("portallinkstab.linkRevoked"));
      setLinks(ls => ls.map(l => l.id === id ? {
        ...l,
        status: "revoked",
        revoked: true
      } : l));
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (links === null) return <p className="text-sm text-gray-500 py-6 text-center">{t("portallinkstab.loadingPaymentLinks")}</p>;
  return <div className="space-y-4 mt-4">
      <Card>
        <h3 className="font-semibold text-gray-100 mb-1 flex items-center gap-2">
          <Link2 className="w-4 h-4" /> {t("portallinkstab.newPaymentLink")}</h3>
        <p className="text-sm text-gray-500 mb-4">{t("portallinkstab.sendAClientALoginFree")}</p>
        <div className="flex flex-col sm:flex-row gap-2">
          <Select value={invoiceId} onValueChange={setInvoiceId}>
            <SelectTrigger className="bg-[#14171E] border-[#222838] flex-1" data-testid="plink-invoice">
              <SelectValue placeholder={unpaid.length ? "Choose an unpaid invoice…" : "No unpaid invoices"} />
            </SelectTrigger>
            <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
              {unpaid.map(i => <SelectItem key={i.id} value={i.id}>
                  {i.client_name} — {money(i.balance_cents)} {t("portallinkstab.due")}</SelectItem>)}
            </SelectContent>
          </Select>
          <Select value={expiry} onValueChange={setExpiry}>
            <SelectTrigger className="bg-[#14171E] border-[#222838] w-36" data-testid="plink-expiry">
              <SelectValue />
            </SelectTrigger>
            <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
              {EXPIRIES.map(e => <SelectItem key={e.hours} value={String(e.hours)}>{t(e.labelKey)}</SelectItem>)}
            </SelectContent>
          </Select>
          <Button onClick={() => generate()} disabled={busy || !unpaid.length} data-testid="plink-generate" className="bg-amber-600 hover:bg-amber-500 text-black font-semibold">
            {busy ? "Generating…" : "Generate link"}
          </Button>
        </div>
        {fresh && <div className="mt-4 rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3 flex items-center gap-3">
            <CheckCircle2 size={18} className="text-emerald-400 shrink-0" />
            <div className="min-w-0 flex-1">
              <div className="text-xs text-gray-400">{t("portallinkstab.linkFor")} {fresh.client_name} {t("portallinkstab.expires")} {fmtDate(fresh.expires_at?.slice(0, 10))}. Copy it now; it can't be shown again.</div>
              <div className="text-sm text-gray-100 font-mono truncate">{fresh.url}</div>
            </div>
            <Button size="sm" variant="outline" className="border-[#222838] shrink-0" onClick={() => copyText(fresh.url, "Payment link")} data-testid="plink-copy-fresh">
              <Copy size={14} className="mr-1" /> {t("portallinkstab.copy")}</Button>
          </div>}
      </Card>

      {reports.length > 0 && <Card className="p-0 overflow-hidden" data-testid="portal-pending-reports-card">
        <div className="px-5 pt-4 pb-2">
          <h3 className="font-semibold text-gray-100">Reported payments — waiting for your confirmation</h3>
          <p className="text-sm text-gray-500">A client reported paying outside the portal. Nothing is posted to their invoice until you confirm it.</p>
        </div>
        <table className="w-full text-sm" data-testid="table-portal-payment-reports">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="px-5 py-3 text-left font-medium">Client</th>
            <th className="px-5 py-3 text-left font-medium">Invoice</th>
            <th className="px-5 py-3 text-left font-medium">Reported</th>
            <th className="px-5 py-3 text-right font-medium">Amount</th>
            <th className="px-5 py-3 text-right font-medium">Actions</th>
          </tr></thead>
          <tbody>
            {reports.map(r => <tr key={r.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                <td className="px-5 py-3 text-gray-100">{r.client_name || "—"}</td>
                <td className="px-5 py-3 text-gray-400">{r.invoice_ref}</td>
                <td className="px-5 py-3 text-gray-400 text-xs whitespace-nowrap">{fmtDate(r.date)} · {r.method}{r.reference ? ` · ${r.reference}` : ""}</td>
                <td className="px-5 py-3 text-right font-mono text-gray-100">{money(r.amount_cents)}</td>
                <td className="px-5 py-3 text-right">
                  <div className="flex justify-end gap-1">
                    <Button size="sm" variant="ghost" className="text-emerald-400 hover:text-emerald-300" onClick={() => decideReport(r.id, "confirm")} data-testid={`plink-confirm-${r.id}`}>
                      <CheckCircle2 size={14} className="mr-1" /> Confirm</Button>
                    <Button size="sm" variant="ghost" className="text-rose-400 hover:text-rose-300" onClick={() => decideReport(r.id, "reject")} data-testid={`plink-reject-${r.id}`}>
                      <Ban size={14} className="mr-1" /> Reject</Button>
                  </div>
                </td>
              </tr>)}
          </tbody>
        </table>
      </Card>}

      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm" data-testid="table-portal-links">
          <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
            <th className="px-5 py-3 text-left font-medium">{t("portallinkstab.client")}</th>
            <th className="px-5 py-3 text-left font-medium">{t("portallinkstab.created")}</th>
            <th className="px-5 py-3 text-left font-medium">{t("portallinkstab.expires2")}</th>
            <th className="px-5 py-3 text-left font-medium">{t("portallinkstab.status")}</th>
            <th className="px-5 py-3 text-right font-medium">{t("portallinkstab.actions")}</th>
          </tr></thead>
          <tbody>
            {links.map(l => <tr key={l.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                <td className="px-5 py-3 text-gray-100">{l.client_name || l.customer_id || "—"}</td>
                <td className="px-5 py-3 text-gray-400 text-xs whitespace-nowrap">{fmtDate(l.created_at?.slice(0, 10))} · {l.created_by || "—"}</td>
                <td className="px-5 py-3 text-gray-400 text-xs whitespace-nowrap">{fmtDate(l.expires_at?.slice(0, 10))}</td>
                <td className="px-5 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[l.status] || ""}`}>{l.status}</span></td>
                <td className="px-5 py-3 text-right">
                  <div className="flex justify-end gap-1">
                    {l.status === "active" && <>
                        <Button size="sm" variant="ghost" title={t("portallinkstab.generateAFreshLinkForThis")} onClick={() => generate(l.invoice_id)} data-testid={`plink-regen-${l.id}`}>
                          <RefreshCw size={14} className="mr-1" /> {t("portallinkstab.newLink")}</Button>
                        <Button size="sm" variant="ghost" className="text-rose-400 hover:text-rose-300" title={t("portallinkstab.revokeThisLink")} onClick={() => revoke(l.id)} data-testid={`plink-revoke-${l.id}`}>
                          <Ban size={14} className="mr-1" /> {t("portallinkstab.revoke")}</Button>
                      </>}
                  </div>
                </td>
              </tr>)}
            {links.length === 0 && <tr><td colSpan={5} className="px-5 py-12 text-center text-gray-500">{t("portallinkstab.noPaymentLinksYetGenerateOne")}</td></tr>}
          </tbody>
        </table>
      </Card>
    </div>;
}

/** One-click: create a 7-day link for an invoice and copy it. */
export async function quickPaymentLink(invoiceId) {
  const r = await api.post("/portal/links", {
    invoice_id: invoiceId,
    expires_in_hours: 24 * 7
  });
  await copyText(portalUrl(r.token), "Payment link");
}
