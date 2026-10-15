import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { toast } from "sonner";
import { Plus, PackagePlus, Sliders, AlertTriangle } from "lucide-react";
import { useTranslation } from "react-i18next";
export default function Inventory() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const isOwner = user?.role === "owner";
  const [products, setProducts] = useState(null);
  const [addOpen, setAddOpen] = useState(false);
  const [restockFor, setRestockFor] = useState(null);
  const [adjustFor, setAdjustFor] = useState(null);
  const [form, setForm] = useState({
    name: "",
    type: "retail",
    price: "",
    unit_cost: "",
    quantity_on_hand: ""
  });
  const [restock, setRestock] = useState({
    quantity: "",
    unit_cost: ""
  });
  const [adjust, setAdjust] = useState({
    quantity_delta: "",
    reason: ""
  });
  const [lowStock, setLowStock] = useState([]);
  const load = () => {
    api.get("/products").then(setProducts).catch(() => setProducts([]));
    api.get("/products/low-stock").then(setLowStock).catch(() => setLowStock([]));
  };
  useEffect(() => {
    load();
  }, []);
  const createProduct = async () => {
    try {
      await api.post("/products", {
        name: form.name,
        type: form.type,
        revenue_code: form.type === "retail" ? "4100" : "4100",
        price: parseFloat(form.price) || 0,
        unit_cost: parseFloat(form.unit_cost) || 0,
        quantity_on_hand: parseInt(form.quantity_on_hand) || 0
      });
      toast.success(t("inventory.productAdded"));
      setAddOpen(false);
      setForm({
        name: "",
        type: "retail",
        price: "",
        unit_cost: "",
        quantity_on_hand: ""
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const doRestock = async () => {
    try {
      await api.post(`/products/${restockFor.id}/restock`, {
        quantity: parseInt(restock.quantity),
        unit_cost: parseFloat(restock.unit_cost)
      });
      toast.success(t("inventory.restockedInventoryPostedToLedger"));
      setRestockFor(null);
      setRestock({
        quantity: "",
        unit_cost: ""
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const doAdjust = async () => {
    try {
      await api.post(`/products/${adjustFor.id}/adjust`, {
        quantity_delta: parseInt(adjust.quantity_delta),
        reason: adjust.reason
      });
      toast.success(t("inventory.adjustmentPosted"));
      setAdjustFor(null);
      setAdjust({
        quantity_delta: "",
        reason: ""
      });
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!products) return <Loading />;
  const totalValue = products.reduce((s, p) => s + p.quantity_on_hand * p.unit_cost_cents, 0);
  return <div className="animate-rise">
      <PageHeader title={t("inventory.inventory")} subtitle={`${products.length} products • ${money(totalValue)} at cost`} testid="inventory-page">
        {isOwner && <Button data-testid="btn-add-product" onClick={() => setAddOpen(true)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" /> {t("inventory.addProduct")}</Button>}
      </PageHeader>

      {lowStock.length > 0 && <div className="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 p-4" data-testid="low-stock-alert">
          <div className="flex items-center gap-2 text-sm font-semibold text-amber-300 mb-2">
            <AlertTriangle size={15} /> {t("inventory.lowStock")} {lowStock.length} {t("inventory.item")}{lowStock.length === 1 ? "" : "s"} {t("inventory.atOrBelowReorderPoint")}</div>
          <div className="flex flex-wrap gap-2">
            {lowStock.map(p => <button key={p.id} onClick={() => {
          setRestockFor(p);
          setRestock({
            quantity: "",
            unit_cost: ""
          });
        }} className="rounded-full border border-amber-500/30 bg-[#14171E] px-3 py-1 text-xs text-gray-200 hover:border-amber-400" title={t("inventory.openRestock")} data-testid={`low-stock-${p.id}`}>
                {p.name} — <span className="font-mono text-amber-300">{p.quantity_on_hand}</span> {t("inventory.left")}</button>)}
          </div>
        </div>}

      <Card className="p-0 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="table-inventory">
            <thead>
              <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-5 py-3 text-left font-medium">{t("inventory.product")}</th>
                <th className="px-5 py-3 text-left font-medium">{t("inventory.type")}</th>
                <th className="px-5 py-3 text-right font-medium">{t("inventory.price")}</th>
                <th className="px-5 py-3 text-right font-medium">{t("inventory.avgCost")}</th>
                <th className="px-5 py-3 text-right font-medium">{t("inventory.onHand")}</th>
                <th className="px-5 py-3 text-right font-medium">{t("inventory.value")}</th>
                {isOwner && <th className="px-5 py-3 text-right font-medium">{t("inventory.actions")}</th>}
              </tr>
            </thead>
            <tbody>
              {products.map(p => <tr key={p.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                  <td className="px-5 py-3 text-gray-100">{p.name}</td>
                  <td className="px-5 py-3"><span className="text-xs px-2 py-0.5 rounded-full bg-[#222838] text-gray-300 capitalize">{p.type}</span></td>
                  <td className="px-5 py-3 text-right font-mono text-gray-300">{money(p.price_cents)}</td>
                  <td className="px-5 py-3 text-right font-mono text-gray-400">{money(p.unit_cost_cents)}</td>
                  <td className="px-5 py-3 text-right font-mono text-gray-200">{p.quantity_on_hand}</td>
                  <td className="px-5 py-3 text-right font-mono text-amber-400">{money(p.quantity_on_hand * p.unit_cost_cents)}</td>
                  {isOwner && <td className="px-5 py-3 text-right space-x-1">
                      <Button size="sm" variant="ghost" className="h-7 text-emerald-400 hover:bg-emerald-500/10" onClick={() => setRestockFor(p)} data-testid={`restock-${p.id}`}><PackagePlus size={14} className="mr-1" />{t("inventory.restock")}</Button>
                      <Button size="sm" variant="ghost" className="h-7 text-amber-400 hover:bg-amber-500/10" onClick={() => setAdjustFor(p)} data-testid={`adjust-${p.id}`}><Sliders size={14} className="mr-1" />{t("inventory.adjust")}</Button>
                    </td>}
                </tr>)}
              {products.length === 0 && <tr><td colSpan={isOwner ? 7 : 6} className="px-5 py-12 text-center text-gray-500">{t("inventory.noProducts")}</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Add product */}
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("inventory.addProduct")}</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div><Label className="text-xs">{t("inventory.name")}</Label><Input data-testid="product-name" value={form.name} onChange={e => setForm({
              ...form,
              name: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div className="grid grid-cols-2 gap-3">
              <div><Label className="text-xs">{t("inventory.salePrice")}</Label><Input data-testid="product-price" value={form.price} onChange={e => setForm({
                ...form,
                price: e.target.value
              })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
              <div><Label className="text-xs">{t("inventory.unitCost")}</Label><Input data-testid="product-cost" value={form.unit_cost} onChange={e => setForm({
                ...form,
                unit_cost: e.target.value
              })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            </div>
            <div><Label className="text-xs">{t("inventory.openingQuantity")}</Label><Input data-testid="product-qty" value={form.quantity_on_hand} onChange={e => setForm({
              ...form,
              quantity_on_hand: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          </div>
          <DialogFooter><Button data-testid="product-save" onClick={createProduct} className="bg-amber-600 hover:bg-amber-500 text-black">{t("inventory.add")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Restock */}
      <Dialog open={!!restockFor} onOpenChange={o => !o && setRestockFor(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("inventory.restock")} {restockFor?.name}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("inventory.quantity")}</Label><Input data-testid="restock-qty" value={restock.quantity} onChange={e => setRestock({
              ...restock,
              quantity: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">{t("inventory.unitCost")}</Label><Input data-testid="restock-cost" value={restock.unit_cost} onChange={e => setRestock({
              ...restock,
              unit_cost: e.target.value
            })} className="bg-[#0F1218] border-[#222838] font-mono" /></div>
          </div>
          <DialogFooter><Button data-testid="restock-save" onClick={doRestock} className="bg-emerald-600 hover:bg-emerald-500 text-black">{t("inventory.restock")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Adjust */}
      <Dialog open={!!adjustFor} onOpenChange={o => !o && setAdjustFor(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838]">
          <DialogHeader><DialogTitle>{t("inventory.adjust")} {adjustFor?.name}</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div><Label className="text-xs">{t("inventory.quantityChange")}</Label><Input data-testid="adjust-delta" value={adjust.quantity_delta} onChange={e => setAdjust({
              ...adjust,
              quantity_delta: e.target.value
            })} placeholder="-2" className="bg-[#0F1218] border-[#222838] font-mono" /></div>
            <div><Label className="text-xs">{t("inventory.reason")}</Label><Input data-testid="adjust-reason" value={adjust.reason} onChange={e => setAdjust({
              ...adjust,
              reason: e.target.value
            })} placeholder={t("inventory.damagedMiscount")} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <DialogFooter><Button data-testid="adjust-save" onClick={doAdjust} className="bg-amber-600 hover:bg-amber-500 text-black">{t("inventory.postAdjustment")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
