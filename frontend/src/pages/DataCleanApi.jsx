import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  KeyRound, Plus, Copy, Check, Ban, Activity, BookOpen, Loader2, X,
} from "lucide-react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { fmtDateTime, num } from "@/lib/format";
import {
  API_ENDPOINTS, ERROR_CODES, barFraction, keyStatus, usageSeries,
} from "@/lib/datacleanApi";

const selectCls = "h-9 rounded-md border border-[#222838] bg-[#14171E] px-2.5 text-sm text-gray-200 focus:outline-none focus:ring-1 focus:ring-amber-500/60";
const inputCls = "h-9 rounded-md border border-[#222838] bg-[#10131a] px-3 text-sm text-gray-100 focus:outline-none focus:ring-1 focus:ring-amber-500/60";
const chip = "px-2 py-0.5 rounded-full text-[11px]";

function SkeletonCards({ count = 3 }) {
  return <div className="space-y-3" aria-hidden="true">
    {Array.from({ length: count }).map((_, i) => (
      <div key={i} className="card-surface p-5 animate-pulse motion-reduce:animate-none">
        <div className="h-4 w-1/3 rounded bg-[#222838] mb-3" />
        <div className="h-3 w-2/3 rounded bg-[#1a1f2b]" />
      </div>))}
  </div>;
}

/* ============================== KEYS TAB ============================== */

function KeysTab() {
  const { t } = useTranslation();
  const [keys, setKeys] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [label, setLabel] = useState("");
  const [perMinute, setPerMinute] = useState("");
  const [perDay, setPerDay] = useState("");
  const [quota, setQuota] = useState("");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState(null); // {key, ...} — shown once
  const [copied, setCopied] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState(null);
  const [revoking, setRevoking] = useState(false);

  const load = useCallback(async () => {
    setLoading(true); setLoadFailed(false);
    try { setKeys(await api.get("/dataclean/keys")); }
    catch (e) { setLoadFailed(true); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const create = async () => {
    setCreating(true);
    try {
      const body = { label: label.trim() || undefined };
      if (perMinute) body.rate_limit_per_minute = Number(perMinute);
      if (perDay) body.rate_limit_per_day = Number(perDay);
      if (quota) body.monthly_row_quota = Number(quota);
      const out = await api.post("/dataclean/keys", body);
      setCreated(out); setCopied(false);
      setLabel(""); setPerMinute(""); setPerDay(""); setQuota("");
      await load();
    } catch (e) { toast.error(errMsg(e)); }
    finally { setCreating(false); }
  };

  const copyKey = async () => {
    try {
      await navigator.clipboard.writeText(created.key);
      setCopied(true);
    } catch (e) { toast.error(errMsg(e)); }
  };

  const revoke = async () => {
    if (!revokeTarget) return;
    setRevoking(true);
    try {
      await api.post(`/dataclean/keys/${revokeTarget.id}/revoke`);
      toast.success(t("datacleanApi.revokedToast"));
      setRevokeTarget(null);
      await load();
    } catch (e) { toast.error(errMsg(e)); }
    finally { setRevoking(false); }
  };

  return <div className="space-y-5">
    <Card>
      <h3 className="font-display font-semibold flex items-center gap-2">
        <Plus size={16} className="text-amber-400" />{t("datacleanApi.createTitle")}
      </h3>
      <div className="mt-4 grid grid-cols-1 md:grid-cols-4 gap-3">
        <div className="md:col-span-1">
          <label className="block text-xs text-gray-500 mb-1.5" htmlFor="dc-label">{t("datacleanApi.label")}</label>
          <input id="dc-label" data-testid="dc-key-label" className={`${inputCls} w-full`} value={label}
            placeholder={t("datacleanApi.labelPlaceholder")} onChange={(e) => setLabel(e.target.value)} />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1.5" htmlFor="dc-pm">{t("datacleanApi.limitPerMinute")}</label>
          <input id="dc-pm" data-testid="dc-key-pm" type="number" min="1" className={`${inputCls} w-full`}
            value={perMinute} placeholder={t("datacleanApi.limitDefault")} onChange={(e) => setPerMinute(e.target.value)} />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1.5" htmlFor="dc-pd">{t("datacleanApi.limitPerDay")}</label>
          <input id="dc-pd" data-testid="dc-key-pd" type="number" min="1" className={`${inputCls} w-full`}
            value={perDay} placeholder={t("datacleanApi.limitDefault")} onChange={(e) => setPerDay(e.target.value)} />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1.5" htmlFor="dc-quota">{t("datacleanApi.limitQuota")}</label>
          <input id="dc-quota" data-testid="dc-key-quota" type="number" min="0" className={`${inputCls} w-full`}
            value={quota} placeholder={t("datacleanApi.limitDefault")} onChange={(e) => setQuota(e.target.value)} />
        </div>
      </div>
      <div className="mt-4">
        <Button data-testid="dc-key-create" onClick={create} disabled={creating}>
          {creating ? <Loader2 size={15} className="animate-spin motion-reduce:animate-none" /> : <KeyRound size={15} />}
          {creating ? t("datacleanApi.creating") : t("datacleanApi.createKey")}
        </Button>
      </div>
    </Card>

    {loading && <SkeletonCards count={2} />}
    {!loading && loadFailed && <Card className="flex items-center justify-between gap-4">
      <p className="text-sm text-gray-400">{t("datacleanApi.loadError")}</p>
      <Button variant="outline" size="sm" onClick={load}>{t("datacleanApi.retry")}</Button>
    </Card>}
    {!loading && !loadFailed && keys.length === 0 &&
      <Card className="p-10 text-center" data-testid="dc-keys-empty">
        <KeyRound size={22} className="mx-auto text-gray-600" />
        <p className="mt-3 text-sm text-gray-500">{t("datacleanApi.noKeys")}</p>
      </Card>}
    <div className="space-y-3">
      {keys.map((k) => {
        const status = keyStatus(k);
        return <article key={k.id} data-testid={`dc-key-${k.id}`}
          className="card-surface p-5 animate-rise motion-reduce:animate-none">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h3 className="font-display font-semibold truncate">{k.label}</h3>
              <p className="font-mono text-xs text-amber-300/90 mt-1">{k.key_stub}</p>
            </div>
            <span className={`${chip} ${status === "active" ? "bg-emerald-500/10 text-emerald-300" : "bg-[#222838] text-gray-400"}`}>
              {status === "active" ? t("datacleanApi.statusActive") : t("datacleanApi.statusRevoked")}
            </span>
          </div>
          <div className="mt-3 grid grid-cols-2 lg:grid-cols-4 gap-3 text-sm">
            <div><div className="text-xs text-gray-500">{t("datacleanApi.colCreated")}</div>
              <div className="text-gray-300 mt-0.5">{fmtDateTime(k.created_at)}</div></div>
            <div><div className="text-xs text-gray-500">{t("datacleanApi.colLastUsed")}</div>
              <div className="text-gray-300 mt-0.5">{k.last_used_at ? fmtDateTime(k.last_used_at) : "—"}</div></div>
            <div><div className="text-xs text-gray-500">{t("datacleanApi.colUsage")}</div>
              <div className="text-gray-300 mt-0.5">{t("datacleanApi.callsRows", { calls: num(k.calls_total), rows: num(k.rows_total) })}</div></div>
            <div><div className="text-xs text-gray-500">{t("datacleanApi.colLimits")}</div>
              <div className="text-gray-300 mt-0.5">{t("datacleanApi.limitsSummary", {
                perMinute: num(k.limits?.per_minute), perDay: num(k.limits?.per_day),
                quota: num(k.limits?.monthly_row_quota) })}</div></div>
          </div>
          {status === "active" && <div className="mt-4">
            <Button size="sm" variant="outline" data-testid={`dc-key-revoke-${k.id}`}
              onClick={() => setRevokeTarget(k)}>
              <Ban size={13} />{t("datacleanApi.revoke")}
            </Button>
          </div>}
        </article>;
      })}
    </div>

    <Dialog open={!!created} onOpenChange={(o) => { if (!o) setCreated(null); }}>
      <DialogContent className="bg-[#14171E] border-[#222838] text-gray-100">
        <DialogHeader>
          <DialogTitle>{t("datacleanApi.keyCreatedTitle")}</DialogTitle>
          <DialogDescription className="text-gray-400">{t("datacleanApi.keyCreatedBody")}</DialogDescription>
        </DialogHeader>
        {created && <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
          <code className="block font-mono text-sm text-amber-200 break-all" data-testid="dc-key-full">{created.key}</code>
        </div>}
        <DialogFooter>
          <Button variant="outline" onClick={copyKey} data-testid="dc-key-copy">
            {copied ? <Check size={14} /> : <Copy size={14} />}
            {copied ? t("datacleanApi.copied") : t("datacleanApi.copy")}
          </Button>
          <Button onClick={() => setCreated(null)}>{t("datacleanApi.close")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog open={!!revokeTarget} onOpenChange={(o) => { if (!o) setRevokeTarget(null); }}>
      <DialogContent className="bg-[#14171E] border-[#222838] text-gray-100">
        <DialogHeader>
          <DialogTitle>{t("datacleanApi.revokeTitle")}</DialogTitle>
          <DialogDescription className="text-gray-400">
            {t("datacleanApi.revokeBody")} {revokeTarget?.label} · {revokeTarget?.key_stub}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={() => setRevokeTarget(null)}>{t("datacleanApi.cancel")}</Button>
          <Button variant="destructive" data-testid="dc-key-revoke-confirm" onClick={revoke} disabled={revoking}>
            {revoking ? t("datacleanApi.loading") : t("datacleanApi.revokeConfirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>;
}

/* ============================= USAGE TAB ============================== */

function UsageTab() {
  const { t } = useTranslation();
  const [days, setDays] = useState(30);
  const [agg, setAgg] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);

  const load = useCallback(async () => {
    setLoading(true); setLoadFailed(false);
    try { setAgg(await api.get("/dataclean/usage", { params: { days } })); }
    catch (e) { setLoadFailed(true); }
    finally { setLoading(false); }
  }, [days]);
  useEffect(() => { load(); }, [load]);

  const series = useMemo(() => usageSeries(agg?.by_day), [agg]);

  if (loading) return <SkeletonCards count={3} />;
  if (loadFailed) return <Card className="flex items-center justify-between gap-4">
    <p className="text-sm text-gray-400">{t("datacleanApi.loadError")}</p>
    <Button variant="outline" size="sm" onClick={load}>{t("datacleanApi.retry")}</Button>
  </Card>;
  if (!agg) return null;

  return <div className="space-y-5">
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="grid grid-cols-2 gap-4">
        <Card className="min-w-[150px]">
          <div className="text-xs uppercase tracking-wider text-gray-500">{t("datacleanApi.totalCalls")}</div>
          <div className="mt-1 font-mono text-3xl font-semibold text-gray-100">{num(agg.totals?.calls)}</div>
        </Card>
        <Card className="min-w-[150px]">
          <div className="text-xs uppercase tracking-wider text-gray-500">{t("datacleanApi.totalRows")}</div>
          <div className="mt-1 font-mono text-3xl font-semibold text-gray-100">{num(agg.totals?.rows)}</div>
        </Card>
      </div>
      <div>
        <label className="block text-xs text-gray-500 mb-1.5" htmlFor="dc-days">{t("datacleanApi.period")}</label>
        <select id="dc-days" data-testid="dc-usage-days" className={selectCls} value={days}
          onChange={(e) => setDays(Number(e.target.value))}>
          <option value={7}>{t("datacleanApi.days7")}</option>
          <option value={30}>{t("datacleanApi.days30")}</option>
          <option value={90}>{t("datacleanApi.days90")}</option>
        </select>
      </div>
    </div>

    <p className="text-xs text-gray-500">
      {agg.tracking_since
        ? `${t("datacleanApi.trackingSince")} ${fmtDateTime(agg.tracking_since)}`
        : t("datacleanApi.trackingNone")}
    </p>

    {agg.totals?.calls === 0
      ? <Card className="p-10 text-center" data-testid="dc-usage-empty">
          <Activity size={22} className="mx-auto text-gray-600" />
          <p className="mt-3 text-sm text-gray-500">{t("datacleanApi.noUsage")}</p>
          <p className="mt-1 text-xs text-gray-600">{t("datacleanApi.noUsageHint")}</p>
        </Card>
      : <>
          <Card>
            <h3 className="font-display font-semibold">{t("datacleanApi.byDay")}</h3>
            <div className="mt-4 flex items-end gap-[3px] h-28" data-testid="dc-usage-chart">
              {series.map((d) => (
                <div key={d.date} className="flex-1 min-w-[3px] rounded-t bg-amber-500/70"
                  style={{ height: `${Math.max(barFraction(d.calls, series) * 100, d.calls ? 4 : 0)}%` }}
                  title={`${d.date} · ${d.calls} ${t("datacleanApi.calls")} · ${d.rows} ${t("datacleanApi.rows")}`} />
              ))}
            </div>
            <div className="mt-2 flex justify-between text-[11px] text-gray-600">
              <span>{series[0]?.date}</span><span>{series[series.length - 1]?.date}</span>
            </div>
          </Card>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <Card className="p-0 overflow-hidden">
              <div className="px-5 py-4 border-b border-[#222838]">
                <h3 className="font-display font-semibold">{t("datacleanApi.byEndpoint")}</h3>
              </div>
              <ul className="divide-y divide-[#171c26]">
                {Object.entries(agg.by_endpoint || {}).map(([ep, count]) => (
                  <li key={ep} className="px-5 py-3 flex justify-between gap-4 text-sm">
                    <span className="font-mono text-xs text-gray-300">{ep}</span>
                    <span className="text-gray-400">{num(count)}</span>
                  </li>))}
              </ul>
            </Card>
            <Card className="p-0 overflow-hidden">
              <div className="px-5 py-4 border-b border-[#222838]">
                <h3 className="font-display font-semibold">{t("datacleanApi.byKey")}</h3>
              </div>
              <ul className="divide-y divide-[#171c26]">
                {(agg.by_key || []).map((k) => (
                  <li key={k.key_id} className="px-5 py-3 flex justify-between gap-4 text-sm">
                    <span className="text-gray-300 truncate">{k.label || k.key_stub || k.key_id}</span>
                    <span className="text-gray-400 shrink-0">{t("datacleanApi.callsRows", { calls: num(k.calls), rows: num(k.rows) })}</span>
                  </li>))}
              </ul>
            </Card>
          </div>
        </>}
  </div>;
}

/* ============================== DOCS TAB ============================== */

const SHAPE_EXAMPLE = `{
  "shape": "dataclean.result/1",
  "original": "(512) 555-0134",
  "normalized": "+15125550134",
  "ok": true,
  "rule": "phone:libphonenumber-e164",
  "confidence": 0.98,
  "notes": []
}`;

const QUICKSTART = `curl -X POST https://bookkeeping-api-ymh6.onrender.com/api/v1/dataclean/cleanup/clean \\
  -H "X-API-Key: mdck_your_key_here" \\
  -H "Content-Type: application/json" \\
  -d '{"kind": "customers", "rows": [
        {"name": "Acme  Supply", "phone": "(512) 555-0134"}]}'`;

function DocsTab() {
  const { t } = useTranslation();
  const [pricing, setPricing] = useState(null);
  useEffect(() => {
    let live = true;
    api.get("/v1/dataclean/pricing").then((p) => live && setPricing(p)).catch(() => {});
    return () => { live = false; };
  }, []);
  const limits = pricing?.rate_limits || {};

  return <div className="space-y-5">
    <Card>
      <h3 className="font-display font-semibold flex items-center gap-2">
        <BookOpen size={16} className="text-amber-400" />{t("datacleanApi.docsAuthTitle")}
      </h3>
      <p className="mt-2 text-sm text-gray-400">{t("datacleanApi.docsAuthBody")}</p>
      <pre className="mt-3 rounded-lg bg-[#10131a] border border-[#222838] p-4 text-xs text-gray-300 overflow-x-auto">{QUICKSTART}</pre>
    </Card>

    <Card className="p-0 overflow-hidden">
      <div className="px-5 py-4 border-b border-[#222838]">
        <h3 className="font-display font-semibold">{t("datacleanApi.docsEndpoints")}</h3>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm" data-testid="dc-docs-endpoints">
          <tbody>
            {API_ENDPOINTS.map((e) => (
              <tr key={e.path} className="border-b border-[#171c26]">
                <td className="px-5 py-3 font-mono text-xs text-amber-300 whitespace-nowrap">{e.method}</td>
                <td className="px-5 py-3 font-mono text-xs text-gray-300 whitespace-nowrap">{e.path}</td>
                <td className="px-5 py-3 text-gray-400">{t(e.descKey)}</td>
                <td className="px-5 py-3 text-xs text-gray-500 whitespace-nowrap">
                  {e.auth === "key" ? t("datacleanApi.docsAuthKey") : t("datacleanApi.docsAuthNone")}
                  {e.metered ? ` · ${t("datacleanApi.docsMetered")}` : ""}
                </td>
              </tr>))}
          </tbody>
        </table>
      </div>
    </Card>

    <Card>
      <h3 className="font-display font-semibold">{t("datacleanApi.docsShapeTitle")}</h3>
      <p className="mt-2 text-sm text-gray-400">{t("datacleanApi.docsShapeBody")}</p>
      <pre className="mt-3 rounded-lg bg-[#10131a] border border-[#222838] p-4 text-xs text-gray-300 overflow-x-auto">{SHAPE_EXAMPLE}</pre>
    </Card>

    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <Card className="p-0 overflow-hidden">
        <div className="px-5 py-4 border-b border-[#222838]">
          <h3 className="font-display font-semibold">{t("datacleanApi.docsErrors")}</h3>
        </div>
        <ul className="divide-y divide-[#171c26]">
          {ERROR_CODES.map((e) => (
            <li key={e.code} className="px-5 py-3 text-sm">
              <div className="flex justify-between gap-4">
                <span className="font-mono text-xs text-rose-300">{e.code}</span>
                <span className="text-gray-500">{e.status}</span>
              </div>
              <p className="mt-1 text-xs text-gray-400">{t(e.descKey)}</p>
            </li>))}
        </ul>
      </Card>
      <Card>
        <h3 className="font-display font-semibold">{t("datacleanApi.docsLimitsTitle")}</h3>
        <p className="mt-2 text-sm text-gray-400">{t("datacleanApi.docsLimitsBody")}</p>
        <ul className="mt-3 space-y-2 text-sm text-gray-300">
          <li className="flex justify-between gap-4"><span>{t("datacleanApi.docsPerMinute")}</span>
            <span className="font-mono">{num(limits.per_minute)}</span></li>
          <li className="flex justify-between gap-4"><span>{t("datacleanApi.docsPerDay")}</span>
            <span className="font-mono">{num(limits.per_day)}</span></li>
          <li className="flex justify-between gap-4"><span>{t("datacleanApi.docsMonthlyQuota")}</span>
            <span className="font-mono">{num(limits.monthly_row_quota)}</span></li>
        </ul>
        <p className="mt-3 text-xs text-gray-600 flex items-center gap-1.5">
          <X size={12} className="shrink-0" />{t("datacleanApi.docsNoBilling")}
        </p>
      </Card>
    </div>
  </div>;
}

/* ================================ PAGE ================================ */

export default function DataCleanApi() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab = ["usage", "docs"].includes(params.get("tab")) ? params.get("tab") : "keys";
  const tabBtn = (key, label) => (
    <button key={key} role="tab" aria-selected={tab === key} data-testid={`dc-tab-${key}`}
      onClick={() => setParams(key === "keys" ? {} : { tab: key })}
      className={`px-4 py-2 rounded-lg text-sm transition-colors motion-reduce:transition-none ${tab === key ? "bg-amber-500/15 text-amber-300 border border-amber-500/40" : "text-gray-400 border border-transparent hover:text-gray-100"}`}>
      {label}
    </button>);
  return <div className="space-y-6 max-w-5xl mx-auto animate-rise motion-reduce:animate-none">
    <PageHeader title={t("datacleanApi.title")} subtitle={t("datacleanApi.subtitle")} testid="dataclean-api-page" />
    <div className="flex gap-2" role="tablist" aria-label={t("datacleanApi.title")}>
      {tabBtn("keys", t("datacleanApi.tabKeys"))}
      {tabBtn("usage", t("datacleanApi.tabUsage"))}
      {tabBtn("docs", t("datacleanApi.tabDocs"))}
    </div>
    {tab === "keys" ? <KeysTab /> : tab === "usage" ? <UsageTab /> : <DocsTab />}
  </div>;
}
