import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { toast } from "sonner";
import { Building2 } from "lucide-react";

/** Multi-entity: every business you own, plus one consolidated view. */
import { useTranslation } from "react-i18next";
import PlanGate from "@/components/PlanGate";
function EntitiesInner() {
  const {
    t
  } = useTranslation();
  const [loaded, setLoaded] = useState(false);
  const [con, setCon] = useState(null);
  useEffect(() => {
    api.get("/entities/consolidated").then(setCon).catch(e => toast.error(errMsg(e)));
    setLoaded(true);
  }, []);
  if (!loaded) return <Loading />;
  return <div className="space-y-6">
      <PageHeader title={t("entities.entities")} subtitle={`One login, ${con?.entity_count || 0} ${con?.entity_count === 1 ? "business" : "businesses"} — combined view.`} />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Card className="p-4"><div className="text-xs text-gray-500">{t("entities.combinedRevenue")}</div><div className="text-2xl font-semibold text-emerald-400">{money(con?.total_revenue_cents || 0)}</div></Card>
        <Card className="p-4"><div className="text-xs text-gray-500">{t("entities.combinedExpenses")}</div><div className="text-2xl font-semibold text-rose-400">{money(con?.total_expense_cents || 0)}</div></Card>
        <Card className="p-4"><div className="text-xs text-gray-500">{t("entities.combinedNet")}</div><div className="text-2xl font-semibold">{money(con?.net_cents || 0)}</div></Card>
        <Card className="p-4"><div className="text-xs text-gray-500">{t("entities.combinedCash")}</div><div className="text-2xl font-semibold text-blue-400">{money(con?.combined_cash_cents || 0)}</div></Card>
      </div>
      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead><tr className="text-left text-gray-500 border-b border-[#222838]">
            <th className="px-4 py-3 font-medium">{t("entities.business")}</th><th className="px-4 py-3 font-medium text-right">{t("entities.revenue")}</th>
            <th className="px-4 py-3 font-medium text-right">{t("entities.net")}</th><th className="px-4 py-3 font-medium text-right">{t("entities.cash")}</th>
          </tr></thead>
          <tbody>
            {(con?.entities || []).map(e => <tr key={e.tenant_id} className="border-b border-[#1a2030]">
                <td className="px-4 py-3"><div className="flex items-center gap-2"><Building2 size={14} className="text-gray-500" /><span className="font-medium">{e.name}</span></div></td>
                <td className="px-4 py-3 text-right font-mono">{money(e.revenue_cents)}</td>
                <td className={`px-4 py-3 text-right font-mono ${e.net_cents >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{money(e.net_cents)}</td>
                <td className="px-4 py-3 text-right font-mono">{money(e.cash_cents)}</td>
              </tr>)}
          </tbody>
        </table>
      </Card>
      {(con?.intercompany_review || []).length > 0 && <Card className="p-5 border-amber-500/30">
          <div className="text-sm font-medium mb-2 text-amber-400">{t("entities.intercompanyReview")}</div>
          <p className="text-xs text-gray-500 mb-2">{t("entities.theseAccountsLookIntercompanyReviewBefore")}</p>
          {(con.intercompany_review || []).map(l => <div key={l.code} className="flex justify-between text-xs py-1 border-b border-[#1a2030]">
              <span className="text-gray-400">{l.code} · {l.name}</span>
              <span className="font-mono">{money(l.amount_cents)}</span>
            </div>)}
        </Card>}
      <p className="text-[11px] text-gray-600">{t("entities.consolidationIsReadOnlyEachEntity")}</p>
    </div>;
}

export default function Entities() {
  return <PlanGate feature="multi_entity"><EntitiesInner /></PlanGate>;
}
