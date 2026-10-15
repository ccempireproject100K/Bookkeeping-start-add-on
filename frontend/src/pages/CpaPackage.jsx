import { useEffect, useState } from "react";
import { api, errMsg, downloadCsv } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Package, CheckCircle2, XCircle, Download } from "lucide-react";
import { useTranslation } from "react-i18next";
export default function CpaPackage() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const [loaded, setLoaded] = useState(false);
  const [year, setYear] = useState("2026");
  const [check, setCheck] = useState(null);
  useEffect(() => {
    api.get(`/cpa/checklist?year=${year}`).then(setCheck).catch(() => {});
    setLoaded(true);
  }, [year]);
  const download = () => {
    downloadCsv(`/cpa/package.zip?year=${year}`, `cpa-package-${year}.zip`);
    toast.success(t("cpapackage.cpaPackageDownloading"));
  };
  if (!loaded) return <Loading />;
  return <div className="space-y-6">
      <PageHeader title={t("cpapackage.cpaPackage")} subtitle="One click — everything your tax preparer needs, organized." actions={<>
          <Select value={year} onValueChange={setYear}><SelectTrigger className="w-28 bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
            <SelectContent>{["2024", "2025", "2026"].map(y => <SelectItem key={y} value={y}>{y}</SelectItem>)}</SelectContent></Select>
          <Button onClick={download} data-testid="cpa-download"><Download size={14} className="mr-1" />{t("cpapackage.downloadPackage")}</Button>
        </>} />
      <Card className="p-5">
        <div className="flex items-center gap-2 mb-3">
          {check?.ready ? <span className="inline-flex items-center gap-1 text-sm text-emerald-400"><CheckCircle2 size={16} /> {t("cpapackage.readyToSend")}</span> : <span className="inline-flex items-center gap-1 text-sm text-amber-400"><XCircle size={16} /> {t("cpapackage.aFewThingsToTidyFirst")}</span>}
        </div>
        <div className="space-y-2">
          {(check?.items || []).map((it, i) => <div key={i} className="flex items-center justify-between py-2 border-b border-[#1a2030] text-sm">
              <span className="flex items-center gap-2">
                {it.ok ? <CheckCircle2 size={15} className="text-emerald-400" /> : <XCircle size={15} className="text-amber-400" />}
                {it.label}</span>
              <span className="text-xs text-gray-500">{it.detail}</span>
            </div>)}
        </div>
      </Card>
      <Card className="p-5">
        <div className="text-sm font-medium mb-2 flex items-center gap-2"><Package size={15} /> {t("cpapackage.whatSInsideTheZIP")}</div>
        <ul className="text-sm text-gray-400 space-y-1 list-disc list-inside">
          <li>{t("cpapackage.profitLossBalanceSheetTrialBalance")}</li>
          <li>{t("cpapackage.generalLedgerEveryPostedLineChronological")}</li>
          <li>{t("cpapackage.chartOfAccountsExpensesInvoices")}</li>
          <li>{t("cpapackage.mileageLogWithIRSRatesApplied")}</li>
          <li>{t("cpapackage.ContractorSummaryWithW9")}</li>
          <li>{t("cpapackage.readmeForYourPreparer")}</li>
        </ul>
      </Card>
    </div>;
}
