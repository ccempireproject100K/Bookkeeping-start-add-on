import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { Loader2 } from "lucide-react";
import { apiGet, apiPost } from "@/lib/api";
import { errMsg, fmtDate, fmtMoney } from "@/lib/format";
import { invalidateInventory } from "@/lib/invalidate";
import type { AdjustReason, Invoice, Movement, ProductDetail } from "@/lib/types";
import { useTeam } from "@/hooks/useMe";
import { Field, NativeSelect } from "@/components/Common";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

export type StockAction = "restock" | "sell" | "use" | "adjust" | "return";

const TITLES: Record<StockAction, string> = {
  restock: "Restock", sell: "Sell to client", use: "Use in service", adjust: "Adjust stock", return: "Customer return",
};
const REASONS: [AdjustReason, string][] = [
  ["count_correction", "Count correction"], ["damaged", "Damaged"], ["expired", "Expired"], ["theft_loss", "Theft / loss"], ["other", "Other"],
];

interface Props { open: boolean; onOpenChange: (o: boolean) => void; detail: ProductDetail | null; action: StockAction }

export default function StockActionDialog({ open, onOpenChange, detail, action }: Props) {
  const p = detail?.product;
  const mode = p?.tracking_mode ?? "none";
  const [qty, setQty] = useState("1");
  const [direction, setDirection] = useState<"remove" | "add">("remove");
  const [cost, setCost] = useState("");
  const [price, setPrice] = useState("");
  const [supplier, setSupplier] = useState("");
  const [lotId, setLotId] = useState("");
  const [lotNumber, setLotNumber] = useState("");
  const [expiry, setExpiry] = useState("");
  const [serialText, setSerialText] = useState("");
  const [serialIds, setSerialIds] = useState<string[]>([]);
  const [warranty, setWarranty] = useState("");
  const [recordExpense, setRecordExpense] = useState(true);
  const [barber, setBarber] = useState("");
  const [link, setLink] = useState<"new" | "invoice">("new");
  const [invoiceId, setInvoiceId] = useState("");
  const [reason, setReason] = useState<AdjustReason | "">("");
  const [refund, setRefund] = useState("");
  const [note, setNote] = useState("");

  const { data: team = [] } = useTeam();
  const { data: invoices = [] } = useQuery({
    queryKey: ["invoices", "open"], queryFn: () => apiGet<Invoice[]>("/invoices"), enabled: open && action === "sell",
    select: (xs) => xs.filter((i) => i.status === "draft" || i.status === "sent"),
  });

  useEffect(() => {
    if (!open || !p) return;
    setQty("1"); setDirection("remove"); setCost(String(p.unit_cost)); setPrice(String(p.sell_price)); setSupplier(p.supplier_name ?? "");
    setLotId(action === "sell" || action === "use" ? detail?.suggested_lot_id ?? "" : ""); setLotNumber(""); setExpiry("");
    setSerialText(""); setSerialIds([]); setWarranty(""); setRecordExpense(true); setBarber(""); setLink("new"); setInvoiceId("");
    setReason(""); setRefund(""); setNote("");
  }, [open, p, action, detail?.suggested_lot_id]);

  const removing = action === "sell" || action === "use" || (action === "adjust" && direction === "remove");
  const typedSerials = serialText.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  const pickable = useMemo(() => (detail?.serials ?? []).filter((s) =>
    action === "return" ? s.status === "sold" || s.status === "used" : s.status === "in_stock"), [detail, action]);
  const addingSerials = mode === "serial" && (action === "restock" || (action === "adjust" && direction === "add"));
  const pickingSerials = mode === "serial" && !addingSerials;
  const n = mode === "serial" ? (addingSerials ? typedSerials.length : serialIds.length) : Number(qty) || 0;
  const lots = (detail?.lots ?? []).filter((l) => (removing ? l.quantity_on_hand > 0 : true));

  const m = useMutation({
    mutationFn: () => {
      const base = { product_id: p!.id, note };
      const lot = lotId || null;
      switch (action) {
        case "restock":
          return apiPost<Movement>("/inventory/stock/restock", {
            ...base, quantity: n, unit_cost: Number(cost) || 0, supplier_name: supplier, lot_id: lot,
            lot_number: lot ? null : lotNumber || null, expiry_date: expiry || null, serial_numbers: typedSerials,
            warranty_until: warranty || null, record_expense: recordExpense,
          });
        case "sell":
          return apiPost<Movement>("/inventory/stock/sell", {
            ...base, quantity: n, unit_price: Number(price) || 0, lot_id: lot, serial_unit_ids: serialIds,
            barber_id: barber || null, link, invoice_id: link === "invoice" ? invoiceId || null : null,
          });
        case "use":
          return apiPost<Movement>("/inventory/stock/use", { ...base, quantity: n, lot_id: lot, serial_unit_ids: serialIds, barber_id: barber || null });
        case "adjust":
          return apiPost<Movement>("/inventory/stock/adjust", {
            ...base, quantity: direction === "add" ? n : -n, reason, lot_id: lot, serial_unit_ids: direction === "remove" ? serialIds : [],
            serial_numbers: direction === "add" ? typedSerials : [], unit_cost: direction === "add" ? Number(cost) || 0 : null,
          });
        case "return":
          return apiPost<Movement>("/inventory/stock/return", {
            ...base, quantity: n, lot_id: lot, serial_unit_ids: serialIds, refund_amount: Number(refund) || 0, barber_id: barber || null,
          });
      }
    },
    onSuccess: (mv) => {
      toast.success(`${TITLES[action]}: ${mv.quantity > 0 ? "+" : ""}${mv.quantity} ${p?.name}`);
      invalidateInventory();
      onOpenChange(false);
    },
    onError: (e) => toast.error(errMsg(e)),
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (n <= 0) return toast.error(mode === "serial" ? "Choose or enter at least one serial number" : "Quantity must be at least 1");
    if (action === "adjust" && !reason) return toast.error("Pick a reason — every adjustment is audited");
    if (mode === "lot" && action === "adjust" && direction === "add" && !lotId) return toast.error("Choose the lot to add to");
    if (mode === "lot" && action === "return" && !lotId) return toast.error("Choose the lot the units go back into");
    if (mode === "lot" && action === "restock" && !lotId && !lotNumber.trim()) return toast.error("Enter a lot number");
    m.mutate();
  };

  if (!p) return null;
  const barberSelect = (
    <Field label="Barber">
      <NativeSelect value={barber} onChange={(e) => setBarber(e.target.value)} data-testid="stock-barber-select">
        <option value="">— None —</option>
        {team.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
      </NativeSelect>
    </Field>
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92vh] overflow-y-auto sm:max-w-lg" data-testid="stock-action-dialog">
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle data-testid="stock-action-title">{TITLES[action]} · {p.name}</DialogTitle>
            <DialogDescription className="font-mono text-xs">
              {p.quantity_on_hand} {p.unit} on hand · avg cost {fmtMoney(p.unit_cost)}
            </DialogDescription>
          </DialogHeader>

          {action === "adjust" && (
            <div className="grid grid-cols-2 gap-2">
              {(["remove", "add"] as const).map((d) => (
                <button key={d} type="button" onClick={() => setDirection(d)} data-testid={`adjust-direction-${d}`}
                  className={`rounded-md border px-3 py-2 text-sm font-medium transition-colors duration-150 ${direction === d ? "border-primary bg-primary/10" : "border-border hover:bg-muted"}`}>
                  {d === "remove" ? "− Remove units" : "+ Add units"}
                </button>
              ))}
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            {mode !== "serial" && (
              <Field label="Quantity">
                <Input type="number" min={1} required value={qty} onChange={(e) => setQty(e.target.value)} className="font-mono" data-testid="stock-qty-input" />
              </Field>
            )}
            {(action === "restock" || (action === "adjust" && direction === "add")) && (
              <Field label="Unit cost ($)">
                <Input type="number" step="0.01" min={0} value={cost} onChange={(e) => setCost(e.target.value)} className="font-mono" data-testid="stock-cost-input" />
              </Field>
            )}
            {action === "sell" && (
              <Field label="Unit price ($)">
                <Input type="number" step="0.01" min={0} value={price} onChange={(e) => setPrice(e.target.value)} className="font-mono" data-testid="stock-price-input" />
              </Field>
            )}
            {action === "return" && (
              <Field label="Refund ($)">
                <Input type="number" step="0.01" min={0} value={refund} onChange={(e) => setRefund(e.target.value)} placeholder="0.00" className="font-mono" data-testid="stock-refund-input" />
              </Field>
            )}
            {action === "adjust" && (
              <Field label="Reason (required)">
                <NativeSelect value={reason} onChange={(e) => setReason(e.target.value as AdjustReason)} data-testid="stock-reason-select">
                  <option value="">Choose…</option>
                  {REASONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </NativeSelect>
              </Field>
            )}
            {action === "restock" && (
              <Field label="Supplier"><Input value={supplier} onChange={(e) => setSupplier(e.target.value)} data-testid="stock-supplier-input" /></Field>
            )}
            {(action === "sell" || action === "use" || action === "return") && barberSelect}
          </div>

          {mode === "lot" && (
            <div className="space-y-3 rounded-lg border border-border bg-muted/30 p-3">
              <Field label={action === "restock" ? "Add to existing lot (or create a new one below)" : removing ? "Take from lot (FEFO suggested)" : "Lot"}>
                <NativeSelect value={lotId} onChange={(e) => setLotId(e.target.value)} data-testid="stock-lot-select">
                  <option value="">{action === "restock" ? "New lot" : removing ? "Auto (earliest expiry first)" : "Choose lot…"}</option>
                  {lots.map((l) => (
                    <option key={l.id} value={l.id}>
                      {`${l.lot_number} · ${l.quantity_on_hand} left · exp ${fmtDate(l.expiry_date)}${l.id === detail?.suggested_lot_id ? " · FEFO" : ""}`}
                    </option>
                  ))}
                </NativeSelect>
              </Field>
              {action === "restock" && !lotId && (
                <div className="grid grid-cols-2 gap-3">
                  <Field label="New lot number"><Input value={lotNumber} onChange={(e) => setLotNumber(e.target.value)} className="font-mono" data-testid="stock-lot-number-input" /></Field>
                  <Field label="Expiry date"><Input type="date" value={expiry} onChange={(e) => setExpiry(e.target.value)} data-testid="stock-expiry-input" /></Field>
                </div>
              )}
            </div>
          )}

          {addingSerials && (
            <div className="grid grid-cols-2 gap-3">
              <Field label={`Serial numbers (${typedSerials.length})`} className="col-span-2">
                <Textarea rows={3} value={serialText} onChange={(e) => setSerialText(e.target.value)} placeholder="One per line — scan them in" className="font-mono" data-testid="stock-serials-input" />
              </Field>
              {action === "restock" && (
                <Field label="Warranty until"><Input type="date" value={warranty} onChange={(e) => setWarranty(e.target.value)} data-testid="stock-warranty-input" /></Field>
              )}
            </div>
          )}

          {pickingSerials && (
            <Field label={`Choose serial numbers (${serialIds.length} selected)`}>
              <div className="max-h-44 space-y-1 overflow-y-auto rounded-md border border-border p-2" data-testid="stock-serial-picker">
                {pickable.length === 0 && <p className="p-2 text-xs text-muted-foreground">No eligible units.</p>}
                {pickable.map((s) => (
                  <label key={s.id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-muted">
                    <Checkbox checked={serialIds.includes(s.id)} data-testid={`stock-serial-option-${s.serial_number}`}
                      onCheckedChange={(c) => setSerialIds((xs) => (c ? [...xs, s.id] : xs.filter((x) => x !== s.id)))} />
                    <span className="font-mono">{s.serial_number}</span>
                  </label>
                ))}
              </div>
            </Field>
          )}

          {action === "sell" && (
            <Field label="Money record">
              <NativeSelect value={link} onChange={(e) => setLink(e.target.value as "new" | "invoice")} data-testid="stock-link-select">
                <option value="new">Create a new income record</option>
                <option value="invoice">Add as a line on an open invoice</option>
              </NativeSelect>
              {link === "invoice" && (
                <NativeSelect className="mt-2" value={invoiceId} onChange={(e) => setInvoiceId(e.target.value)} data-testid="stock-invoice-select">
                  <option value="">Choose invoice…</option>
                  {invoices.map((i) => <option key={i.id} value={i.id}>{`${i.number} · ${i.client_name} · ${fmtMoney(i.total)}`}</option>)}
                </NativeSelect>
              )}
            </Field>
          )}
          {action === "restock" && (
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={recordExpense} onCheckedChange={(c) => setRecordExpense(!!c)} data-testid="stock-record-expense-checkbox" />
              Record {fmtMoney(n * (Number(cost) || 0))} as an expense
            </label>
          )}

          <Field label="Note"><Input value={note} onChange={(e) => setNote(e.target.value)} data-testid="stock-note-input" /></Field>

          <div className="rounded-md bg-muted/50 px-3 py-2 text-xs text-muted-foreground" data-testid="stock-action-summary">
            On hand after: <span className="font-mono font-semibold text-foreground">{p.quantity_on_hand + (removing ? -n : n)}</span>
            {action === "sell" && <> · income <span className="font-mono font-semibold text-foreground">{fmtMoney(n * (Number(price) || 0))}</span></>}
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} data-testid="stock-cancel-button">Cancel</Button>
            <Button type="submit" disabled={m.isPending} data-testid="stock-submit-button">
              {m.isPending && <Loader2 className="animate-spin" />} {TITLES[action]}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
