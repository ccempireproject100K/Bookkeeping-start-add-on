import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { Users, TrendingUp, TrendingDown, Minus } from "lucide-react";
import { useTranslation } from "react-i18next";
import PlanGate from "@/components/PlanGate";
const POS_CLS = {
  above: "text-emerald-400",
  typical: "text-amber-400",
  below: "text-rose-400"
};
const POS_ICON = {
  above: TrendingUp,
  typical: Minus,
  below: TrendingDown
};
const POS_TXT = {
  above: "Above most shops",
  typical: "In the typical range",
  below: "Below most shops"
};
function BenchmarksInner() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const isOwner = user?.role === "owner";
  const [loaded, setLoaded] = useState(false);
  const [status, setStatus] = useState(null);
  const [data, setData] = useState(null);
  const reload = () => {
    api.get("/benchmarks/status").then(setStatus).catch(() => {});
    api.get("/benchmarks").then(setData).catch(e => toast.error(errMsg(e)));
  };
  useEffect(() => {
    reload();
    setLoaded(true);
  }, []);
  const toggle = async () => {
    try {
      await api.post("/benchmarks/opt-in", {
        opt_in: !status.opt_in
      });
      toast.success(status.opt_in ? "Opted out" : "Opted in — your anonymized KPIs help the pool");
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const contribute = async () => {
    try {
      await api.post("/benchmarks/contribute", {});
      toast.success(t("benchmarks.contributedThisMonthSSnapshot"));
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const fmt = (v, unit) => unit === "$" ? `$${Number(v).toLocaleString()}` : `${v}${unit === "%" ? "%" : unit ? " " + unit : ""}`;
  if (!loaded) return <Loading />;
  return <div className="space-y-6">
      <PageHeader title={t("benchmarks.benchmarks")} subtitle={`How you compare to ${data?.trade || "similar"} shops — fully anonymized.`} actions={isOwner && status && <div className="flex gap-2">
            <Button variant="outline" onClick={toggle} data-testid="bench-optin">
              {status.opt_in ? "Opt out" : "Opt in"}
            </Button>
            {status.opt_in && !status.contributed_this_month && <Button onClick={contribute} data-testid="bench-contribute">{t("benchmarks.contributeThisMonth")}</Button>}
          </div>} />
      {status && <Card className="p-4 flex items-center gap-3 text-sm">
          <Users size={16} className="text-blue-400 shrink-0" />
          <p className="text-gray-300">
            {status.pool_size >= status.min_pool ? `${status.pool_size} ${data?.trade || ""} shops in this month's pool — comparisons are live.` : `Pool building: ${status.pool_size}/${status.min_pool} shops needed before comparisons unlock. Your numbers stay private until then.`}
          </p>
        </Card>}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {(data?.cards || []).map(c => {
        const Icon = POS_ICON[c.position] || Minus;
        return <Card key={c.id} className="p-5">
              <div className="flex items-center justify-between mb-2">
                <div className="text-sm font-medium">{c.label}</div>
                {c.ready && <span className={`text-xs flex items-center gap-1 ${POS_CLS[c.position]}`}>
                    <Icon size={13} /> {POS_TXT[c.position]}
                  </span>}
              </div>
              {c.ready ? <div className="space-y-2">
                  <div className="flex items-end justify-between text-xs text-gray-500">
                    <span>{t("benchmarks.bottom25")} {fmt(c.p25, c.unit)}</span>
                    <span>{t("benchmarks.median")} {fmt(c.median, c.unit)}</span>
                    <span>{t("benchmarks.top25")} {fmt(c.p75, c.unit)}</span>
                  </div>
                  <div className="relative h-2 rounded-full bg-[#222838]">
                    <div className="absolute inset-y-0 left-1/4 right-1/4 rounded-full bg-blue-500/40" />
                    {c.mine != null && (() => {
                const lo = Math.min(c.p25, c.mine),
                  hi = Math.max(c.p75, c.mine);
                const pct = hi === lo ? 50 : Math.min(100, Math.max(0, (c.mine - lo) / (hi - lo) * 100));
                return <div className="absolute top-1/2 -translate-y-1/2 w-3.5 h-3.5 rounded-full bg-amber-400 border-2 border-black" style={{
                  left: `calc(${pct}% - 7px)`
                }} title={`You: ${fmt(c.mine, c.unit)}`} />;
              })()}
                  </div>
                  <p className="text-xs text-gray-400">{t("benchmarks.you")} <span className="font-medium text-gray-200">{c.mine != null ? fmt(c.mine, c.unit) : "—"}</span> · {c.pool} {t("benchmarks.shops")}</p>
                </div> : <p className="text-xs text-gray-600">{t("benchmarks.unlocksWhen")} {data.min_pool} {t("benchmarks.shopsContributeThisMonth")}{c.pool} {t("benchmarks.soFarYourDataStaysPrivate")}</p>}
              {c.hint && <p className="text-xs text-gray-600 mt-2">{c.hint}</p>}
            </Card>;
      })}
      </div>
      <p className="text-[11px] text-gray-600">{t("benchmarks.benchmarksAreAggregatesMedianQuartilesFrom")}</p>
    </div>;
}

export default function Benchmarks() {
  return <PlanGate feature="advanced_reports"><BenchmarksInner /></PlanGate>;
}
