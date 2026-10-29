import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { dollars } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { AlertTriangle, TrendingUp, TrendingDown, DollarSign, Briefcase } from "lucide-react";
import { useTranslation } from "react-i18next";
const TABS = [{
  key: "job",
  label: "By Job"
}, {
  key: "customer",
  label: "By Customer"
}, {
  key: "service_type",
  label: "By Service Type"
}, {
  key: "technician",
  label: "By Technician"
}];
export default function JobProfitability() {
  const {
    t
  } = useTranslation();
  const [tab, setTab] = useState("job");
  const [data, setData] = useState(null);
  const [dash, setDash] = useState(null);
  useEffect(() => {
    api.get("/jobs/reports/dashboard").then(setDash);
  }, []);
  useEffect(() => {
    setData(null);
    api.get(`/jobs/reports/profitability?group_by=${tab}`).then(setData);
  }, [tab]);
  const marginCls = m => m < 0 ? "text-rose-400" : m < 20 ? "text-amber-400" : "text-emerald-400";
  return <div className="animate-rise">
      <PageHeader title={t("jobprofitability.jobProfitability")} subtitle="Where you make — and lose — money" testid="job-profitability-page" />

      {dash && <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-5" data-testid="job-dashboard-kpis">
            {[{
          l: "Total Gross Profit",
          v: dollars(dash.kpis.gross_profit),
          i: DollarSign,
          c: dash.kpis.gross_profit < 0 ? "text-rose-400" : "text-emerald-400"
        }, {
          l: "Avg Margin",
          v: `${dash.kpis.avg_margin}%`,
          i: TrendingUp,
          c: marginCls(dash.kpis.avg_margin)
        }, {
          l: "Profitable Jobs",
          v: dash.kpis.profitable_count,
          i: TrendingUp,
          c: "text-emerald-400"
        }, {
          l: "Unprofitable Jobs",
          v: dash.kpis.unprofitable_count,
          i: TrendingDown,
          c: dash.kpis.unprofitable_count ? "text-rose-400" : "text-gray-300"
        }].map(k => <Card key={k.l} className="hover:border-[#34404f] transition-colors">
                <div className="flex items-start justify-between">
                  <span className="text-xs uppercase tracking-wider text-gray-500">{k.l}</span>
                  <k.i size={18} className={k.c} />
                </div>
                <div className={`mt-3 font-mono text-2xl font-semibold ${k.c}`}>{k.v}</div>
              </Card>)}
          </div>

          {(dash.unprofitable.length > 0 || dash.at_risk.length > 0) && <Card className="mb-5 border-rose-500/30" data-testid="jobs-needing-attention">
              <h3 className="font-display font-semibold mb-3 flex items-center gap-2 text-rose-300"><AlertTriangle size={16} /> {t("jobprofitability.jobsNeedingAttention")}</h3>
              <div className="space-y-2">
                {dash.unprofitable.map(j => <div key={j.job_number} className="flex items-center justify-between py-2 px-3 rounded-lg bg-rose-500/5 border border-rose-500/20 text-sm">
                    <span><span className="font-mono text-amber-400">{j.job_number}</span> <span className="text-gray-300">{j.customer}</span> <span className="text-rose-400 text-xs ms-1">{t("jobprofitability.loss")}</span></span>
                    <span className="font-mono text-rose-400">{dollars(j.gross_profit)} · {j.margin}%</span>
                  </div>)}
                {dash.at_risk.map(j => <div key={j.job_number} className="flex items-center justify-between py-2 px-3 rounded-lg bg-amber-500/5 border border-amber-500/20 text-sm">
                    <span><span className="font-mono text-amber-400">{j.job_number}</span> <span className="text-gray-300">{j.customer}</span> <span className="text-amber-400 text-xs ms-1">{t("jobprofitability.lowMARGIN")}</span></span>
                    <span className="font-mono text-amber-400">{dollars(j.gross_profit)} · {j.margin}%</span>
                  </div>)}
              </div>
            </Card>}
        </>}

      <div className="flex gap-2 mb-4 flex-wrap">
        {TABS.map(t => <button key={t.key} onClick={() => setTab(t.key)} data-testid={`jp-tab-${t.key}`} className={`px-3 py-1.5 rounded-lg text-sm border transition-colors ${tab === t.key ? "bg-amber-500/10 text-amber-400 border-amber-500/30" : "text-gray-400 border-[#222838] hover:bg-[#161b25]"}`}>
            {t.label}
          </button>)}
      </div>

      {!data ? <Loading /> : <Card className="p-0 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="table-profitability">
              <thead>
                <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="px-5 py-3 text-start font-medium">{tab === "technician" ? "Technician" : "Name"}</th>
                  {tab === "technician" ? <>
                      <th className="px-5 py-3 text-end font-medium">{t("jobprofitability.hours")}</th>
                      <th className="px-5 py-3 text-end font-medium">{t("jobprofitability.jobs")}</th>
                      <th className="px-5 py-3 text-end font-medium">{t("jobprofitability.laborCost")}</th>
                    </> : <>
                      <th className="px-5 py-3 text-end font-medium">{t("jobprofitability.revenue")}</th>
                      <th className="px-5 py-3 text-end font-medium">{t("jobprofitability.cost")}</th>
                      <th className="px-5 py-3 text-end font-medium">{t("jobprofitability.grossProfit")}</th>
                      <th className="px-5 py-3 text-end font-medium">{t("jobprofitability.margin")}</th>
                    </>}
                </tr>
              </thead>
              <tbody>
                {data.items.map((it, i) => <tr key={i} className={`border-b border-[#171c26] hover:bg-[#161b25] ${it.unprofitable ? "bg-rose-500/5" : ""}`}>
                    <td className="px-5 py-3 text-gray-100 flex items-center gap-2">
                      {it.unprofitable && <AlertTriangle size={14} className="text-rose-400" />}{it.label}
                    </td>
                    {tab === "technician" ? <>
                        <td className="px-5 py-3 text-end font-mono text-gray-400">{it.hours}</td>
                        <td className="px-5 py-3 text-end font-mono text-gray-300">{it.jobs}</td>
                        <td className="px-5 py-3 text-end font-mono text-gray-200">{dollars(it.labor_cost)}</td>
                      </> : <>
                        <td className="px-5 py-3 text-end font-mono text-gray-300">{dollars(it.revenue)}</td>
                        <td className="px-5 py-3 text-end font-mono text-gray-400">{dollars(it.cost)}</td>
                        <td className={`px-5 py-3 text-end font-mono ${it.gross_profit < 0 ? "text-rose-400" : "text-emerald-400"}`}>{dollars(it.gross_profit)}</td>
                        <td className={`px-5 py-3 text-end font-mono ${marginCls(it.margin)}`}>{it.margin}%</td>
                      </>}
                  </tr>)}
                {data.items.length === 0 && <tr><td colSpan={5} className="px-5 py-12 text-center text-gray-500">{t("jobprofitability.noJobDataYet")}</td></tr>}
              </tbody>
            </table>
          </div>
        </Card>}
    </div>;
}
