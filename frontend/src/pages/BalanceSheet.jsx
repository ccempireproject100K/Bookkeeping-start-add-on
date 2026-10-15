import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { money, today, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Input } from "@/components/ui/input";
import { CheckCircle2, AlertTriangle } from "lucide-react";
import { useTranslation } from "react-i18next";
function Section({
  title,
  rows,
  total
}) {
  const {
    t
  } = useTranslation();
  return <div>
      <div className="px-5 py-3 border-b border-[#222838] bg-[#11151c] flex items-center justify-between">
        <h3 className="font-display font-semibold text-sm uppercase tracking-wider text-amber-500/90">{title}</h3>
        <span className="font-mono text-sm text-gray-200">{money(total)}</span>
      </div>
      <table className="w-full text-sm">
        <tbody>
          {rows.map(r => <tr key={r.code} className="border-b border-[#171c26]">
              <td className="px-5 py-2 font-mono text-amber-400 w-24">{r.code}</td>
              <td className="px-5 py-2 text-gray-300">{r.name}</td>
              <td className="px-5 py-2 text-right font-mono text-gray-200">{money(r.amount)}</td>
            </tr>)}
          {rows.length === 0 && <tr><td colSpan={3} className="px-5 py-4 text-gray-600 text-center">{t("balancesheet.none")}</td></tr>}
        </tbody>
      </table>
    </div>;
}
export default function BalanceSheet() {
  const {
    t
  } = useTranslation();
  const [asOf, setAsOf] = useState(today());
  const [data, setData] = useState(null);
  useEffect(() => {
    setData(null);
    api.get(`/ledger/balance-sheet?as_of=${asOf}`).then(setData);
  }, [asOf]);
  return <div className="animate-rise">
      <PageHeader title={t("balancesheet.balanceSheet")} subtitle="Financial position" testid="balance-sheet-page">
        <Input type="date" value={asOf} onChange={e => setAsOf(e.target.value)} data-testid="bs-asof" className="w-auto bg-[#14171E] border-[#222838]" />
      </PageHeader>

      {!data ? <Loading /> : <div className="grid lg:grid-cols-2 gap-5">
          <Card className="p-0 overflow-hidden" data-testid="bs-assets">
            <Section title={t("balancesheet.assets")} rows={data.assets} total={data.assets_total} />
            <div className="px-5 py-3 border-t-2 border-[#2a3140] flex justify-between font-semibold">
              <span>{t("balancesheet.totalAssets")}</span>
              <span className="font-mono text-emerald-300" data-testid="bs-assets-total">{money(data.assets_total)}</span>
            </div>
          </Card>
          <div className="space-y-5">
            <Card className="p-0 overflow-hidden" data-testid="bs-liabilities">
              <Section title={t("balancesheet.liabilities")} rows={data.liabilities} total={data.liabilities_total} />
            </Card>
            <Card className="p-0 overflow-hidden" data-testid="bs-equity">
              <Section title={t("balancesheet.equity")} rows={data.equity} total={data.equity_total} />
            </Card>
            <Card className="flex items-center justify-between">
              <span className="font-semibold">{t("balancesheet.liabilitiesEquity")}</span>
              <div className="flex items-center gap-3">
                <span className="font-mono text-gray-200" data-testid="bs-le-total">{money(data.liabilities_equity_total)}</span>
                <span className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs ${data.balanced ? "bg-emerald-950 text-emerald-300 border border-emerald-800" : "bg-amber-950 text-amber-300 border border-amber-800"}`}>
                  {data.balanced ? <CheckCircle2 size={13} /> : <AlertTriangle size={13} />}{data.balanced ? "Balanced" : "Off"}
                </span>
              </div>
            </Card>
          </div>
        </div>}
    </div>;
}
