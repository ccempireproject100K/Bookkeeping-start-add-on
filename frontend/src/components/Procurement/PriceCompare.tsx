import { Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowRightLeft } from "lucide-react";
import { apiPost } from "@/lib/api";
import { errMsg } from "@/lib/format";
import { invalidateInventory } from "@/lib/invalidate";
import { can, useMe } from "@/hooks/useMe";
import { Button } from "@/components/ui/button";
import { apiGet } from "@/lib/api";
import { fmtDate, fmtMoney } from "@/lib/format";
import type { ProductPrices, SupplierScorecard, SwitchSupplierIn } from "@/lib/types";
import { useState } from "react";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { Panel, Pill } from "@/components/Common";

/** What each supplier last charged you per product (from restock history), cheapest first. */
import { useTranslation } from "react-i18next";
export default function PriceCompare({
  productId
}: {
  productId?: string;
}) {
  const {
    t
  } = useTranslation();
  const {
    data: rows = [],
    isLoading
  } = useQuery({
    queryKey: ["prices", productId ?? ""],
    queryFn: () => apiGet<ProductPrices[]>(`/inventory/prices${productId ? `?product_id=${productId}` : ""}`)
  });
  const savings = rows.filter(r => r.saving_per_unit > 0);
  const {
    data: me
  } = useMe();
  const {
    data: cards = []
  } = useQuery({
    queryKey: ["scorecards"],
    queryFn: () => apiGet<SupplierScorecard[]>("/inventory/supplier-scorecards")
  });
  const byName = new Map(cards.map(c => [c.name.toLowerCase(), c]));
  const card = (name: string | null) => name ? byName.get(name.toLowerCase()) : undefined;
  const [confirm, setConfirm] = useState<{
    id: string;
    product: string;
    name: string;
    c: SupplierScorecard;
  } | null>(null);
  // Grade-aware: switching to a C/D supplier asks first, showing why they scored low.
  const trySwitch = (id: string, product: string, name: string) => {
    const c = card(name);
    if (c && (c.grade === "C" || c.grade === "D")) setConfirm({
      id,
      product,
      name,
      c
    });else sw.mutate({
      id,
      name
    });
  };
  const sw = useMutation({
    mutationFn: ({
      id,
      name
    }: {
      id: string;
      name: string;
    }) => apiPost<ProductPrices>(`/inventory/products/${id}/switch-supplier`, {
      supplier_name: name
    } satisfies SwitchSupplierIn),
    onSuccess: r => {
      toast.success(`${r.name} now orders from ${r.current_supplier}`);
      invalidateInventory();
    },
    onError: e => toast.error(errMsg(e))
  });
  if (productId && rows.length === 0) return null;
  return <div className="space-y-3" data-testid={productId ? "product-prices-panel" : "price-compare"}>
      {!productId && <p className="text-xs text-muted-foreground">{t("pricecompare.basedOnWhatYouActuallyPaid")} {savings.length > 0 ? `${savings.length} product(s) are cheaper elsewhere.` : "Your current suppliers are the cheapest you've used."} {t("pricecompare.switchingOnlyChangesWhoFutureOrders")}</p>}
      <div className={productId ? "" : "grid gap-3 md:grid-cols-2 xl:grid-cols-3"}>
        {rows.map(r => <Panel key={r.product_id} className="p-4" data-testid={`price-card-${r.product_id}`}>
            <div className="flex items-start justify-between gap-2">
              {productId ? <div className="text-sm font-semibold">{t("pricecompare.supplierPrices")}</div> : <Link to={`/inventory/products/${r.product_id}`} className="text-sm font-medium hover:underline">{r.name}</Link>}
              {r.saving_per_unit > 0 && <Pill tone="success" testId={`price-saving-${r.product_id}`}>{`Save ${fmtMoney(r.saving_per_unit)}/unit`}</Pill>}
            </div>
            {r.saving_per_unit > 0 && r.best_supplier && can(me, "product:write") && <Button size="xs" variant={["C", "D"].includes(card(r.best_supplier)?.grade ?? "") ? "outline" : "default"} className="mt-2 w-full" onClick={() => trySwitch(r.product_id, r.name, r.best_supplier!)} disabled={sw.isPending} data-testid={`price-switch-${r.product_id}`}>
                <ArrowRightLeft /> {t("pricecompare.switchTo")} {r.best_supplier} <GradeChip grade={card(r.best_supplier)?.grade} testId={`price-switch-grade-${r.product_id}`} />
              </Button>}
            <div className="mt-2 space-y-1">
              {r.prices.map((s, i) => <div key={s.supplier_name} className="flex items-center justify-between gap-2 text-xs">
                  <span className="truncate">{i === 0 && <span className="mr-1 text-emerald-600">●</span>}{s.supplier_name} <GradeChip grade={card(s.supplier_name)?.grade} />{s.is_current_supplier && <span className="ml-1 text-muted-foreground">{t("pricecompare.current")}</span>}{!s.is_current_supplier && can(me, "product:write") && i > 0 && <button className="ml-1 text-[10px] text-primary hover:underline" onClick={() => trySwitch(r.product_id, r.name, s.supplier_name)} data-testid={`price-use-${r.product_id}-${i}`}>{t("pricecompare.use")}</button>}</span>
                  <span className="whitespace-nowrap font-mono tabular-nums">{fmtMoney(s.last_cost)} <span className="text-muted-foreground">· {fmtDate(s.last_date)} · {s.source === "list" ? <span className="text-blue-500" data-testid={`price-list-tag-${r.product_id}-${i}`}>{t("pricecompare.priceList")}</span> : `${s.purchases}×`}</span></span>
                </div>)}
            </div>
          </Panel>)}
      </div>
      <Dialog open={!!confirm} onOpenChange={o => !o && setConfirm(null)}>
        <DialogContent className="sm:max-w-md" data-testid="switch-warning-dialog">
          <DialogHeader>
            <DialogTitle>{confirm?.name} {t("pricecompare.hasA")} {confirm?.c.grade} {t("pricecompare.grade")}</DialogTitle>
            <DialogDescription>{t("pricecompare.cheaperIsnTAlwaysBetterFrom")}</DialogDescription>
          </DialogHeader>
          {confirm && <ul className="space-y-1 text-sm" data-testid="switch-warning-reasons">
              {confirm.c.on_time_rate !== null && <li>{t("pricecompare.onTime")} {confirm.c.on_time_rate}{t("pricecompare.ofTheTime")}</li>}
              {confirm.c.avg_lead_days !== null && <li>{t("pricecompare.takes")} {confirm.c.avg_lead_days} {t("pricecompare.daysOnAveragePromised")} {confirm.c.promised_lead_days})</li>}
              {!!confirm.c.short_shipment_rate && <li>{t("pricecompare.shippedShortOn")} {confirm.c.short_shipment_rate}{t("pricecompare.ofOrdersFillRate")} {confirm.c.fill_rate}%)</li>}
              {!!confirm.c.price_change_pct && <li>{t("pricecompare.pricesDrifted")} {confirm.c.price_change_pct > 0 ? "+" : ""}{confirm.c.price_change_pct}%</li>}
            </ul>}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirm(null)} data-testid="switch-warning-cancel">{t("pricecompare.keepCurrentSupplier")}</Button>
            <Button variant="destructive" onClick={() => {
            if (confirm) sw.mutate({
              id: confirm.id,
              name: confirm.name
            });
            setConfirm(null);
          }} data-testid="switch-warning-confirm">{t("pricecompare.switch")} {confirm?.product} {t("pricecompare.anyway")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {!isLoading && rows.length === 0 && <p className="py-6 text-center text-sm text-muted-foreground">{t("pricecompare.noSupplierPriceHistoryYetIt")}</p>}
    </div>;
}
const GRADE_TONE: Record<string, string> = {
  A: "bg-emerald-500",
  B: "bg-blue-500",
  C: "bg-amber-500",
  D: "bg-red-500"
};
function GradeChip({
  grade,
  testId
}: {
  grade?: string;
  testId?: string;
}) {
  if (!grade || grade === "—") return null;
  return <span data-testid={testId} title={`Supplier grade ${grade}`} className={cn("inline-grid size-4 place-items-center rounded text-[9px] font-bold text-white", GRADE_TONE[grade])}>{grade}</span>;
}
