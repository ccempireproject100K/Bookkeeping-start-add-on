import { useEffect, useMemo, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, dollars } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { Scissors, Plus, Minus, Trash2, Banknote, CreditCard, ShoppingBag } from "lucide-react";

const SERVICES = [
  { service_type: "haircut", name: "Signature Haircut", price: 35 },
  { service_type: "haircut", name: "Skin Fade", price: 40 },
  { service_type: "beard", name: "Beard Trim", price: 20 },
  { service_type: "service", name: "Hot Towel Shave", price: 40 },
];

export default function QuickSell() {
  const { user } = useAuth();
  const [products, setProducts] = useState(null);
  const [team, setTeam] = useState([]);
  const [barberId, setBarberId] = useState("");
  const [cart, setCart] = useState([]); // {kind, id, name, price, qty, product_id?}
  const [payment, setPayment] = useState("cash");
  const [discount, setDiscount] = useState("");
  const [busy, setBusy] = useState(false);

  const rate = user?.tenant?.settings?.sales_tax_rate || 0;

  useEffect(() => {
    api.get("/products").then((p) => setProducts(p.filter((x) => x.type === "retail")));
    api.get("/auth/team").then((t) => setTeam(t.filter((u) => u.role === "barber")));
  }, []);

  const addService = (s) => setCart((c) => [...c, { kind: "service", ...s, qty: 1 }]);
  const addProduct = (p) => setCart((c) => {
    const ex = c.find((i) => i.product_id === p.id);
    if (ex) return c.map((i) => i.product_id === p.id ? { ...i, qty: i.qty + 1 } : i);
    return [...c, { kind: "product", product_id: p.id, name: p.name, price: p.price_cents / 100, qty: 1 }];
  });
  const changeQty = (idx, d) => setCart((c) => c.map((i, x) => x === idx ? { ...i, qty: Math.max(1, i.qty + d) } : i));
  const removeItem = (idx) => setCart((c) => c.filter((_, x) => x !== idx));

  const totals = useMemo(() => {
    const product_subtotal = cart.filter((i) => i.kind === "product").reduce((s, i) => s + i.price * i.qty, 0);
    const service_subtotal = cart.filter((i) => i.kind === "service").reduce((s, i) => s + i.price * i.qty, 0);
    const subtotal = product_subtotal + service_subtotal;
    const disc = Math.min(parseFloat(discount) || 0, subtotal);
    const discOnProd = subtotal ? (disc * product_subtotal) / subtotal : 0;
    const tax = Math.max(product_subtotal - discOnProd, 0) * rate;
    const barber = team.find((b) => b.id === barberId);
    const commission = service_subtotal * (barber?.commission_rate || 0);
    return { product_subtotal, service_subtotal, subtotal, disc, tax, total: subtotal - disc + tax, commission };
  }, [cart, discount, rate, barberId, team]);

  const complete = async () => {
    if (cart.length === 0) return toast.error("Cart is empty");
    setBusy(true);
    try {
      await api.post("/pos/sell", {
        barber_id: barberId || null,
        payment_method: payment,
        discount: parseFloat(discount) || 0,
        product_items: cart.filter((i) => i.kind === "product").map((i) => ({ product_id: i.product_id, qty: i.qty })),
        service_items: cart.filter((i) => i.kind === "service").map((i) => ({ service_type: i.service_type, name: i.name, price: i.price })),
      });
      toast.success(`Sale complete — ${dollars(totals.total)}`);
      setCart([]); setDiscount("");
      api.get("/products").then((p) => setProducts(p.filter((x) => x.type === "retail")));
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };

  if (!products) return <Loading />;

  return (
    <div className="animate-rise">
      <PageHeader title="Quick Sell" subtitle="Point of sale — posts a balanced sale entry" testid="quick-sell-page" />
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
        {/* Catalog */}
        <div className="lg:col-span-7 space-y-5">
          <Card>
            <h3 className="font-display font-semibold mb-3 flex items-center gap-2"><Scissors size={16} className="text-amber-400" /> Services</h3>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {SERVICES.map((s, i) => (
                <button key={i} onClick={() => addService(s)} data-testid={`pos-service-${i}`}
                  className="p-3 rounded-lg bg-[#0F1218] border border-[#222838] hover:border-amber-500/40 text-left transition-colors">
                  <div className="text-sm text-gray-100">{s.name}</div>
                  <div className="font-mono text-amber-400 text-sm mt-1">{dollars(s.price)}</div>
                </button>
              ))}
            </div>
          </Card>
          <Card>
            <h3 className="font-display font-semibold mb-3 flex items-center gap-2"><ShoppingBag size={16} className="text-amber-400" /> Retail Products</h3>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
              {products.map((p) => (
                <button key={p.id} onClick={() => addProduct(p)} data-testid="pos-product-card"
                  className="p-3 rounded-lg bg-[#0F1218] border border-[#222838] hover:border-amber-500/40 text-left transition-colors disabled:opacity-40"
                  disabled={p.quantity_on_hand <= 0}>
                  <div className="text-sm text-gray-100 truncate">{p.name}</div>
                  <div className="flex items-center justify-between mt-1">
                    <span className="font-mono text-amber-400 text-sm">{money(p.price_cents)}</span>
                    <span className="text-[10px] text-gray-500">{p.quantity_on_hand} left</span>
                  </div>
                </button>
              ))}
            </div>
          </Card>
        </div>

        {/* Cart */}
        <div className="lg:col-span-5">
          <Card className="sticky top-20">
            <div className="mb-3">
              <label className="text-xs text-gray-400">Barber</label>
              <Select value={barberId} onValueChange={setBarberId}>
                <SelectTrigger data-testid="pos-barber-select" className="bg-[#0F1218] border-[#222838] mt-1"><SelectValue placeholder="Select barber" /></SelectTrigger>
                <SelectContent className="bg-[#14171E] border-[#222838]">
                  {team.map((b) => <SelectItem key={b.id} value={b.id}>{b.name} ({Math.round((b.commission_rate || 0) * 100)}%)</SelectItem>)}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2 max-h-[280px] overflow-y-auto mb-3">
              {cart.length === 0 && <div className="text-center text-gray-600 text-sm py-8">Cart is empty</div>}
              {cart.map((i, idx) => (
                <div key={idx} className="flex items-center gap-2 p-2 rounded-lg bg-[#0F1218] border border-[#1b2230]">
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-gray-100 truncate">{i.name}</div>
                    <div className="font-mono text-xs text-amber-400">{dollars(i.price)}</div>
                  </div>
                  <div className="flex items-center gap-1">
                    <button onClick={() => changeQty(idx, -1)} className="w-6 h-6 grid place-items-center rounded bg-[#1b2230] text-gray-300"><Minus size={12} /></button>
                    <span className="w-6 text-center text-sm font-mono">{i.qty}</span>
                    <button onClick={() => changeQty(idx, 1)} className="w-6 h-6 grid place-items-center rounded bg-[#1b2230] text-gray-300"><Plus size={12} /></button>
                    <button onClick={() => removeItem(idx)} className="ml-1 text-gray-600 hover:text-rose-400"><Trash2 size={14} /></button>
                  </div>
                </div>
              ))}
            </div>

            <div className="flex items-center gap-2 mb-3">
              <button data-testid="pos-payment-cash-btn" onClick={() => setPayment("cash")}
                className={`flex-1 py-2 rounded-lg text-sm flex items-center justify-center gap-1.5 border transition-colors ${payment === "cash" ? "bg-amber-500/10 text-amber-400 border-amber-500/40" : "border-[#222838] text-gray-400"}`}>
                <Banknote size={15} /> Cash
              </button>
              <button data-testid="pos-payment-card-btn" onClick={() => setPayment("card")}
                className={`flex-1 py-2 rounded-lg text-sm flex items-center justify-center gap-1.5 border transition-colors ${payment === "card" ? "bg-amber-500/10 text-amber-400 border-amber-500/40" : "border-[#222838] text-gray-400"}`}>
                <CreditCard size={15} /> Card
              </button>
            </div>

            <div className="mb-3">
              <label className="text-xs text-gray-400">Discount ($)</label>
              <Input data-testid="pos-discount" value={discount} onChange={(e) => setDiscount(e.target.value)} placeholder="0.00" className="bg-[#0F1218] border-[#222838] mt-1 font-mono" />
            </div>

            <div className="space-y-1 text-sm border-t border-[#222838] pt-3 font-mono">
              <Row label="Subtotal" value={dollars(totals.subtotal)} />
              {totals.disc > 0 && <Row label="Discount" value={`-${dollars(totals.disc)}`} tone="text-rose-400" />}
              <Row label="Sales tax" value={dollars(totals.tax)} />
              {barberId && <Row label="Barber commission" value={dollars(totals.commission)} tone="text-sky-400" testid="pos-commission-split-display" />}
              <div className="flex justify-between pt-2 text-base font-semibold">
                <span>Total</span>
                <span className="text-amber-400" data-testid="pos-cart-total">{dollars(totals.total)}</span>
              </div>
            </div>

            <Button data-testid="btn-complete-sale" disabled={busy || cart.length === 0} onClick={complete}
              className="w-full mt-4 bg-amber-600 hover:bg-amber-500 text-black font-semibold">
              {busy ? "Processing…" : "Complete Sale"}
            </Button>
          </Card>
        </div>
      </div>
    </div>
  );
}

function Row({ label, value, tone = "text-gray-300", testid }) {
  return (
    <div className="flex justify-between" data-testid={testid}>
      <span className="text-gray-500">{label}</span>
      <span className={tone}>{value}</span>
    </div>
  );
}
