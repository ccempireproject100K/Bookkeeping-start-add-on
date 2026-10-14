import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { dollars } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { AlertTriangle } from "lucide-react";

const TABS = [
  { key: "job", label: "By Job" },
  { key: "customer", label: "By Customer" },
  { key: "service_type", label: "By Service Type" },
  { key: "technician", label: "By Technician" },
];

export default function JobProfitability() {
  const [tab, setTab] = useState("job");
  const [data, setData] = useState(null);

  useEffect(() => {
    setData(null);
    api.get(`/jobs/reports/profitability?group_by=${tab}`).then(setData);
  }, [tab]);

  const marginCls = (m) => (m < 0 ? "text-rose-400" : m < 20 ? "text-amber-400" : "text-emerald-400");

  return (
    <div className="animate-rise">
      <PageHeader title="Job Profitability" subtitle="Where you make — and lose — money" testid="job-profitability-page" />

      <div className="flex gap-2 mb-4 flex-wrap">
        {TABS.map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)} data-testid={`jp-tab-${t.key}`}
            className={`px-3 py-1.5 rounded-lg text-sm border transition-colors ${tab === t.key ? "bg-amber-500/10 text-amber-400 border-amber-500/30" : "text-gray-400 border-[#222838] hover:bg-[#161b25]"}`}>
            {t.label}
          </button>
        ))}
      </div>

      {!data ? <Loading /> : (
        <Card className="p-0 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="table-profitability">
              <thead>
                <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="px-5 py-3 text-left font-medium">{tab === "technician" ? "Technician" : "Name"}</th>
                  {tab === "technician" ? (
                    <>
                      <th className="px-5 py-3 text-right font-medium">Hours</th>
                      <th className="px-5 py-3 text-right font-medium">Jobs</th>
                      <th className="px-5 py-3 text-right font-medium">Labor Cost</th>
                    </>
                  ) : (
                    <>
                      <th className="px-5 py-3 text-right font-medium">Revenue</th>
                      <th className="px-5 py-3 text-right font-medium">Cost</th>
                      <th className="px-5 py-3 text-right font-medium">Gross Profit</th>
                      <th className="px-5 py-3 text-right font-medium">Margin</th>
                    </>
                  )}
                </tr>
              </thead>
              <tbody>
                {data.items.map((it, i) => (
                  <tr key={i} className={`border-b border-[#171c26] hover:bg-[#161b25] ${it.unprofitable ? "bg-rose-500/5" : ""}`}>
                    <td className="px-5 py-3 text-gray-100 flex items-center gap-2">
                      {it.unprofitable && <AlertTriangle size={14} className="text-rose-400" />}{it.label}
                    </td>
                    {tab === "technician" ? (
                      <>
                        <td className="px-5 py-3 text-right font-mono text-gray-400">{it.hours}</td>
                        <td className="px-5 py-3 text-right font-mono text-gray-300">{it.jobs}</td>
                        <td className="px-5 py-3 text-right font-mono text-gray-200">{dollars(it.labor_cost)}</td>
                      </>
                    ) : (
                      <>
                        <td className="px-5 py-3 text-right font-mono text-gray-300">{dollars(it.revenue)}</td>
                        <td className="px-5 py-3 text-right font-mono text-gray-400">{dollars(it.cost)}</td>
                        <td className={`px-5 py-3 text-right font-mono ${it.gross_profit < 0 ? "text-rose-400" : "text-emerald-400"}`}>{dollars(it.gross_profit)}</td>
                        <td className={`px-5 py-3 text-right font-mono ${marginCls(it.margin)}`}>{it.margin}%</td>
                      </>
                    )}
                  </tr>
                ))}
                {data.items.length === 0 && <tr><td colSpan={5} className="px-5 py-12 text-center text-gray-500">No job data yet</td></tr>}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
