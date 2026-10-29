import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { Activity, AlertTriangle, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import PlanGate from "@/components/PlanGate";
function PulseInner() {
  const {
    user
  } = useAuth();
  const [loaded, setLoaded] = useState(false);
  const [data, setData] = useState(null);
  const reload = () => api.get("/pulse").then(setData).catch(e => toast.error(errMsg(e)));
  useEffect(() => {
    reload();
    setLoaded(true);
  }, []);
  if (!loaded) return <Loading />;
  const t = data?.today_pnl || {};
  const d = data?.deltas || {};
  return <div className="space-y-6">
      <PageHeader title={t("pulse.profitPulse")} subtitle={`Live margins and today's story — ${data?.weekday || ""}.`} actions={<Button variant="outline" onClick={reload}><RefreshCw size={14} className="me-1" />{t("pulse.refresh")}</Button>} />
      {data?.narration && <Card className="p-5">
          <div className="flex items-start gap-3">
            <Activity size={20} className="text-emerald-400 mt-0.5 shrink-0" />
            <p className="text-[15px] leading-relaxed">{data.narration}</p>
          </div>
        </Card>}
      <div className="grid grid-cols-3 gap-4">
        <Card className="p-4"><div className="text-xs text-gray-500">{t("pulse.todaySRevenue")}</div>
          <div className="text-2xl font-semibold">{money(t.revenue_cents ?? 0)}</div>
          <div className={`text-xs ${d.revenue_cents >= 0 ? "text-emerald-400" : "text-red-400"}`}>
            {d.revenue_cents >= 0 ? "+" : ""}{money(d.revenue_cents ?? 0)} {t("pulse.vsLast")} {data?.weekday}</div></Card>
        <Card className="p-4"><div className="text-xs text-gray-500">{t("pulse.todaySNet")}</div>
          <div className={`text-2xl font-semibold ${(t.net_cents ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>{money(t.net_cents ?? 0)}</div>
          <div className={`text-xs ${d.net_cents >= 0 ? "text-emerald-400" : "text-red-400"}`}>
            {d.net_cents >= 0 ? "+" : ""}{money(d.net_cents ?? 0)} {t("pulse.vsLast")} {data?.weekday}</div></Card>
        <Card className="p-4"><div className="text-xs text-gray-500">{t("pulse.jobsUnderwater")}</div>
          <div className={`text-2xl font-semibold ${data?.underwater_count ?? 0 ? "text-red-400" : "text-emerald-400"}`}>{data?.underwater_count ?? 0}</div>
          <div className="text-xs text-gray-500">{(data?.tickers || []).length} {t("pulse.activeJobsTracked")}</div></Card>
      </div>
      {!!(data?.underwater || []).length && <Card className="p-4 border-red-500/30">
          <div className="flex items-center gap-2 text-red-400 font-medium text-sm mb-2"><AlertTriangle size={15} /> {t("pulse.underwaterActNow")}</div>
          {(data.underwater || []).map(u => <div key={u.job_id} className="flex justify-between py-1.5 border-b border-[#1a2030] text-sm">
              <span>{u.job_number} · {u.client}</span>
              <span className="font-mono text-red-400">{money(u.gross_profit_cents)} ({u.margin_pct}%)</span>
            </div>)}
        </Card>}
      <Card className="p-0 overflow-hidden">
        <div className="px-4 py-3 text-sm font-medium border-b border-[#222838]">{t("pulse.liveJobMargins")}</div>
        <table className="w-full text-sm">
          <thead><tr className="text-start text-gray-500 border-b border-[#222838]">
            <th className="px-4 py-2 font-medium">{t("pulse.job")}</th><th className="px-4 py-2 font-medium text-end">{t("pulse.revenue")}</th>
            <th className="px-4 py-2 font-medium text-end">{t("pulse.cost")}</th><th className="px-4 py-2 font-medium text-end">{t("pulse.margin")}</th>
            <th className="px-4 py-2 font-medium"></th>
          </tr></thead>
          <tbody>
            {(data?.tickers || []).map(j => <tr key={j.job_id} className="border-b border-[#1a2030] hover:bg-[#141926]">
                <td className="px-4 py-2.5">{j.job_number} <span className="text-gray-500 text-xs">· {j.client}</span></td>
                <td className="px-4 py-2.5 text-end font-mono">{money(j.revenue_cents)}</td>
                <td className="px-4 py-2.5 text-end font-mono">{money(j.cost_cents)}</td>
                <td className={`px-4 py-2.5 text-end font-mono ${j.margin_pct < 0 ? "text-red-400" : j.margin_pct < 20 ? "text-amber-400" : "text-emerald-400"}`}>{j.margin_pct}%</td>
                <td className="px-4 py-2.5"><div className="w-24 h-1.5 bg-[#1a2030] rounded-full overflow-hidden">
                  <div className={`h-full rounded-full ${j.margin_pct < 0 ? "bg-red-500" : j.margin_pct < 20 ? "bg-amber-500" : "bg-emerald-500"}`} style={{
                  width: `${Math.min(100, Math.max(0, j.margin_pct))}%`
                }} /></div></td>
              </tr>)}
            {!(data?.tickers || []).length && <tr><td colSpan={5} className="px-4 py-8 text-center text-gray-500 text-sm">{t("pulse.noActiveJobsRightNow")}</td></tr>}
          </tbody>
        </table>
      </Card>
    </div>;
}

export default function Pulse() {
  return <PlanGate feature="advanced_reports"><PulseInner /></PlanGate>;
}
