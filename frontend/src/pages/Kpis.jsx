import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Gauge } from "lucide-react";
import { useTranslation } from "react-i18next";
export default function Kpis() {
  const {
    t
  } = useTranslation();
  const [loaded, setLoaded] = useState(false);
  const [data, setData] = useState(null);
  const [days, setDays] = useState("90");
  useEffect(() => {
    api.get(`/kpis?days=${days}`).then(setData).catch(e => toast.error(errMsg(e)));
    setLoaded(true);
  }, [days]);
  if (!loaded) return <Loading />;
  return <div className="space-y-6">
      <PageHeader title={`${data?.trade || ""} KPIs`} subtitle="The numbers that matter for your trade — computed from your books." actions={<Select value={days} onValueChange={setDays}>
            <SelectTrigger className="w-36 bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="30">{t("kpis.last30Days")}</SelectItem>
              <SelectItem value="90">{t("kpis.last90Days")}</SelectItem>
              <SelectItem value="365">{t("kpis.last12Months")}</SelectItem>
            </SelectContent>
          </Select>} />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {(data?.cards || []).map(c => <Card key={c.id} className="p-5">
            <div className="flex items-center gap-1.5 text-xs text-gray-500 mb-1">
              <Gauge size={12} /> {c.label}
            </div>
            <div className="text-3xl font-semibold">
              {c.unit === "$" ? `$${c.value.toLocaleString()}` : `${c.value}${c.unit === "/day" || c.unit === "days" ? "" : c.unit}`}
              {(c.unit === "/day" || c.unit === "days") && <span className="text-sm text-gray-500 ml-1">{c.unit}</span>}
            </div>
            {c.hint && <div className="text-xs text-gray-600 mt-1">{c.hint}</div>}
            <div className="text-[10px] text-gray-700 mt-1">{c.samples} {t("kpis.samples")}</div>
          </Card>)}
      </div>
      {!(data?.cards || []).length && <Card className="p-10 text-center text-gray-500 text-sm">{t("kpis.notEnoughDataYetInvoiceA")}</Card>}
    </div>;
}
