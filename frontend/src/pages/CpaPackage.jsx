import { useEffect, useState } from "react";
import { api, errMsg, downloadCsv } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Package, CheckCircle2, XCircle, Download } from "lucide-react";

export default function CpaPackage() {
  const { user } = useAuth();
  const [loaded, setLoaded] = useState(false);
  const [year, setYear] = useState("2026");
  const [check, setCheck] = useState(null);

  useEffect(() => {
    api.get(`/cpa/checklist?year=${year}`).then(setCheck).catch(() => {});
    setLoaded(true);
  }, [year]);

  const download = () => {
    downloadCsv(`/cpa/package.zip?year=${year}`, `cpa-package-${year}.zip`);
    toast.success("CPA package downloading");
  };

  if (!loaded) return <Loading />;
  return (
    <div className="space-y-6">
      <PageHeader title="CPA Package" subtitle="One click — everything your tax preparer needs, organized."
        actions={<>
          <Select value={year} onValueChange={setYear}><SelectTrigger className="w-28 bg-[#0F1218] border-[#222838]"><SelectValue /></SelectTrigger>
            <SelectContent>{["2024", "2025", "2026"].map((y) => <SelectItem key={y} value={y}>{y}</SelectItem>)}</SelectContent></Select>
          <Button onClick={download} data-testid="cpa-download"><Download size={14} className="mr-1" />Download package</Button>
        </>} />
      <Card className="p-5">
        <div className="flex items-center gap-2 mb-3">
          {check?.ready
            ? <span className="inline-flex items-center gap-1 text-sm text-emerald-400"><CheckCircle2 size={16} /> Ready to send</span>
            : <span className="inline-flex items-center gap-1 text-sm text-amber-400"><XCircle size={16} /> A few things to tidy first</span>}
        </div>
        <div className="space-y-2">
          {(check?.items || []).map((it, i) => (
            <div key={i} className="flex items-center justify-between py-2 border-b border-[#1a2030] text-sm">
              <span className="flex items-center gap-2">
                {it.ok ? <CheckCircle2 size={15} className="text-emerald-400" /> : <XCircle size={15} className="text-amber-400" />}
                {it.label}</span>
              <span className="text-xs text-gray-500">{it.detail}</span>
            </div>
          ))}
        </div>
      </Card>
      <Card className="p-5">
        <div className="text-sm font-medium mb-2 flex items-center gap-2"><Package size={15} /> What's inside the ZIP</div>
        <ul className="text-sm text-gray-400 space-y-1 list-disc list-inside">
          <li>Profit & loss, balance sheet, trial balance</li>
          <li>General ledger — every posted line, chronological</li>
          <li>Chart of accounts, expenses, invoices</li>
          <li>Mileage log with IRS rates applied</li>
          <li>1099 contractor summary with W-9 status</li>
          <li>README for your preparer</li>
        </ul>
      </Card>
    </div>
  );
}
