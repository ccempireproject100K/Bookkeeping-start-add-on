import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Check, Loader2, Sparkles, X } from "lucide-react";
import { apiPost } from "@/lib/api";
import { errMsg, fmtMoney } from "@/lib/format";
import { invalidateInventory } from "@/lib/invalidate";
import type { POCreate, PurchaseOrder, ReorderPlan, ReorderPlanLine } from "@/lib/types";
import { can, useMe } from "@/hooks/useMe";
import { Panel } from "@/components/Common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** ChatGPT proposes this week's order from the rule-based candidates; owner tweaks and approves in one tap. */
export default function AiPlan() {
  const { data: me } = useMe();
  const [plan, setPlan] = useState<ReorderPlan | null>(null);
  const [lines, setLines] = useState<ReorderPlanLine[]>([]);
  const gen = useMutation({
    mutationFn: () => apiPost<ReorderPlan>("/ai/reorder-plan"),
    onSuccess: (p) => { setPlan(p); setLines(p.lines); },
    onError: (e) => toast.error(errMsg(e)),
  });
  const approve = useMutation({
    mutationFn: async () => {
      const groups = new Map<string, ReorderPlanLine[]>();
      lines.forEach((l) => groups.set(l.supplier_id, [...(groups.get(l.supplier_id) ?? []), l]));
      const out: PurchaseOrder[] = [];
      for (const [supplier_id, ls] of groups) {
        const body: POCreate = { supplier_id, lines: ls.map((l) => ({ product_id: l.product_id, quantity: l.quantity, unit_cost: l.unit_cost })), expected_date: null, notes: `AI plan: ${plan?.summary ?? ""}`.slice(0, 1900) };
        out.push(await apiPost<PurchaseOrder>("/inventory/purchase-orders", body));
      }
      return out;
    },
    onSuccess: (pos) => { toast.success(`Drafted ${pos.map((p) => p.number).join(", ")} — review in Purchase orders`); setPlan(null); invalidateInventory(); },
    onError: (e) => toast.error(errMsg(e)),
  });
  if (!me?.ai_enabled || !can(me, "report:read")) return null;
  const total = lines.reduce((s, l) => s + l.quantity * l.unit_cost, 0);

  return (
    <Panel className="relative overflow-hidden p-4" data-testid="ai-plan-panel">
      <div className="pointer-events-none absolute -right-12 -top-12 size-40 rounded-full bg-primary/10 blur-3xl" />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold"><Sparkles className="size-4 text-primary" /> AI reorder draft</div>
          <p className="text-xs text-muted-foreground">ChatGPT weighs urgency, demand, cash and supplier prices, then proposes this week's order. Nothing is created until you approve.</p>
        </div>
        <Button size="sm" variant={plan ? "outline" : "default"} onClick={() => gen.mutate()} disabled={gen.isPending} data-testid="ai-plan-generate-button">
          {gen.isPending ? <Loader2 className="animate-spin" /> : <Sparkles />} {plan ? "Re-plan" : "Draft this week's order"}
        </Button>
      </div>
      {plan && (
        <div className="mt-3 space-y-2">
          <p className="text-sm" data-testid="ai-plan-summary">{plan.summary}</p>
          {lines.map((l, i) => (
            <div key={l.product_id} className="grid grid-cols-[1fr_64px_auto] items-center gap-2 rounded-md border border-border p-2 animate-rise" data-testid={`ai-plan-line-${l.product_id}`}>
              <div className="min-w-0">
                <div className="truncate text-sm">{l.name} <span className="text-xs text-muted-foreground">· {l.supplier_name} · {fmtMoney(l.unit_cost)}</span></div>
                <div className="truncate text-[11px] text-muted-foreground">{l.reason}</div>
              </div>
              <Input type="number" min={1} value={l.quantity} onChange={(e) => setLines(lines.map((x, j) => (j === i ? { ...x, quantity: Math.max(1, Number(e.target.value)) } : x)))} className="h-8 font-mono" data-testid={`ai-plan-qty-${l.product_id}`} />
              <Button variant="ghost" size="icon-sm" onClick={() => setLines(lines.filter((_, j) => j !== i))} data-testid={`ai-plan-remove-${l.product_id}`} aria-label="Remove"><X /></Button>
            </div>
          ))}
          {lines.length > 0 && can(me, "po:write") && (
            <Button className="w-full" onClick={() => approve.mutate()} disabled={approve.isPending} data-testid="ai-plan-approve-button">
              {approve.isPending ? <Loader2 className="animate-spin" /> : <Check />} Approve · draft {new Set(lines.map((l) => l.supplier_id)).size} PO(s) · {fmtMoney(total)}
            </Button>
          )}
          <p className="text-[10px] text-muted-foreground">AI-generated · {plan.model} · quantities are limited to 50–150% of the rule-based suggestion</p>
        </div>
      )}
    </Panel>
  );
}
