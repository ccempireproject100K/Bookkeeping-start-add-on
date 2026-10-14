import { useEffect, useState } from "react";
import { api, errMsg, downloadCsv } from "@/lib/api";
import { dollars } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Download } from "lucide-react";

export default function BusinessReports() {
  const [ibc, setIbc] = useState(null);
  const [ebv, setEbv] = useState(null);

  useEffect(() => {
    api.get("/reports/income-by-customer").then(setIbc).catch(() => setIbc({ rows: [], total: 0 }));
    api.get("/reports/expenses-by-vendor").then(setEbv).catch(() => setEbv({ rows: [], total: 0 }));
  }, []);

  if (!ibc || !ebv) return <Loading />;
  return (
    <div className="animate-rise">
      <PageHeader title="Business Reports" subtitle="Income by customer & expenses by vendor" testid="business-reports-page" />
      <Tabs defaultValue="income">
        <TabsList className="bg-[#14171E] border border-[#222838]">
          <TabsTrigger value="income" data-testid="tab-income-customer">Income by Customer</TabsTrigger>
          <TabsTrigger value="expense" data-testid="tab-expense-vendor">Expenses by Vendor</TabsTrigger>
        </TabsList>

        <TabsContent value="income" className="mt-4">
          <div className="mb-3 flex justify-end"><Button data-testid="btn-ibc-csv" variant="outline" onClick={() => downloadCsv("/reports/income-by-customer.csv", "income_by_customer.csv")} className="border-[#222838]"><Download size={15} className="mr-1" />CSV</Button></div>
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-ibc">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-4 py-3 text-left font-medium">Customer</th><th className="px-4 py-3 text-right font-medium">Invoices</th>
                <th className="px-4 py-3 text-right font-medium">Paid</th><th className="px-4 py-3 text-right font-medium">Income</th></tr></thead>
              <tbody>
                {ibc.rows.map((r) => (
                  <tr key={r.customer} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-4 py-3 text-gray-100">{r.customer}</td><td className="px-4 py-3 text-right text-gray-400">{r.invoices}</td>
                    <td className="px-4 py-3 text-right font-mono text-gray-400">{dollars(r.paid)}</td>
                    <td className="px-4 py-3 text-right font-mono text-emerald-400">{dollars(r.income)}</td>
                  </tr>
                ))}
                {ibc.rows.length === 0 && <tr><td colSpan={4} className="px-5 py-12 text-center text-gray-500">No customer income yet.</td></tr>}
              </tbody>
              {ibc.rows.length > 0 && <tfoot><tr className="font-semibold"><td className="px-4 py-3 text-gray-300">Total</td><td /><td /><td className="px-4 py-3 text-right font-mono text-amber-400">{dollars(ibc.total)}</td></tr></tfoot>}
            </table>
          </Card>
        </TabsContent>

        <TabsContent value="expense" className="mt-4">
          <div className="mb-3 flex justify-end"><Button data-testid="btn-ebv-csv" variant="outline" onClick={() => downloadCsv("/reports/expenses-by-vendor.csv", "expenses_by_vendor.csv")} className="border-[#222838]"><Download size={15} className="mr-1" />CSV</Button></div>
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-sm" data-testid="table-ebv">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-4 py-3 text-left font-medium">Vendor</th><th className="px-4 py-3 text-right font-medium">Transactions</th>
                <th className="px-4 py-3 text-right font-medium">Spend</th></tr></thead>
              <tbody>
                {ebv.rows.map((r) => (
                  <tr key={r.vendor} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-4 py-3 text-gray-100">{r.vendor}</td><td className="px-4 py-3 text-right text-gray-400">{r.count}</td>
                    <td className="px-4 py-3 text-right font-mono text-rose-400">{dollars(r.spend)}</td>
                  </tr>
                ))}
                {ebv.rows.length === 0 && <tr><td colSpan={3} className="px-5 py-12 text-center text-gray-500">No vendor expenses yet.</td></tr>}
              </tbody>
              {ebv.rows.length > 0 && <tfoot><tr className="font-semibold"><td className="px-4 py-3 text-gray-300">Total</td><td /><td className="px-4 py-3 text-right font-mono text-amber-400">{dollars(ebv.total)}</td></tr></tfoot>}
            </table>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
