import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { toast } from "sonner";
import { Link2, Copy, Mail, Ban, Plus, Check } from "lucide-react";
import { useTranslation } from "react-i18next";

const STATUS_TONE = {
  active: "bg-emerald-500/15 text-emerald-400",
  expired: "bg-gray-500/15 text-gray-400",
  used: "bg-sky-500/15 text-sky-400",
  revoked: "bg-rose-500/15 text-rose-400",
};
const EMAIL_TONE = {
  sent: "bg-emerald-500/15 text-emerald-400",
  queued: "bg-amber-500/15 text-amber-400",
  failed: "bg-rose-500/15 text-rose-400",
  no_recipient: "bg-amber-500/15 text-amber-400",
  not_sent: "bg-gray-500/15 text-gray-500",
};

export default function PortalLinks() {
  const { t } = useTranslation();
  const [links, setLinks] = useState(null);
  const [invoices, setInvoices] = useState([]);
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [result, setResult] = useState(null); // {portal_url, email_status, ...} after create/send
  const [copied, setCopied] = useState(false);
  const [sendOpen, setSendOpen] = useState(null); // link row being (re)sent
  const [sendEmail, setSendEmail] = useState("");
  const [sending, setSending] = useState(false);
  const [form, setForm] = useState({ invoice_id: "", expires_in_hours: 168, send_email: true });

  const reload = () => {
    api.get("/portal/links").then(setLinks).catch(() => setLinks([]));
  };
  useEffect(() => {
    reload();
    api.get("/invoices").then(rows => {
      setInvoices((rows || []).filter(i => (i.total_cents || 0) - (i.paid_cents || 0) > 0));
    }).catch(() => {});
  }, []);

  const doCreate = async () => {
    if (!form.invoice_id) { toast.error(t("portal.selectInvoice")); return; }
    setCreating(true);
    try {
      const res = await api.post("/portal/links", {
        invoice_id: form.invoice_id,
        expires_in_hours: Number(form.expires_in_hours) || 168,
        send_email: form.send_email,
        origin_url: window.location.origin,
      });
      const url = res.portal_url || `${window.location.origin}/p/${res.token}`;
      setResult({ ...res, portal_url: url });
      setOpen(false);
      reload();
      toast.success(t("portal.linkCreated"));
    } catch (e) {
      toast.error(errMsg(e));
    }
    setCreating(false);
  };

  const doRevoke = async (id) => {
    if (!window.confirm(t("portal.revokeConfirm"))) return;
    try {
      await api.post(`/portal/links/${id}/revoke`);
      toast.success(t("portal.revoked"));
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const doSend = async () => {
    if (!sendOpen) return;
    setSending(true);
    try {
      const res = await api.post(`/portal/links/${sendOpen.id}/send`, {
        origin_url: window.location.origin,
        to_email: sendEmail.trim(),
      });
      const url = res.portal_url || `${window.location.origin}/p/${res.token}`;
      setResult({ ...res, portal_url: url });
      setSendOpen(null);
      setSendEmail("");
      reload();
      toast.success(res.email_status === "sent" ? t("portal.emailSent") : t("portal.emailQueued"));
    } catch (e) {
      toast.error(errMsg(e));
    }
    setSending(false);
  };

  const copyUrl = async (url) => {
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = url;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
    toast.success(t("portal.copied"));
  };

  if (links === null) return <Loading />;

  return (
    <div className="animate-rise">
      <PageHeader
        title={t("portal.title")}
        subtitle={t("portal.subtitle")}
        testid="portal-links-page"
        action={<Button onClick={() => { setResult(null); setOpen(true); }} data-testid="portal-new-link"><Plus size={16} className="mr-1" />{t("portal.newLink")}</Button>}
      />

      {result?.portal_url && (
        <Card className="mb-6 border-amber-500/40" data-testid="portal-result">
          <div className="text-sm font-medium text-amber-400 mb-2">{t("portal.linkReady")}</div>
          <div className="flex items-center gap-2">
            <Input readOnly value={result.portal_url} className="font-mono text-xs bg-[#14171E] border-[#222838]" data-testid="portal-url" />
            <Button variant="outline" size="sm" onClick={() => copyUrl(result.portal_url)} data-testid="portal-copy">
              {copied ? <Check size={14} /> : <Copy size={14} />}
            </Button>
          </div>
          <div className="mt-2 text-xs text-gray-400">
            {t("portal.emailStatus")}: <span className={`px-2 py-0.5 rounded-full ${EMAIL_TONE[result.email_status] || EMAIL_TONE.not_sent}`}>{result.email_status}</span>
            {result.email_to && <span className="ml-2">{result.email_to}</span>}
            {result.email_error && result.email_status !== "sent" && <span className="ml-2 text-amber-400/80">{result.email_error}</span>}
          </div>
          <div className="mt-1 text-[11px] text-gray-500">{t("portal.sendRotatesNote")}</div>
        </Card>
      )}

      <Card className="p-0 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="portal-links-table">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-5 py-3 font-medium">{t("portal.client")}</th>
                <th className="px-5 py-3 font-medium">{t("portal.invoice")}</th>
                <th className="px-5 py-3 font-medium">{t("portal.created")}</th>
                <th className="px-5 py-3 font-medium">{t("portal.expires")}</th>
                <th className="px-5 py-3 font-medium">{t("portal.status")}</th>
                <th className="px-5 py-3 font-medium">{t("portal.email")}</th>
                <th className="px-5 py-3 font-medium text-right">{t("portal.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {links.map(l => (
                <tr key={l.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                  <td className="px-5 py-3">{l.client_name || l.customer_id || "—"}</td>
                  <td className="px-5 py-3 font-mono text-amber-400 text-xs">{l.invoice_id?.slice(0, 8)}</td>
                  <td className="px-5 py-3 text-gray-400">{fmtDate(l.created_at)}</td>
                  <td className="px-5 py-3 text-gray-400">{fmtDate(l.expires_at)}</td>
                  <td className="px-5 py-3">
                    <span className={`px-2 py-0.5 rounded-full text-xs ${STATUS_TONE[l.status] || STATUS_TONE.expired}`}>{t(`portal.status_${l.status}`)}</span>
                  </td>
                  <td className="px-5 py-3">
                    <span className={`px-2 py-0.5 rounded-full text-xs ${EMAIL_TONE[l.email_status] || EMAIL_TONE.not_sent}`}>{l.email_status || "not_sent"}</span>
                    {l.email_to && <div className="text-[11px] text-gray-500 mt-0.5">{l.email_to}</div>}
                  </td>
                  <td className="px-5 py-3 text-right whitespace-nowrap">
                    {l.status === "active" && (
                      <>
                        <Button variant="ghost" size="sm" title={t("portal.sendEmail")} onClick={() => { setSendOpen(l); setSendEmail(l.email_to || ""); }} data-testid={`portal-send-${l.id}`}>
                          <Mail size={14} />
                        </Button>
                        <Button variant="ghost" size="sm" title={t("portal.revoke")} onClick={() => doRevoke(l.id)} data-testid={`portal-revoke-${l.id}`}>
                          <Ban size={14} className="text-rose-400" />
                        </Button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
              {links.length === 0 && (
                <tr><td colSpan={7} className="px-5 py-12 text-center text-gray-500">{t("portal.noLinks")}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent data-testid="portal-new-dialog">
          <DialogHeader><DialogTitle>{t("portal.newLink")}</DialogTitle></DialogHeader>
          <div className="space-y-4">
            <div>
              <Label>{t("portal.invoice")}</Label>
              <Select value={form.invoice_id} onValueChange={v => setForm({ ...form, invoice_id: v })}>
                <SelectTrigger className="mt-1 bg-[#14171E] border-[#222838]" data-testid="portal-invoice-select">
                  <SelectValue placeholder={t("portal.selectInvoice")} />
                </SelectTrigger>
                <SelectContent>
                  {invoices.map(i => (
                    <SelectItem key={i.id} value={i.id}>
                      {(i.client_name || i.id.slice(0, 8))} — {money((i.total_cents || 0) - (i.paid_cents || 0))} {t("portal.due")}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>{t("portal.expiryHours")}</Label>
              <Input type="number" min={1} max={2160} value={form.expires_in_hours}
                onChange={e => setForm({ ...form, expires_in_hours: e.target.value })}
                className="mt-1 bg-[#14171E] border-[#222838]" data-testid="portal-expiry" />
            </div>
            <div className="flex items-center justify-between">
              <Label>{t("portal.sendViaEmail")}</Label>
              <Switch checked={form.send_email} onCheckedChange={v => setForm({ ...form, send_email: v })} data-testid="portal-send-toggle" />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>{t("portal.cancel")}</Button>
            <Button onClick={doCreate} disabled={creating} data-testid="portal-create">
              <Link2 size={14} className="mr-1" />{t("portal.createLink")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!sendOpen} onOpenChange={v => { if (!v) setSendOpen(null); }}>
        <DialogContent data-testid="portal-send-dialog">
          <DialogHeader><DialogTitle>{t("portal.sendEmail")}</DialogTitle></DialogHeader>
          <div className="space-y-4">
            <p className="text-sm text-gray-400">{t("portal.sendRotatesNote")}</p>
            <div>
              <Label>{t("portal.emailOverride")}</Label>
              <Input value={sendEmail} onChange={e => setSendEmail(e.target.value)}
                placeholder={t("portal.emailOverridePh")}
                className="mt-1 bg-[#14171E] border-[#222838]" data-testid="portal-email-input" />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSendOpen(null)}>{t("portal.cancel")}</Button>
            <Button onClick={doSend} disabled={sending} data-testid="portal-send-confirm">
              <Mail size={14} className="mr-1" />{t("portal.sendNow")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
