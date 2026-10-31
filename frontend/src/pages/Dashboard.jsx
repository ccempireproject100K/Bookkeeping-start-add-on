import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { dollars, money, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import SetupChecklist from "@/components/SetupChecklist";
import DataQualityPanel from "@/components/DataQualityPanel";
import { Wallet, TrendingUp, TrendingDown, DollarSign, ArrowDownLeft, ArrowUpRight, Package } from "lucide-react";
import { useTranslation } from "react-i18next";
const TILES = [{
  key: "cash",
  label: "Cash on Hand",
  icon: Wallet,
  testid: "stat-tile-cash",
  tone: "gold"
}, {
  key: "revenue",
  label: "Revenue (MTD)",
  icon: TrendingUp,
  testid: "stat-tile-revenue",
  tone: "emerald"
}, {
  key: "expenses",
  label: "Expenses (MTD)",
  icon: TrendingDown,
  testid: "stat-tile-expenses",
  tone: "rose"
}, {
  key: "net_income",
  label: "Net Income (MTD)",
  icon: DollarSign,
  testid: "stat-tile-net-income",
  tone: "auto"
}, {
  key: "ar",
  label: "Accounts Receivable",
  icon: ArrowDownLeft,
  testid: "stat-tile-ar",
  tone: "blue"
}, {
  key: "ap",
  label: "Accounts Payable",
  icon: ArrowUpRight,
  testid: "stat-tile-ap",
  tone: "amber"
}, {
  key: "inventory",
  label: "Inventory Value",
  icon: Package,
  testid: "stat-tile-inventory-val",
  tone: "gray"
}];
const TONE = {
  gold: "text-amber-400",
  emerald: "text-emerald-400",
  rose: "text-rose-400",
  blue: "text-sky-400",
  amber: "text-amber-300",
  gray: "text-gray-200"
};
export default function Dashboard() {
  const { t } = useTranslation();
  const [data, setData] = useState(null);
  const [loadError, setLoadError] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  useEffect(() => {
    setLoadError(false);
    api.get("/dashboard").then(setData).catch(() => setLoadError(true));
  }, [retryKey]);
  if (loadError) return <div className="animate-rise" data-testid="dashboard-error">
      <PageHeader title={t("dashboard.dashboard")} testid="dashboard-page" />
      <Card className="p-8 text-center">
        <p className="text-gray-300">{t("dashboard.loadError")}</p>
        <button onClick={() => setRetryKey(k => k + 1)} data-testid="dashboard-retry"
          className="mt-4 px-4 py-2 rounded-lg bg-amber-500/20 border border-amber-500/40 text-amber-300 hover:bg-amber-500/30">
          {t("dashboard.retry")}
        </button>
      </Card>
    </div>;
  if (!data) return <Loading />;
  // tiles may be missing if the API returns an error/partial shape — default to {}
  // so the page renders zeros instead of throwing.
  const tiles = data.tiles || {};
  return <div className="animate-rise">
      <PageHeader title={t("dashboard.dashboard")} subtitle={`Ledger snapshot • as of ${fmtDate(data.as_of)}`} testid="dashboard-page" />
      <SetupChecklist />
      <DataQualityPanel />

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        {TILES.map(tile => {
        const val = tiles[tile.key];
        const tone = tile.tone === "auto" ? val >= 0 ? "emerald" : "rose" : tile.tone;
        return <Card key={tile.key} data-testid={tile.testid} className="hover:border-[#34404f] transition-colors">
              <div className="flex items-start justify-between">
                <span className="text-xs uppercase tracking-wider text-gray-500">{tile.label}</span>
                <tile.icon size={18} className={TONE[tone]} />
              </div>
              <div className={`mt-3 font-mono text-2xl font-semibold ${TONE[tone]}`}>
                {dollars(val)}
              </div>
            </Card>;
      })}
        <Card className="bg-gradient-to-br from-amber-500/10 to-transparent border-amber-500/30">
          <span className="text-xs uppercase tracking-wider text-amber-500/90">{t("dashboard.accountingEquation")}</span>
          <div className="mt-3 text-sm text-gray-300 font-mono leading-relaxed">{t("dashboard.assetsLiabilities")}<br />{t("dashboard.equity")}</div>
          <div className="mt-2 text-[11px] text-emerald-400">{t("dashboard.balancedToTheCent")}</div>
        </Card>
      </div>

      <Card className="p-0 overflow-hidden">
        <div className="px-5 py-4 border-b border-[#222838] flex items-center justify-between">
          <h3 className="font-display font-semibold">{t("dashboard.recentJournalEntries")}</h3>
          <span className="text-xs text-gray-500">{data.month}</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="dashboard-recent-entries">
            <thead>
              <tr className="text-start text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-5 py-3 font-medium">{t("dashboard.number")}</th>
                <th className="px-5 py-3 font-medium">{t("dashboard.date")}</th>
                <th className="px-5 py-3 font-medium">{t("dashboard.source")}</th>
                <th className="px-5 py-3 font-medium">{t("dashboard.memo")}</th>
                <th className="px-5 py-3 font-medium text-end">{t("dashboard.amount")}</th>
              </tr>
            </thead>
            <tbody>
              {(data.recent_entries || []).map(e => <tr key={e.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                  <td className="px-5 py-3 font-mono text-amber-400">{e.number}</td>
                  <td className="px-5 py-3 text-gray-400">{fmtDate(e.entry_date)}</td>
                  <td className="px-5 py-3">
                    <span className="px-2 py-0.5 rounded-full text-xs bg-[#222838] text-gray-300 capitalize">
                      {e.source?.replace("_", " ")}
                    </span>
                  </td>
                  <td className="px-5 py-3 text-gray-400 max-w-[280px] truncate">{e.memo || "—"}</td>
                  <td className="px-5 py-3 text-end font-mono text-gray-200">{money(e.total_cents)}</td>
                </tr>)}
              {(!data.recent_entries || data.recent_entries.length === 0) && <tr><td colSpan={5} className="px-5 py-12 text-center text-gray-500">{t("dashboard.noEntriesYet")}</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>
    </div>;
}
