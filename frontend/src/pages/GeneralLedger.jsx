import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useTranslation } from "react-i18next";
export default function GeneralLedger() {
  const {
    t
  } = useTranslation();
  const [accounts, setAccounts] = useState([]);
  const [code, setCode] = useState("1010");
  const [data, setData] = useState(null);
  useEffect(() => {
    api.get("/ledger/accounts").then(setAccounts);
  }, []);
  useEffect(() => {
    setData(null);
    api.get(`/ledger/general-ledger?account=${code}`).then(setData);
  }, [code]);
  return <div className="animate-rise">
      <PageHeader title={t("generalledger.generalLedger")} subtitle="Running balance per account" testid="general-ledger-page">
        <Select value={code} onValueChange={setCode}>
          <SelectTrigger data-testid="gl-account-select" className="w-[280px] bg-[#14171E] border-[#222838]"><SelectValue /></SelectTrigger>
          <SelectContent className="bg-[#14171E] border-[#222838] max-h-[400px]">
            {accounts.map(a => <SelectItem key={a.code} value={a.code}>{a.code} — {a.name}</SelectItem>)}
          </SelectContent>
        </Select>
      </PageHeader>

      {!data ? <Loading /> : <Card className="p-0 overflow-hidden">
          <div className="px-5 py-4 border-b border-[#222838] flex items-center justify-between">
            <h3 className="font-display font-semibold">{data.account_code} — {data.account_name}</h3>
            <span className="text-sm text-gray-400">{t("generalledger.closing")} <span className="font-mono text-gray-100 ml-1">{money(data.closing)}</span></span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="table-general-ledger">
              <thead>
                <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="px-5 py-3 text-left font-medium">{t("generalledger.entry")}</th>
                  <th className="px-5 py-3 text-left font-medium">{t("generalledger.date")}</th>
                  <th className="px-5 py-3 text-left font-medium">{t("generalledger.memo")}</th>
                  <th className="px-5 py-3 text-right font-medium">{t("generalledger.debit")}</th>
                  <th className="px-5 py-3 text-right font-medium">{t("generalledger.credit")}</th>
                  <th className="px-5 py-3 text-right font-medium">{t("generalledger.balance")}</th>
                </tr>
              </thead>
              <tbody>
                <tr className="border-b border-[#171c26] text-gray-500">
                  <td className="px-5 py-2" colSpan={5}>{t("generalledger.openingBalance")}</td>
                  <td className="px-5 py-2 text-right font-mono">{money(data.opening)}</td>
                </tr>
                {data.lines.map((l, i) => <tr key={i} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-5 py-2 font-mono text-amber-400">{l.entry_number}</td>
                    <td className="px-5 py-2 text-gray-400">{fmtDate(l.entry_date)}</td>
                    <td className="px-5 py-2 text-gray-400 max-w-[260px] truncate">{l.memo || "—"}</td>
                    <td className="px-5 py-2 text-right font-mono text-emerald-400">{l.debit ? money(l.debit) : ""}</td>
                    <td className="px-5 py-2 text-right font-mono text-rose-400">{l.credit ? money(l.credit) : ""}</td>
                    <td className="px-5 py-2 text-right font-mono text-gray-200">{money(l.balance)}</td>
                  </tr>)}
                {data.lines.length === 0 && <tr><td colSpan={6} className="px-5 py-10 text-center text-gray-500">{t("generalledger.noActivity")}</td></tr>}
              </tbody>
            </table>
          </div>
        </Card>}
    </div>;
}
