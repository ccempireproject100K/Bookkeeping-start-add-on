import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiPost } from "@/lib/api";
import { errMsg, fmtMoney } from "@/lib/format";
import { invalidateInventory } from "@/lib/invalidate";
import type { PurchaseOrder, ReceiveIn } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

/** Receive what actually arrived; anything short stays open as a backorder unless closed. */
export default function ReceiveDialog({ po, onClose }: { po: PurchaseOrder | null; onClose: () => void }) {
  const [qty, setQty] = useState<Record<string, string>>({});
  const [cost, setCost] = useState<Record<string, string>>({});
  const [close, setClose] = useState(false);
  useEffect(() => {
    if (!po) return;
    setQty(Object.fromEntries(po.lines.map((l) => [l.product_id, String(l.quantity - l.received_qty)])));
    setCost(Object.fromEntries(po.lines.map((l) => [l.product_id, String(l.unit_cost)])));
    setClose(false);
  }, [po]);

  const m = useMutation({
    mutationFn: () => apiPost<PurchaseOrder>(`/inventory/purchase-orders/${po!.id}/receive`, {
      lines: po!.lines.map((l) => ({ product_id: l.product_id, quantity: Number(qty[l.product_id]) || 0, unit_cost: Number(cost[l.product_id]) })),
      close_backorder: close,
    } satisfies ReceiveIn),
    onSuccess: (r) => { toast.success(r.status === "partial" ? `${r.number}: received — rest on backorder` : `${r.number} fully received`); invalidateInventory(); onClose(); },
    onError: (e) => toast.error(errMsg(e)),
  });
  const total = po?.lines.reduce((s, l) => s + (Number(qty[l.product_id]) || 0) * (Number(cost[l.product_id]) || 0), 0) ?? 0;

  return (
    <Dialog open={!!po} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[92vh] overflow-y-auto sm:max-w-lg" data-testid="receive-dialog">
        <DialogHeader>
          <DialogTitle>Receive {po?.number}</DialogTitle>
          <DialogDescription>Enter what's in the box. Change the cost if the invoice price differs; it updates average cost and the expense.</DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          {po?.lines.map((l) => {
            const open = l.quantity - l.received_qty;
            return (
              <div key={l.product_id} className="grid grid-cols-[1fr_64px_80px] items-center gap-2" data-testid={`receive-line-${l.product_id}`}>
                <div className="min-w-0"><div className="truncate text-sm">{l.name}</div><div className="text-[11px] text-muted-foreground">{open} outstanding of {l.quantity}</div></div>
                <Input type="number" min={0} max={open} disabled={open <= 0} value={qty[l.product_id] ?? ""} onChange={(e) => setQty({ ...qty, [l.product_id]: e.target.value })} className="font-mono" data-testid={`receive-qty-${l.product_id}`} />
                <Input type="number" step="0.01" min={0} value={cost[l.product_id] ?? ""} onChange={(e) => setCost({ ...cost, [l.product_id]: e.target.value })} className="font-mono" data-testid={`receive-cost-${l.product_id}`} />
              </div>
            );
          })}
        </div>
        <label className="flex items-start gap-2 text-sm">
          <Checkbox checked={close} onCheckedChange={(c) => setClose(!!c)} data-testid="receive-close-backorder" />
          <span>Close the order — the supplier won't send the missing items</span>
        </label>
        <DialogFooter>
          <span className="mr-auto self-center font-mono text-sm" data-testid="receive-total">Expense {fmtMoney(total)}</span>
          <Button onClick={() => m.mutate()} disabled={m.isPending} data-testid="receive-submit-button">Receive</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
