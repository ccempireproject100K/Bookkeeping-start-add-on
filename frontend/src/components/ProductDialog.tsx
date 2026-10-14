import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { ImagePlus, Loader2, X } from "lucide-react";
import { apiGet, apiPost, apiPut } from "@/lib/api";
import { errMsg } from "@/lib/format";
import { invalidateInventory } from "@/lib/invalidate";
import type { Product, ProductCreate, ProductType, Supplier, TrackingMode } from "@/lib/types";
import { Field, NativeSelect } from "@/components/Common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

interface Props {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  product?: Product | null;
  initialBarcode?: string;
  onSaved?: (p: Product) => void;
}

const blank = (barcode = ""): ProductCreate => ({
  name: "", type: "retail", category: "", brand: "", sku: "", barcode, sell_price: 0, unit: "each", tracking_mode: "none",
  reorder_point: 5, reorder_qty: null, supplier_id: null, image: null, description: "",
  opening_qty: 0, opening_unit_cost: 0, opening_lot_number: "", opening_expiry_date: "", opening_serials: [],
});

// Downscale an uploaded photo to a ~320px JPEG data URL so it stays small in the DB.
async function toThumb(file: File): Promise<string> {
  const url = URL.createObjectURL(file);
  const img = new Image();
  await new Promise<void>((res, rej) => { img.onload = () => res(); img.onerror = () => rej(new Error("bad image")); img.src = url; });
  const scale = Math.min(1, 320 / Math.max(img.width, img.height));
  const c = document.createElement("canvas");
  c.width = Math.round(img.width * scale);
  c.height = Math.round(img.height * scale);
  c.getContext("2d")?.drawImage(img, 0, 0, c.width, c.height);
  URL.revokeObjectURL(url);
  return c.toDataURL("image/jpeg", 0.8);
}

export default function ProductDialog({ open, onOpenChange, product, initialBarcode, onSaved }: Props) {
  const [f, setF] = useState<ProductCreate>(blank());
  const [serialText, setSerialText] = useState("");
  const editing = !!product;
  const { data: suppliers = [] } = useQuery({ queryKey: ["suppliers"], queryFn: () => apiGet<Supplier[]>("/inventory/suppliers"), enabled: open });

  useEffect(() => {
    if (!open) return;
    setSerialText("");
    setF(product ? { ...blank(), ...product, sku: product.sku ?? "", barcode: product.barcode ?? "" } : blank(initialBarcode ?? ""));
  }, [open, product, initialBarcode]);

  const set = <K extends keyof ProductCreate>(k: K, v: ProductCreate[K]) => setF((s) => ({ ...s, [k]: v }));
  const num = (v: string) => (v === "" ? 0 : Number(v));

  const m = useMutation({
    mutationFn: () => {
      const base = {
        name: f.name, type: f.type, category: f.category, brand: f.brand, sku: f.sku || null, barcode: f.barcode || null,
        sell_price: f.type === "supply" ? 0 : f.sell_price, unit: f.unit || "each", tracking_mode: f.tracking_mode,
        reorder_point: f.reorder_point, reorder_qty: f.reorder_qty, supplier_id: f.supplier_id || null, image: f.image,
        description: f.description,
      };
      if (product) return apiPut<Product>(`/inventory/products/${product.id}`, { ...base, active: product.active });
      const serials = serialText.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
      return apiPost<Product>("/inventory/products", {
        ...base, opening_qty: f.tracking_mode === "serial" ? serials.length : f.opening_qty, opening_unit_cost: f.opening_unit_cost,
        opening_lot_number: f.opening_lot_number || null, opening_expiry_date: f.opening_expiry_date || null, opening_serials: serials,
      });
    },
    onSuccess: (p) => {
      toast.success(editing ? "Product updated" : `${p.name} created`);
      invalidateInventory();
      onOpenChange(false);
      onSaved?.(p);
    },
    onError: (e) => toast.error(errMsg(e)),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92vh] overflow-y-auto sm:max-w-2xl" data-testid="product-dialog">
        <form onSubmit={(e) => { e.preventDefault(); m.mutate(); }} className="space-y-5">
          <DialogHeader>
            <DialogTitle>{editing ? `Edit ${product?.name}` : "New product"}</DialogTitle>
            <DialogDescription>
              {editing ? "Quantity never changes here — use restock, sell or adjust so the ledger stays exact." : "Opening stock is recorded as the first ledger movement."}
            </DialogDescription>
          </DialogHeader>

          <div className="flex gap-4">
            <label className="group relative grid size-24 shrink-0 cursor-pointer place-items-center overflow-hidden rounded-lg border border-dashed border-border bg-muted/40 transition-colors duration-150 hover:border-primary" data-testid="product-image-upload">
              {f.image ? <img src={f.image} alt="" className="size-full object-cover" /> : <ImagePlus className="size-6 text-muted-foreground" />}
              <input type="file" accept="image/*" className="sr-only" data-testid="product-image-input"
                onChange={async (e) => { const file = e.target.files?.[0]; if (file) set("image", await toThumb(file)); }} />
              {f.image && (
                <button type="button" onClick={(e) => { e.preventDefault(); set("image", null); }} data-testid="product-image-remove"
                  className="absolute right-1 top-1 rounded-full bg-background/90 p-0.5"><X className="size-3" /></button>
              )}
            </label>
            <div className="grid flex-1 gap-3 sm:grid-cols-2">
              <Field label="Name" className="sm:col-span-2">
                <Input required value={f.name} onChange={(e) => set("name", e.target.value)} data-testid="product-name-input" />
              </Field>
              <Field label="Type">
                <NativeSelect value={f.type} onChange={(e) => set("type", e.target.value as ProductType)} data-testid="product-type-select">
                  <option value="retail">Retail (sold to clients)</option>
                  <option value="supply">Supply (used in services)</option>
                </NativeSelect>
              </Field>
              <Field label="Tracking" hint={editing && product?.quantity_on_hand ? "Change only when on-hand is zero" : undefined}>
                <NativeSelect value={f.tracking_mode} onChange={(e) => set("tracking_mode", e.target.value as TrackingMode)} data-testid="product-tracking-select">
                  <option value="none">Quantity only</option>
                  <option value="lot">Lot / batch + expiry</option>
                  <option value="serial">Serial number per unit</option>
                </NativeSelect>
              </Field>
            </div>
          </div>

          <div className="grid gap-3 grid-cols-2 sm:grid-cols-4">
            <Field label="Category"><Input value={f.category} onChange={(e) => set("category", e.target.value)} data-testid="product-category-input" /></Field>
            <Field label="Brand"><Input value={f.brand} onChange={(e) => set("brand", e.target.value)} data-testid="product-brand-input" /></Field>
            <Field label="SKU"><Input value={f.sku ?? ""} onChange={(e) => set("sku", e.target.value)} className="font-mono" data-testid="product-sku-input" /></Field>
            <Field label="Barcode"><Input value={f.barcode ?? ""} onChange={(e) => set("barcode", e.target.value)} className="font-mono" data-testid="product-barcode-input" /></Field>
            {f.type === "retail" && (
              <Field label="Sell price ($)"><Input type="number" step="0.01" min={0} value={f.sell_price} onChange={(e) => set("sell_price", num(e.target.value))} className="font-mono" data-testid="product-price-input" /></Field>
            )}
            <Field label="Unit"><Input value={f.unit} onChange={(e) => set("unit", e.target.value)} data-testid="product-unit-input" /></Field>
            <Field label="Reorder at"><Input type="number" min={0} value={f.reorder_point} onChange={(e) => set("reorder_point", num(e.target.value))} className="font-mono" data-testid="product-reorder-input" /></Field>
            <Field label="Supplier">
              <NativeSelect value={f.supplier_id ?? ""} onChange={(e) => set("supplier_id", e.target.value || null)} data-testid="product-supplier-select">
                <option value="">None</option>
                {suppliers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </NativeSelect>
            </Field>
          </div>

          {!editing && (
            <div className="rounded-lg border border-border bg-muted/30 p-4">
              <div className="label-caps mb-3">Opening stock</div>
              <div className="grid gap-3 grid-cols-2 sm:grid-cols-4">
                {f.tracking_mode !== "serial" && (
                  <Field label="Quantity"><Input type="number" min={0} value={f.opening_qty} onChange={(e) => set("opening_qty", num(e.target.value))} className="font-mono" data-testid="product-opening-qty-input" /></Field>
                )}
                <Field label="Unit cost ($)"><Input type="number" step="0.01" min={0} value={f.opening_unit_cost} onChange={(e) => set("opening_unit_cost", num(e.target.value))} className="font-mono" data-testid="product-opening-cost-input" /></Field>
                {f.tracking_mode === "lot" && (
                  <>
                    <Field label="Lot number"><Input value={f.opening_lot_number ?? ""} onChange={(e) => set("opening_lot_number", e.target.value)} className="font-mono" data-testid="product-opening-lot-input" /></Field>
                    <Field label="Expiry"><Input type="date" value={f.opening_expiry_date ?? ""} onChange={(e) => set("opening_expiry_date", e.target.value)} data-testid="product-opening-expiry-input" /></Field>
                  </>
                )}
                {f.tracking_mode === "serial" && (
                  <Field label="Serial numbers (one per line)" className="col-span-2 sm:col-span-3">
                    <Textarea rows={3} value={serialText} onChange={(e) => setSerialText(e.target.value)} className="font-mono" data-testid="product-opening-serials-input" />
                  </Field>
                )}
              </div>
            </div>
          )}

          <Field label="Description"><Textarea rows={2} value={f.description} onChange={(e) => set("description", e.target.value)} data-testid="product-description-input" /></Field>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} data-testid="product-cancel-button">Cancel</Button>
            <Button type="submit" disabled={m.isPending} data-testid="product-save-button">
              {m.isPending && <Loader2 className="animate-spin" />} {editing ? "Save changes" : "Create product"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
