import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { money, today, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Input } from "@/components/ui/input";
import { CheckCircle2, AlertTriangle } from "lucide-react";

export default function TrialBalance() {
  const [asOf, setAsOf] = useState(today());
  const [data, setData] = useState(null);

  const load = (d) => { setData(null); api.get(`/ledger/trial-balance?as_of=${d}`).then(setData); };
  useEffect(() => { load(asOf); }, [asOf]);

  return (
    <div className="animate-rise">
      <PageHeader title="Trial Balance" subtitle="Every debit matched by a credit" testid="trial-balance-page">
        <Input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)}
          data-testid="tb-asof" className="w-auto bg-[#14171E] border-[#222838]" />
      </PageHeader>

      {!data ? <Loading /> : (
        <Card className="p-0 overflow-hidden">
          <div className="px-5 py-4 border-b border-[#222838] flex items-center justify-between">
            <span className="text-sm text-gray-400">As of {fmtDate(data.as_of)}</span>
            <span data-testid="tb-balanced-badge" className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-medium ${data.balanced ? "bg-emerald-950 text-emerald-300 border border-emerald-800" : "bg-amber-950 text-amber-300 border border-amber-800"}`}>
              {data.balanced ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}
              {data.balanced ? "Balanced" : "Out of balance"}
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="table-trial-balance">
              <thead>
                <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="px-5 py-3 text-left font-medium">Code</th>
                  <th className="px-5 py-3 text-left font-medium">Account</th>
                  <th className="px-5 py-3 text-right font-medium">Debit</th>
                  <th className="px-5 py-3 text-right font-medium">Credit</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r) => (
                  <tr key={r.code} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-5 py-2.5 font-mono text-amber-400">{r.code}</td>
                    <td className="px-5 py-2.5 text-gray-200">{r.name}</td>
                    <td className="px-5 py-2.5 text-right font-mono text-emerald-400">{r.debit ? money(r.debit) : ""}</td>
                    <td className="px-5 py-2.5 text-right font-mono text-rose-400">{r.credit ? money(r.credit) : ""}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t-2 border-[#2a3140] font-semibold">
                  <td className="px-5 py-3" colSpan={2}>Totals</td>
                  <td className="px-5 py-3 text-right font-mono text-emerald-300" data-testid="tb-total-debit">{money(data.total_debit)}</td>
                  <td className="px-5 py-3 text-right font-mono text-rose-300" data-testid="tb-total-credit">{money(data.total_credit)}</td>
                </tr>
              </tfoot>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
