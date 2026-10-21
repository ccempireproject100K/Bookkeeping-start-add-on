import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { toast } from "sonner";
import { KeyRound, Copy, Ban, Plus, Webhook, Send, Trash2, BookOpen } from "lucide-react";
import { fmtDate } from "@/lib/format";

const DELIVERY_TONE = {
  delivered: "bg-emerald-500/15 text-emerald-400",
  pending: "bg-amber-500/15 text-amber-400",
  failed: "bg-rose-500/15 text-rose-400",
};

function CopyRow({ value }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2">
      <code className="flex-1 break-all rounded bg-black/40 px-2 py-1 text-xs">{value}</code>
      <Button size="sm" variant="outline" onClick={() => { navigator.clipboard.writeText(value); setCopied(true); }}>
        <Copy className="h-3 w-3" /> {copied ? "Copied" : "Copy"}
      </Button>
    </div>
  );
}

export default function Developers() {
  const [keys, setKeys] = useState(null);
  const [hooks, setHooks] = useState(null);
  const [deliveries, setDeliveries] = useState([]);
  const [events, setEvents] = useState([]);
  const [keyLabel, setKeyLabel] = useState("");
  const [newKey, setNewKey] = useState(null);       // shown once
  const [newSecret, setNewSecret] = useState(null); // shown once
  const [hookForm, setHookForm] = useState({ url: "", events: [] });

  const reload = () => {
    api.get("/developers/keys").then(setKeys).catch(() => setKeys([]));
    api.get("/developers/webhooks").then(setHooks).catch(() => setHooks([]));
    api.get("/developers/deliveries", { params: { limit: 50 } }).then((r) => setDeliveries(r.deliveries || r || [])).catch(() => {});
    api.get("/developers/events").then((r) => setEvents(r.events || r || [])).catch(() => {});
  };
  useEffect(reload, []);

  const createKey = async () => {
    try {
      const res = await api.post("/developers/keys", { label: keyLabel || "API key" });
      setNewKey(res);
      setKeyLabel("");
      reload();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const revokeKey = async (id) => {
    if (!window.confirm("Revoke this API key? Anything using it stops working immediately.")) return;
    try { await api.post(`/developers/keys/${id}/revoke`); toast.success("Key revoked"); reload(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const toggleEvent = (ev) => setHookForm((f) => ({
    ...f, events: f.events.includes(ev) ? f.events.filter((x) => x !== ev) : [...f.events, ev],
  }));

  const createHook = async () => {
    if (!hookForm.url || hookForm.events.length === 0) { toast.error("Give the webhook a URL and at least one event."); return; }
    try {
      const res = await api.post("/developers/webhooks", hookForm);
      setNewSecret(res);
      setHookForm({ url: "", events: [] });
      reload();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const deleteHook = async (id) => {
    if (!window.confirm("Delete this webhook subscription?")) return;
    try { await api.del(`/developers/webhooks/${id}`); toast.success("Webhook deleted"); reload(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const testHook = async (id) => {
    try {
      const res = await api.post(`/developers/webhooks/${id}/test`);
      toast.success(`Test event ${res.status || "sent"}`);
      reload();
    } catch (e) { toast.error(errMsg(e)); }
  };

  if (keys === null || hooks === null) return <Loading />;

  return (
    <div>
      <PageHeader title="Developers" subtitle="API keys and webhooks for connecting your own tools to this workspace. Keys and signing secrets are shown exactly once — store them somewhere safe." />

      <Card>
        <div className="mb-3 flex items-center gap-2 font-semibold"><KeyRound className="h-4 w-4" /> API keys</div>
        <div className="mb-3 flex items-end gap-2">
          <div className="flex-1">
            <Label>Label for a new key</Label>
            <Input value={keyLabel} onChange={(e) => setKeyLabel(e.target.value)} placeholder="e.g. My reporting script" />
          </div>
          <Button onClick={createKey}><Plus className="h-4 w-4" /> Create key</Button>
        </div>
        {keys.length === 0 && <p className="text-sm text-gray-400">No API keys yet.</p>}
        <div className="space-y-2">
          {keys.map((k) => (
            <div key={k.id} className="flex items-center justify-between rounded border border-white/10 px-3 py-2 text-sm">
              <div>
                <div className="font-medium">{k.label} {k.revoked_at && <span className="text-rose-400">(revoked)</span>}</div>
                <div className="text-xs text-gray-400"><code>{k.key_stub}</code> · created {fmtDate(k.created_at)} · last used {k.last_used_at ? fmtDate(k.last_used_at) : "never"}</div>
              </div>
              {!k.revoked_at && <Button size="sm" variant="outline" onClick={() => revokeKey(k.id)}><Ban className="h-3 w-3" /> Revoke</Button>}
            </div>
          ))}
        </div>
      </Card>

      <div className="mt-4" />
      <Card>
        <div className="mb-3 flex items-center gap-2 font-semibold"><Webhook className="h-4 w-4" /> Webhooks</div>
        <div className="space-y-2">
          <div>
            <Label>Endpoint URL (https)</Label>
            <Input value={hookForm.url} onChange={(e) => setHookForm({ ...hookForm, url: e.target.value })} placeholder="https://example.com/hooks/books" />
          </div>
          <div>
            <Label>Events</Label>
            <div className="mt-1 flex flex-wrap gap-2">
              {events.map((ev) => {
                const name = typeof ev === "string" ? ev : ev.event;
                return (
                  <Button key={name} size="sm" variant={hookForm.events.includes(name) ? "default" : "outline"} onClick={() => toggleEvent(name)}>
                    {name}
                  </Button>
                );
              })}
            </div>
          </div>
          <Button onClick={createHook}><Plus className="h-4 w-4" /> Add webhook</Button>
        </div>
        <div className="mt-4 space-y-2">
          {hooks.length === 0 && <p className="text-sm text-gray-400">No webhook subscriptions yet.</p>}
          {hooks.map((h) => (
            <div key={h.id} className="flex items-center justify-between rounded border border-white/10 px-3 py-2 text-sm">
              <div>
                <div className="font-medium break-all">{h.url}</div>
                <div className="text-xs text-gray-400">{(h.events || []).join(", ")}</div>
              </div>
              <div className="flex gap-2">
                <Button size="sm" variant="outline" onClick={() => testHook(h.id)}><Send className="h-3 w-3" /> Send test</Button>
                <Button size="sm" variant="outline" onClick={() => deleteHook(h.id)}><Trash2 className="h-3 w-3" /> Delete</Button>
              </div>
            </div>
          ))}
        </div>
        {deliveries.length > 0 && (
          <div className="mt-4">
            <div className="mb-2 text-sm font-medium">Recent deliveries</div>
            <div className="space-y-1 text-xs">
              {deliveries.map((d) => (
                <div key={d.id} className="flex items-center justify-between rounded bg-black/20 px-2 py-1">
                  <span>{d.event} · {fmtDate(d.created_at)}</span>
                  <span className={`rounded-full px-2 py-0.5 ${DELIVERY_TONE[d.status] || ""}`}>{d.status} · {d.attempts} attempt(s){d.last_status ? ` · HTTP ${d.last_status}` : ""}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>

      <div className="mt-4" />
      <Card>
        <div className="mb-2 flex items-center gap-2 font-semibold"><BookOpen className="h-4 w-4" /> How it works</div>
        <div className="space-y-2 text-sm text-gray-300">
          <p><span className="font-medium text-gray-100">Authenticate</span> with your key on every request: <code>Authorization: Bearer maek_…</code> (or the <code>X-API-Key</code> header). Keys are read-only in v1: <code>GET /api/developers/v1/whoami</code>, <code>GET /api/developers/v1/invoices</code>, <code>GET /api/developers/v1/customers</code>. Amounts are integer cents.</p>
          <p><span className="font-medium text-gray-100">Webhooks</span> are POSTed as JSON: <code>{"{ id, event, tenant_id, created_at, data }"}</code>. Verify the <code>X-MAE-Signature-256</code> header — it is <code>sha256=</code> plus the HMAC-SHA256 hex of the exact request body, keyed with your subscription's signing secret. Deliveries retry up to 3 attempts with backoff (about 30s, then 60s); every attempt appears in the log above.</p>
          <p><span className="font-medium text-gray-100">Events</span> fire when things happen in this workspace: {events.map((e) => (typeof e === "string" ? e : e.event)).join(", ") || "invoice.created, invoice.paid, payment.received, customer.created, webhook.test"}.</p>
        </div>
      </Card>

      <Dialog open={!!newKey} onOpenChange={(o) => !o && setNewKey(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Your new API key — copy it now</DialogTitle></DialogHeader>
          <p className="text-sm text-amber-400">This is the only time the full key is shown. It is stored hashed and cannot be recovered.</p>
          {newKey && <CopyRow value={newKey.key || newKey.api_key || ""} />}
          <DialogFooter><Button onClick={() => setNewKey(null)}>Done</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!newSecret} onOpenChange={(o) => !o && setNewSecret(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Webhook signing secret — copy it now</DialogTitle></DialogHeader>
          <p className="text-sm text-amber-400">Use this secret to verify the X-MAE-Signature-256 header on every delivery. It is shown only once.</p>
          {newSecret && <CopyRow value={newSecret.signing_secret || ""} />}
          <DialogFooter><Button onClick={() => setNewSecret(null)}>Done</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
