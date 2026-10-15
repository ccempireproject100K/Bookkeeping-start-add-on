import { useEffect, useState } from "react";
import { api, errMsg, downloadCsv } from "@/lib/api";
import { dollars, today, monthStart } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Download } from "lucide-react";
import { useTranslation } from "react-i18next";
function Section({
  title,
  data
}) {
  const {
    t
  } = useTranslation();
  return <div className="mb-5">
      <div className="text-xs uppercase tracking-widest text-gray-500 mb-2">{title}</div>
      {data.items.length === 0 ? <div className="text-gray-600 text-sm pl-2">{t("cashflow.noActivity")}</div> : data.items.map(it => <div key={it.code} className="flex justify-between py-1.5 border-b border-[#171c26] text-sm">
          <span className="text-gray-300">{it.code} · {it.name}</span>
          <span className={`font-mono ${it.amount_cents < 0 ? "text-rose-400" : "text-emerald-400"}`}>{dollars(it.amount)}</span>
        </div>)}
      <div className="flex justify-between py-2 text-sm font-semibold">
        <span className="text-gray-400">{t("cashflow.netCashFrom")} {title.toLowerCase()}</span>
        <span className="font-mono text-gray-100">{dollars(data.total)}</span>
      </div>
    </div>;
}
export default function CashFlow() {
  const {
    t
  } = useTranslation();
  const [from, setFrom] = useState(monthStart());
  const [to, setTo] = useState(today());
  const [data, setData] = useState(null);
  const load = () => api.get(`/reports/cash-flow?date_from=${from}&date_to=${to}`).then(setData).catch(e => errMsg(e));
  useEffect(() => {
    load();
  }, []);
  if (!data) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("cashflow.cashFlowStatement")} subtitle="Operating, investing & financing activities" testid="cashflow-page">
        <Button data-testid="btn-cashflow-csv" variant="outline" onClick={() => downloadCsv(`/reports/cash-flow.csv?date_from=${from}&date_to=${to}`, "cash_flow.csv")} className="border-[#222838]"><Download size={15} className="mr-1" />{t("cashflow.csv")}</Button>
      </PageHeader>
      <div className="flex items-end gap-3 mb-5">
        <div><Label className="text-xs">{t("cashflow.from")}</Label><Input data-testid="cf-from" type="date" value={from} onChange={e => setFrom(e.target.value)} className="bg-[#0F1218] border-[#222838]" /></div>
        <div><Label className="text-xs">{t("cashflow.to")}</Label><Input data-testid="cf-to" type="date" value={to} onChange={e => setTo(e.target.value)} className="bg-[#0F1218] border-[#222838]" /></div>
        <Button data-testid="cf-run" onClick={load} className="bg-amber-600 hover:bg-amber-500 text-black">{t("cashflow.run")}</Button>
      </div>
      <div className="grid lg:grid-cols-2 gap-6">
        <Card>
          <Section title={t("cashflow.operating")} data={data.operating} />
          <Section title={t("cashflow.investing")} data={data.investing} />
          <Section title={t("cashflow.financing")} data={data.financing} />
        </Card>
        <Card className="h-fit">
          <div className="space-y-3">
            <div className="flex justify-between"><span className="text-gray-400">{t("cashflow.openingCash")}</span><span className="font-mono text-gray-200">{dollars(data.opening)}</span></div>
            <div className="flex justify-between"><span className="text-gray-400">{t("cashflow.netChangeInCash")}</span><span className={`font-mono ${data.net_change_cents < 0 ? "text-rose-400" : "text-emerald-400"}`}>{dollars(data.net_change)}</span></div>
            <div className="flex justify-between border-t border-[#222838] pt-3 text-lg font-semibold"><span className="text-gray-200">{t("cashflow.closingCash")}</span><span className="font-mono text-amber-400" data-testid="cf-closing">{dollars(data.closing)}</span></div>
          </div>
        </Card>
      </div>
    </div>;
}
