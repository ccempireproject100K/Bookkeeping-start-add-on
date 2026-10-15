import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import { fmtMoney } from "@/lib/format";
import type { SupplierScorecard } from "@/lib/types";
import { Panel } from "@/components/Common";
import { cn } from "@/lib/utils";

const GRADE: Record<string, string> = {
  A: "bg-emerald-500 text-white", B: "bg-blue-500 text-white", C: "bg-amber-500 text-white", D: "bg-red-500 text-white", "—": "bg-muted text-muted-foreground",
};

function Metric({ label, value, warn, testId }: { label: string; value: string; warn?: boolean; testId: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={cn("font-mono text-sm font-semibold tabular-nums", warn && "text-red-500")} data-testid={testId}>{value}</div>
    </div>
  );
}

const pct = (v: number | null) => (v === null ? "—" : `${v}%`);

/** Who to trust: graded from your own PO history (on-time, short shipments, fill rate, price drift). */
export default function Scorecards() {
  const { data: rows = [] } = useQuery({ queryKey: ["scorecards"], queryFn: () => apiGet<SupplierScorecard[]>("/inventory/supplier-scorecards") });
  if (!rows.length) return null;
  return (
    <div className="space-y-2" data-testid="supplier-scorecards">
      <div className="text-sm font-semibold">Supplier scorecards</div>
      <div className="grid gap-3 md:grid-cols-2">
        {rows.map((r) => (
          <Panel key={r.supplier_id} className="p-4" data-testid={`scorecard-${r.supplier_id}`}>
            <div className="flex items-start gap-3">
              <div className={cn("grid size-11 shrink-0 place-items-center rounded-lg font-heading text-xl font-bold", GRADE[r.grade] ?? GRADE["—"])} data-testid={`scorecard-grade-${r.supplier_id}`}>{r.grade}</div>
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">{r.name}</div>
                <div className="text-[11px] text-muted-foreground">{r.orders_received} order(s) received · {fmtMoney(r.total_spend)} spent{r.open_orders ? ` · ${r.open_orders} open` : ""}</div>
              </div>
            </div>
            <div className="mt-3 grid grid-cols-3 gap-3 sm:grid-cols-5">
              <Metric label="On time" value={pct(r.on_time_rate)} warn={r.on_time_rate !== null && r.on_time_rate < 80} testId={`scorecard-ontime-${r.supplier_id}`} />
              <Metric label="Lead time" value={r.avg_lead_days === null ? "—" : `${r.avg_lead_days}d`} warn={r.avg_lead_days !== null && r.avg_lead_days > r.promised_lead_days + 1} testId={`scorecard-lead-${r.supplier_id}`} />
              <Metric label="Short ships" value={pct(r.short_shipment_rate)} warn={!!r.short_shipment_rate && r.short_shipment_rate > 10} testId={`scorecard-short-${r.supplier_id}`} />
              <Metric label="Fill rate" value={pct(r.fill_rate)} warn={r.fill_rate !== null && r.fill_rate < 95} testId={`scorecard-fill-${r.supplier_id}`} />
              <Metric label="Price drift" value={r.price_change_pct === null ? "—" : `${r.price_change_pct > 0 ? "+" : ""}${r.price_change_pct}%`} warn={!!r.price_change_pct && r.price_change_pct > 3} testId={`scorecard-price-${r.supplier_id}`} />
            </div>
            <div className="mt-2 text-[10px] text-muted-foreground">Promised {r.promised_lead_days}d lead{r.price_increases ? ` · ${r.price_increases} price increase(s)` : ""}{r.grade === "—" ? " · grade after 2 deliveries" : ""}</div>
          </Panel>
        ))}
      </div>
    </div>
  );
}
