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
import { Plus, Check, PackageCheck, FileText, X } from "lucide-react";
import { useTranslation } from "react-i18next";
const ST_CLS = {
  draft: "bg-gray-500/15 text-gray-400",
  approved: "bg-blue-500/15 text-blue-400",
  partially_received: "bg-amber-500/15 text-amber-400",
  received: "bg-sky-500/15 text-sky-400",
  billed: "bg-emerald-500/15 text-emerald-400",
  cancelled: "bg-rose-500/15 text-rose-400"
};
export default function PurchaseOrders() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const canManage = user?.role === "owner";
  const [loaded, setLoaded] = useState(false);
  const [pos, setPos] = useState([]);
  const [detail, setDetail] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [products, setProducts] = useState([]);
  const [po, setPo] = useState({
    vendor: "",
    expected_date: "",
    notes: "",
    lines: [{
      product_id: "",
      quantity: 1,
      unit_cost: ""
    }]
  });
  const reload = () => {
    api.get("/purchase-orders").then(setPos).catch(e => toast.error(errMsg(e)));
    api.get("/products").then(setProducts).catch(() => {});
  };
  useEffect(() => {
    reload();
    setLoaded(true);
  }, []);
  const openDetail = p => api.get(`/purchase-orders/${p.id}`).then(setDetail).catch(e => toast.error(errMsg(e)));
  const addLine = () => setPo({
    ...po,
    lines: [...po.lines, {
      product_id: "",
      quantity: 1,
      unit_cost: ""
    }]
  });
  const create = async () => {
    const lines = po.lines.filter(l => l.product_id && l.quantity > 0 && parseFloat(l.unit_cost) >= 0);
    if (!po.vendor.trim() || !lines.length) {
      toast.error(t("purchaseorders.vendorAndAtLeastOneValid"));
      return;
    }
    try {
      const r = await api.post("/purchase-orders", {
        ...po,
        lines: lines.map(l => ({
          ...l,
          unit_cost: parseFloat(l.unit_cost)
        }))
      });
      toast.success(`PO ${r.number} created`);
      setCreateOpen(false);
      setPo({
        vendor: "",
        expected_date: "",
        notes: "",
        lines: [{
          product_id: "",
          quantity: 1,
          unit_cost: ""
        }]
      });
      reload();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const act = async (id, action, body) => {
    try {
      const r = await api.post(`/purchase-orders/${id}/${action}`, body || {});
      const d = action === "convert-to-bill" ? r.po : r;
      setDetail(d);
      reload();
      toast.success(action === "convert-to-bill" ? `Bill ${r.bill.id.slice(0, 8)} created` : "Done");
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const receiveAll = p => {
    const lines = p.lines.filter(l => l.received_qty < l.quantity).map(l => ({
      product_id: l.product_id,
      quantity: l.quantity - l.received_qty
    }));
    if (!lines.length) {
      toast.error(t("purchaseorders.nothingLeftToReceive"));
      return;
    }
    if (confirm(`Receive ${lines.length} line(s) into inventory?`)) act(p.id, "receive", {
      lines
    });
  };
  if (!loaded) return <Loading />;
  return <div className="space-y-6">
      <PageHeader title={t("purchaseorders.purchaseOrders")} subtitle="Order stock, receive it, pay the bill — the full procurement loop." actions={canManage && <Button onClick={() => setCreateOpen(true)} data-testid="po-add"><Plus size={14} className="mr-1" />{t("purchaseorders.newPO")}</Button>} />
      <div className="grid lg:grid-cols-[1fr_440px] gap-6">
        <Card className="p-0 overflow-hidden h-fit">
          <table className="w-full text-sm">
            <thead><tr className="text-left text-gray-500 border-b border-[#222838]">
              <th className="px-4 py-3 font-medium">{t("purchaseorders.po")}</th><th className="px-4 py-3 font-medium">{t("purchaseorders.vendor")}</th>
              <th className="px-4 py-3 font-medium text-right">{t("purchaseorders.total")}</th><th className="px-4 py-3 font-medium">{t("purchaseorders.status")}</th>
            </tr></thead>
            <tbody>
              {pos.map(p => <tr key={p.id} className={`border-b border-[#1a2030] hover:bg-[#141926] cursor-pointer ${detail?.id === p.id ? "bg-[#141926]" : ""}`} onClick={() => openDetail(p)}>
                  <td className="px-4 py-3 font-medium">{p.number}</td>
                  <td className="px-4 py-3 text-gray-400">{p.vendor}</td>
                  <td className="px-4 py-3 text-right font-mono">{money(p.total_cents)}</td>
                  <td className="px-4 py-3"><span className={`text-xs px-2 py-0.5 rounded-full capitalize ${ST_CLS[p.status]}`}>{p.status.replace("_", " ")}</span></td>
                </tr>)}
              {!pos.length && <tr><td colSpan={4} className="px-4 py-8 text-center text-gray-500 text-sm">{t("purchaseorders.noPurchaseOrdersYet")}</td></tr>}
            </tbody>
          </table>
        </Card>
        <div>
          {!detail && <Card className="p-10 text-center text-gray-500 text-sm">{t("purchaseorders.selectAPOToWorkIt")}</Card>}
          {detail && <Card className="p-5 space-y-4">
              <div className="flex items-start justify-between">
                <div><div className="font-medium text-lg">{detail.number}</div>
                  <div className="text-xs text-gray-500">{detail.vendor}{detail.expected_date ? ` · expected ${detail.expected_date}` : ""}</div></div>
                <span className={`text-xs px-2 py-0.5 rounded-full capitalize ${ST_CLS[detail.status]}`}>{detail.status.replace("_", " ")}</span>
              </div>
              <div className="space-y-1.5">
                {detail.lines.map((l, i) => <div key={i} className="flex justify-between text-xs bg-[#0F1218] rounded p-2">
                    <span className="text-gray-300">{l.product_name} × {l.quantity}</span>
                    <span className="font-mono">{money(l.quantity * l.unit_cost_cents)}</span>
                  </div>)}
                <div className="flex justify-between text-xs pt-1">
                  <span className="text-gray-500">{t("purchaseorders.receivedSoFar")}</span>
                  <span className="font-mono">{money(detail.received_cents)} {t("purchaseorders.of")} {money(detail.total_cents)}</span>
                </div>
              </div>
              {canManage && <div className="flex gap-2 flex-wrap">
                  {detail.status === "draft" && <Button size="sm" onClick={() => act(detail.id, "approve")}><Check size={13} className="mr-1" />{t("purchaseorders.approve")}</Button>}
                  {["approved", "partially_received"].includes(detail.status) && <Button size="sm" onClick={() => receiveAll(detail)}><PackageCheck size={13} className="mr-1" />{t("purchaseorders.receiveAll")}</Button>}
                  {["received", "partially_received"].includes(detail.status) && !detail.bill_id && <Button size="sm" variant="outline" onClick={() => {
              if (confirm("Create vendor bill from received goods?")) act(detail.id, "convert-to-bill", {});
            }}>
                      <FileText size={13} className="mr-1" />{t("purchaseorders.convertToBill")}</Button>}
                  {["draft", "approved"].includes(detail.status) && <Button size="sm" variant="ghost" onClick={() => {
              if (confirm("Cancel this PO?")) act(detail.id, "cancel", {});
            }}>
                      <X size={13} className="mr-1" />{t("purchaseorders.cancel")}</Button>}
                </div>}
              {detail.bill_id && <p className="text-xs text-emerald-400">{t("purchaseorders.billedSeeBills")}</p>}
              {detail.notes && <p className="text-xs text-gray-600">{detail.notes}</p>}
            </Card>}
        </div>
      </div>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader><DialogTitle>{t("purchaseorders.newPurchaseOrder")}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("purchaseorders.vendor2")}</Label><Input value={po.vendor} onChange={e => setPo({
              ...po,
              vendor: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("purchaseorders.expectedDate")}</Label><Input type="date" value={po.expected_date} onChange={e => setPo({
              ...po,
              expected_date: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
            <div className="col-span-2"><Label className="text-xs">{t("purchaseorders.notes")}</Label><Input value={po.notes} onChange={e => setPo({
              ...po,
              notes: e.target.value
            })} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>
          <div className="space-y-2 mt-2">
            <div className="text-xs text-gray-500">{t("purchaseorders.lines")}</div>
            {po.lines.map((l, i) => <div key={i} className="grid grid-cols-[1fr_70px_90px] gap-2">
                <select value={l.product_id} onChange={e => {
              const ls = [...po.lines];
              ls[i].product_id = e.target.value;
              setPo({
                ...po,
                lines: ls
              });
            }} className="bg-[#0F1218] border border-[#222838] rounded-md h-9 text-xs px-2">
                  <option value="">{t("purchaseorders.product")}</option>
                  {products.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
                <Input type="number" min="1" value={l.quantity} onChange={e => {
              const ls = [...po.lines];
              ls[i].quantity = parseInt(e.target.value) || 0;
              setPo({
                ...po,
                lines: ls
              });
            }} placeholder={t("purchaseorders.qty")} className="bg-[#0F1218] border-[#222838] h-9 text-xs" />
                <Input type="number" min="0" step="0.01" value={l.unit_cost} onChange={e => {
              const ls = [...po.lines];
              ls[i].unit_cost = e.target.value;
              setPo({
                ...po,
                lines: ls
              });
            }} placeholder={t("purchaseorders.cost")} className="bg-[#0F1218] border-[#222838] h-9 text-xs font-mono" />
              </div>)}
            <Button size="sm" variant="ghost" onClick={addLine}>{t("purchaseorders.addLine")}</Button>
          </div>
          <DialogFooter><Button variant="ghost" onClick={() => setCreateOpen(false)}>{t("purchaseorders.cancel")}</Button><Button onClick={create} data-testid="po-create">{t("purchaseorders.createPO")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>;
}
