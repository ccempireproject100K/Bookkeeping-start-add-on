import { Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import { apiGet, apiPost } from "@/lib/api";
import { errMsg, fmtDate, fmtMoney } from "@/lib/format";
import { invalidateInventory } from "@/lib/invalidate";
import type { POApprovalIn, PurchaseOrder } from "@/lib/types";
import { Panel } from "@/components/Common";
import { Button } from "@/components/ui/button";

/** Dashboard banner: over-limit draft orders the owner must approve. Hidden when nothing is waiting. */
export default function ApprovalsCard() {
  const { data: pos = [] } = useQuery({ queryKey: ["pos", "draft"], queryFn: () => apiGet<PurchaseOrder[]>("/inventory/purchase-orders?status=draft") });
  const waiting = pos.filter((p) => p.awaiting_approval);
  const m = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) => apiPost<PurchaseOrder>(`/inventory/purchase-orders/${id}/approval`, { approve, note: approve ? "" : "Rejected from dashboard" } satisfies POApprovalIn),
    onSuccess: (po) => { toast.success(po.status === "cancelled" ? `${po.number} rejected` : `${po.number} approved`); invalidateInventory(); },
    onError: (e) => toast.error(errMsg(e)),
  });
  if (!waiting.length) return null;

  return (
    <Panel className="mt-3 border-amber-500/40 bg-amber-500/5 p-4 md:p-5" data-testid="approvals-card">
      <div className="flex items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-base font-semibold"><ShieldAlert className="size-4 text-amber-500" /> {waiting.length} order(s) waiting for your approval</h3>
        <Link to="/procurement" className="text-xs text-primary hover:underline" data-testid="approvals-view-all">Procurement</Link>
      </div>
      <div className="mt-2 divide-y divide-border">
        {waiting.map((po) => (
          <div key={po.id} className="flex flex-wrap items-center gap-3 py-2.5" data-testid={`approval-row-${po.number}`}>
            <div className="min-w-0 flex-1">
              <div className="text-sm"><span className="font-mono font-semibold">{po.number}</span> · {po.supplier_name}</div>
              <div className="truncate text-[11px] text-muted-foreground">{po.lines.length} line(s) · by {po.created_by} · {fmtDate(po.created_at)}</div>
            </div>
            <span className="font-mono text-sm font-semibold tabular-nums">{fmtMoney(po.total)}</span>
            <Button size="xs" onClick={() => m.mutate({ id: po.id, approve: true })} disabled={m.isPending} data-testid={`approval-approve-${po.number}`}><ShieldCheck /> Approve</Button>
            <Button size="xs" variant="ghost" onClick={() => m.mutate({ id: po.id, approve: false })} disabled={m.isPending} data-testid={`approval-reject-${po.number}`}>Reject</Button>
          </div>
        ))}
      </div>
    </Panel>
  );
}
