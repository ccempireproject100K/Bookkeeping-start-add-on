import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { money, today, monthStart, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Input } from "@/components/ui/input";
import { useTranslation } from "react-i18next";
export default function ProfitLoss() {
  const {
    t
  } = useTranslation();
  const [from, setFrom] = useState(monthStart());
  const [to, setTo] = useState(today());
  const [data, setData] = useState(null);
  useEffect(() => {
    setData(null);
    api.get(`/ledger/pnl?date_from=${from}&date_to=${to}`).then(setData);
  }, [from, to]);
  return <div className="animate-rise">
      <PageHeader title={t("profitloss.profitLoss")} subtitle="GL-derived income statement" testid="profit-loss-page">
        <div className="flex items-center gap-2">
          <Input type="date" value={from} onChange={e => setFrom(e.target.value)} data-testid="pnl-from" className="w-auto bg-[#14171E] border-[#222838]" />
          <span className="text-gray-500">→</span>
          <Input type="date" value={to} onChange={e => setTo(e.target.value)} data-testid="pnl-to" className="w-auto bg-[#14171E] border-[#222838]" />
        </div>
      </PageHeader>

      {!data ? <Loading /> : <div className="grid lg:grid-cols-2 gap-5">
          <Card className="p-0 overflow-hidden">
            <div className="px-5 py-3 border-b border-[#222838] bg-[#11151c] flex justify-between">
              <h3 className="font-display font-semibold text-sm uppercase tracking-wider text-emerald-400/90">{t("profitloss.revenue")}</h3>
              <span className="font-mono text-sm text-emerald-300">{money(data.revenue_total)}</span>
            </div>
            <table className="w-full text-sm">
              <tbody>
                {data.revenue.map(r => <tr key={r.code} className="border-b border-[#171c26]">
                    <td className="px-5 py-2 font-mono text-amber-400 w-24">{r.code}</td>
                    <td className="px-5 py-2 text-gray-300">{r.name}</td>
                    <td className="px-5 py-2 text-end font-mono text-gray-200">{money(r.amount)}</td>
                  </tr>)}
                {data.revenue.length === 0 && <tr><td colSpan={3} className="px-5 py-4 text-center text-gray-600">{t("profitloss.noRevenue")}</td></tr>}
              </tbody>
            </table>
          </Card>

          <Card className="p-0 overflow-hidden">
            <div className="px-5 py-3 border-b border-[#222838] bg-[#11151c] flex justify-between">
              <h3 className="font-display font-semibold text-sm uppercase tracking-wider text-rose-400/90">{t("profitloss.expenses")}</h3>
              <span className="font-mono text-sm text-rose-300">{money(data.expense_total)}</span>
            </div>
            <table className="w-full text-sm">
              <tbody>
                {data.expense.map(r => <tr key={r.code} className="border-b border-[#171c26]">
                    <td className="px-5 py-2 font-mono text-amber-400 w-24">{r.code}</td>
                    <td className="px-5 py-2 text-gray-300">{r.name}</td>
                    <td className="px-5 py-2 text-end font-mono text-gray-200">{money(r.amount)}</td>
                  </tr>)}
                {data.expense.length === 0 && <tr><td colSpan={3} className="px-5 py-4 text-center text-gray-600">{t("profitloss.noExpenses")}</td></tr>}
              </tbody>
            </table>
          </Card>

          <Card className="lg:col-span-2 flex items-center justify-between bg-gradient-to-r from-amber-500/10 to-transparent border-amber-500/30">
            <span className="font-display font-semibold text-lg">{t("profitloss.netIncome")}</span>
            <span data-testid="pnl-net-income" className={`font-mono text-2xl font-bold ${data.net_income >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
              {money(data.net_income)}
            </span>
          </Card>
        </div>}
    </div>;
}
