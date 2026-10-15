import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { Lock, LockOpen, Clock } from "lucide-react";

const STATUS = {
  open: { label: "Open", cls: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20", icon: LockOpen },
  soft_closed: { label: "Soft Closed", cls: "bg-amber-500/10 text-amber-400 border-amber-500/20", icon: Clock },
  locked: { label: "Locked", cls: "bg-rose-500/10 text-rose-400 border-rose-500/20", icon: Lock },
};

export default function FiscalPeriods() {
  const { user } = useAuth();
  const [periods, setPeriods] = useState(null);
  const isOwner = user?.role === "owner";

  const load = () => api.get("/ledger/periods").then(setPeriods).catch(() => setPeriods([]));
  useEffect(() => { load(); }, []);

  const setStatus = async (key, status) => {
    try {
      await api.post(`/ledger/periods/${key}/status`, { status });
      toast.success(`Period ${key} → ${STATUS[status].label}`);
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  if (!periods) return <Loading />;

  return (
    <div className="animate-rise">
      <PageHeader title="Fiscal Periods" subtitle="Lock closed months to protect posted books" testid="fiscal-periods-page" />
      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm" data-testid="table-periods">
          <thead>
            <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
              <th className="px-5 py-3 text-left font-medium">Period</th>
              <th className="px-5 py-3 text-left font-medium">Range</th>
              <th className="px-5 py-3 text-left font-medium">Status</th>
              <th className="px-5 py-3 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {periods.map((p) => {
              const s = STATUS[p.status] || STATUS.open;
              return (
                <tr key={p.key} className="border-b border-[#171c26] hover:bg-[#161b25]" data-testid={`period-${p.key}`}>
                  <td className="px-5 py-3 font-mono text-amber-400">{p.key}</td>
                  <td className="px-5 py-3 text-gray-400">{fmtDate(p.start_date)} – {fmtDate(p.end_date)}</td>
                  <td className="px-5 py-3">
                    <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs border ${s.cls}`}>
                      <s.icon size={12} />{s.label}
                    </span>
                  </td>
                  <td className="px-5 py-3 text-right space-x-2">
                    {isOwner && p.status !== "open" && (
                      <Button size="sm" variant="ghost" className="h-7 text-emerald-400 hover:bg-emerald-500/10" onClick={() => setStatus(p.key, "open")} data-testid={`reopen-${p.key}`}>Reopen</Button>
                    )}
                    {isOwner && p.status === "open" && (
                      <Button size="sm" variant="ghost" className="h-7 text-amber-400 hover:bg-amber-500/10" onClick={() => setStatus(p.key, "soft_closed")} data-testid={`soft-close-${p.key}`}>Soft Close</Button>
                    )}
                    {isOwner && p.status !== "locked" && (
                      <Button size="sm" variant="ghost" className="h-7 text-rose-400 hover:bg-rose-500/10" onClick={() => setStatus(p.key, "locked")} data-testid={`lock-${p.key}`}>Lock</Button>
                    )}
                  </td>
                </tr>
              );
            })}
            {periods.length === 0 && <tr><td colSpan={4} className="px-5 py-12 text-center text-gray-500">No periods yet</td></tr>}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
