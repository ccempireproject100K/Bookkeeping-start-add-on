import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import StockScanInput from "@/components/StockScanInput";
import { toast } from "sonner";
import { Barcode, PackageCheck, Search, ShieldCheck, AlarmClock, MapPin, FileWarning } from "lucide-react";

const WARRANTY_LABEL = {
  covered: ["Covered", "text-emerald-300 border-emerald-500/40 bg-emerald-500/10"],
  expiring_soon: ["Expiring ≤30d", "text-amber-300 border-amber-500/40 bg-amber-500/10"],
  out_of_warranty: ["Out of warranty", "text-rose-300 border-rose-500/40 bg-rose-500/10"],
  not_installed: ["Not installed", "text-gray-300 border-[#222838] bg-[#14171E]"],
  no_warranty: ["No warranty", "text-gray-300 border-[#222838] bg-[#14171E]"],
};

export function WarrantyChip({ status }) {
  const [label, cls] = WARRANTY_LABEL[status] || WARRANTY_LABEL.no_warranty;
  return <span className={`text-[11px] px-2 py-0.5 rounded-full border ${cls}`}>{label}</span>;
}

/**
 * Lot & serial tracking panel (Top-20 #17): receive serials/lots,
 * assign specific units to a job/customer, one-lookup unit story with
 * its warranty clock, expiring-lots list, parts-at-a-site report,
 * warranty counts, and recall flagging.
 */
export default function TrackingPanel({ products, onChanged }) {
  const tracked = products.filter((p) => p.tracking_mode && p.tracking_mode !== "none");
  const [pid, setPid] = useState("");
  const product = tracked.find((p) => p.id === pid) || tracked[0] || null;

  const [serialText, setSerialText] = useState("");
  const [lotNumber, setLotNumber] = useState("");
  const [lotQty, setLotQty] = useState("");
  const [expiry, setExpiry] = useState("");
  const [assignSerials, setAssignSerials] = useState("");
  const [assignQty, setAssignQty] = useState("");
  const [jobId, setJobId] = useState("");
  const [customerId, setCustomerId] = useState("");
  const [scanCode, setScanCode] = useState("");
  const [lookup, setLookup] = useState(null);
  const [expiring, setExpiring] = useState([]);
  const [site, setSite] = useState(null);
  const [warranty, setWarranty] = useState(null);
  const [recall, setRecall] = useState({ from: "", to: "", lot: "", reason: "" });
  const [recallResult, setRecallResult] = useState(null);

  const loadSide = () => {
    api.get("/inventory/expiring?days=30").then(setExpiring).catch(() => setExpiring([]));
    api.get("/inventory/warranty").then(setWarranty).catch(() => setWarranty(null));
  };
  useEffect(loadSide, []);

  if (!tracked.length) {
    return (
      <Card className="mt-4 p-5" >
        <div className="flex items-center gap-2 text-sm font-semibold text-gray-200"><Barcode size={15} /> Lot &amp; Serial Tracking</div>
        <p className="mt-1 text-sm text-gray-400">No tracked products yet. Add a product with tracking set to <b>Serial</b> or <b>Lot</b> and this panel lights up — untracked products keep working exactly as before.</p>
      </Card>
    );
  }

  const serialsOf = (text) => text.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);

  const doReceive = async () => {
    try {
      if (product.tracking_mode === "serial") {
        const serials = serialsOf(serialText);
        await api.post("/inventory/receive", { product_id: product.id, quantity: serials.length, serials });
        setSerialText("");
      } else {
        await api.post("/inventory/receive", { product_id: product.id, quantity: parseInt(lotQty) || 0, lot_number: lotNumber, expiry_date: expiry || undefined });
        setLotNumber(""); setLotQty(""); setExpiry("");
      }
      toast.success("Received into tracked stock");
      loadSide(); onChanged && onChanged();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const doAssign = async () => {
    try {
      const body = { product_id: product.id, job_id: jobId || undefined, customer_id: customerId || undefined };
      if (product.tracking_mode === "serial") body.serials = serialsOf(assignSerials);
      else body.quantity = parseInt(assignQty) || 0;
      const res = await api.post("/inventory/assign", body);
      toast.success(`Assigned ${res.quantity} — ${product.tracking_mode === "lot" ? `lot ${res.lot.lot_number} (earliest expiry first)` : "units installed"}`);
      setAssignSerials(""); setAssignQty("");
      loadSide(); onChanged && onChanged();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const doLookup = async () => {
    try {
      const res = await api.post("/inventory/scan", { code: scanCode, action: "lookup" });
      if (res.result.found === "serial") setLookup({ kind: "serial", ...res.result });
      else if (res.result.found === "lot") setLookup({ kind: "lot", lots: res.result.lots });
      else { setLookup({ kind: "none" }); toast.error("That code isn't in tracked stock"); }
    } catch (e) { toast.error(errMsg(e)); }
  };

  const doSite = async () => {
    try {
      const q = jobId ? `job_id=${encodeURIComponent(jobId)}` : `customer_id=${encodeURIComponent(customerId)}`;
      setSite(await api.get(`/inventory/at?${q}`));
    } catch (e) { toast.error(errMsg(e)); }
  };

  const doRecall = async () => {
    try {
      const body = { reason: recall.reason, product_id: product.id };
      if (recall.lot) body.lot_number = recall.lot;
      else { body.serial_from = recall.from; body.serial_to = recall.to; }
      const res = await api.post("/inventory/recalls", body);
      setRecallResult(res);
      toast.success(`Recall flagged ${res.affected_count} item(s)`);
    } catch (e) { toast.error(errMsg(e)); }
  };

  const inp = "bg-[#0F1218] border-[#222838]";

  return (
    <Card className="mt-4 p-5" >
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-gray-100"><Barcode size={15} /> Lot &amp; Serial Tracking</div>
        <select data-testid="tracking-product" value={product.id} onChange={(e) => setPid(e.target.value)} className="bg-[#0F1218] border border-[#222838] rounded-md px-2 py-1.5 text-sm text-gray-200">
          {tracked.map((p) => <option key={p.id} value={p.id}>{p.name} — {p.tracking_mode}{p.warranty_months ? ` • ${p.warranty_months}mo warranty` : ""}</option>)}
        </select>
        {warranty && <span className="text-xs text-gray-400">Installed units: {Object.entries(warranty.counts).map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(" • ") || "none yet"}</span>}
      </div>

      <div className="grid md:grid-cols-2 gap-5 mt-4">
        <div className="space-y-3">
          <div className="text-xs uppercase tracking-wider text-gray-500 flex items-center gap-1"><PackageCheck size={13} /> Receive</div>
          {product.tracking_mode === "serial" ? (
            <div>
              <Label className="text-xs">Serial numbers — one per unit (paste or separate with spaces/commas)</Label>
              <textarea data-testid="receive-serials" value={serialText} onChange={(e) => setSerialText(e.target.value)} rows={3} className={`w-full rounded-md border px-3 py-2 text-sm font-mono text-gray-100 ${inp}`} placeholder={"e.g. ABC-001\nABC-002"} />
            </div>
          ) : (
            <div className="grid grid-cols-3 gap-2">
              <div><Label className="text-xs">Lot number</Label><Input data-testid="receive-lot" value={lotNumber} onChange={(e) => setLotNumber(e.target.value)} className={inp} /></div>
              <div><Label className="text-xs">Quantity</Label><Input data-testid="receive-lot-qty" value={lotQty} onChange={(e) => setLotQty(e.target.value)} className={`${inp} font-mono`} /></div>
              <div><Label className="text-xs">Expiry (optional)</Label><Input data-testid="receive-lot-expiry" type="date" value={expiry} onChange={(e) => setExpiry(e.target.value)} className={inp} /></div>
            </div>
          )}
          <Button data-testid="receive-save" onClick={doReceive} className="bg-emerald-600 hover:bg-emerald-500 text-black">Receive</Button>

          <div className="text-xs uppercase tracking-wider text-gray-500 pt-2">Assign / install</div>
          <div className="grid grid-cols-2 gap-2">
            <div><Label className="text-xs">Job ID</Label><Input data-testid="assign-job" value={jobId} onChange={(e) => setJobId(e.target.value)} className={inp} /></div>
            <div><Label className="text-xs">Customer ID</Label><Input data-testid="assign-customer" value={customerId} onChange={(e) => setCustomerId(e.target.value)} className={inp} /></div>
          </div>
          {product.tracking_mode === "serial" ? (
            <div><Label className="text-xs">Serials to install</Label><Input data-testid="assign-serials" value={assignSerials} onChange={(e) => setAssignSerials(e.target.value)} className={`${inp} font-mono`} placeholder="e.g. ABC-001 ABC-002" /></div>
          ) : (
            <div><Label className="text-xs">Quantity (draws earliest-expiry lot first)</Label><Input data-testid="assign-qty" value={assignQty} onChange={(e) => setAssignQty(e.target.value)} className={`${inp} font-mono`} /></div>
          )}
          <div className="flex gap-2">
            <Button data-testid="assign-save" onClick={doAssign} className="bg-amber-600 hover:bg-amber-500 text-black">Assign</Button>
            <Button data-testid="site-report" variant="ghost" onClick={doSite} className="text-sky-400 hover:bg-sky-500/10"><MapPin size={14} className="mr-1" /> Parts at this site</Button>
          </div>
          {site && (
            <div className="rounded-lg border border-[#222838] p-3 text-sm" data-testid="site-result">
              <div className="text-gray-300 mb-2">{site.total_units} tracked part(s) installed here</div>
              {site.serial_units.map((u) => (
                <div key={u.id} className="flex items-center justify-between py-1 border-b border-[#171c26] last:border-0">
                  <span className="font-mono text-gray-200">{u.serial}</span>
                  <span className="text-gray-400 text-xs">{u.product_name} • installed {u.assigned_date}</span>
                  <WarrantyChip status={u.warranty_status} />
                </div>
              ))}
              {site.lot_draws.map((d, i) => (
                <div key={i} className="flex items-center justify-between py-1 border-b border-[#171c26] last:border-0">
                  <span className="font-mono text-gray-200">Lot {d.lot_number} × {d.quantity}</span>
                  <span className="text-gray-400 text-xs">{d.product_name} • {d.install_date}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="space-y-3">
          <div className="text-xs uppercase tracking-wider text-gray-500 flex items-center gap-1"><Search size={13} /> Find a unit — scan or type</div>
          <StockScanInput testid="scan-code" value={scanCode} onChange={setScanCode} />
          <Button data-testid="scan-lookup" onClick={doLookup} className="bg-sky-600 hover:bg-sky-500 text-black">Look up</Button>
          {lookup?.kind === "serial" && (
            <div className="rounded-lg border border-[#222838] p-3 text-sm" data-testid="unit-story">
              <div className="flex items-center justify-between">
                <span className="font-mono text-gray-100">{lookup.unit.serial}</span>
                <WarrantyChip status={lookup.unit.warranty_status} />
              </div>
              <div className="text-gray-400 text-xs mt-1">
                {lookup.unit.product_name} • {lookup.unit.status}
                {lookup.unit.job_id ? ` • job ${lookup.unit.job_id}` : ""}{lookup.unit.customer_id ? ` • customer ${lookup.unit.customer_id}` : ""}
                {lookup.unit.warranty_start ? ` • warranty ${lookup.unit.warranty_start} → ${lookup.unit.warranty_until || "—"}` : ""}
                {lookup.unit.recalled ? " • UNDER RECALL" : ""}
              </div>
              <div className="mt-2 space-y-1">
                {lookup.story.map((m) => (
                  <div key={m.id} className="text-xs text-gray-400">
                    <span className="text-gray-200 capitalize">{m.type}</span> — {(m.created_at || "").slice(0, 10)}
                    {m.job_id ? ` → job ${m.job_id}` : ""}{m.customer_id ? ` → customer ${m.customer_id}` : ""}
                    {m.source_ref ? ` • ref ${String(m.source_ref).slice(0, 8)}` : ""}{m.note ? ` • ${m.note}` : ""}
                  </div>
                ))}
              </div>
            </div>
          )}
          {lookup?.kind === "lot" && (
            <div className="rounded-lg border border-[#222838] p-3 text-sm" data-testid="lot-lookup">
              {lookup.lots.map((l) => <div key={l.id} className="text-gray-300 text-xs py-0.5">Lot {l.lot_number} — {l.product_name}: {l.quantity_on_hand} on hand{l.expiry_date ? `, expires ${l.expiry_date}` : ""}</div>)}
            </div>
          )}

          <div className="text-xs uppercase tracking-wider text-gray-500 pt-2 flex items-center gap-1"><AlarmClock size={13} /> Use first — expiring within 30 days</div>
          <div data-testid="expiring-list">
            {expiring.length === 0 && <p className="text-xs text-gray-500">Nothing expiring soon.</p>}
            {expiring.map((l) => (
              <div key={l.id} className="flex justify-between text-xs py-1 border-b border-[#171c26]">
                <span className="font-mono text-gray-200">Lot {l.lot_number}</span>
                <span className="text-gray-400">{l.product_name} • {l.quantity_on_hand} left</span>
                <span className={l.expired ? "text-rose-300" : "text-amber-300"}>{l.expired ? `expired ${l.expiry_date}` : `${l.days_left}d left (${l.expiry_date})`}</span>
              </div>
            ))}
          </div>

          <div className="text-xs uppercase tracking-wider text-gray-500 pt-2 flex items-center gap-1"><FileWarning size={13} /> Recall — flag a serial range or a lot</div>
          <div className="grid grid-cols-3 gap-2">
            <div><Label className="text-xs">Serial from</Label><Input data-testid="recall-from" value={recall.from} onChange={(e) => setRecall({ ...recall, from: e.target.value })} className={`${inp} font-mono`} /></div>
            <div><Label className="text-xs">Serial to</Label><Input data-testid="recall-to" value={recall.to} onChange={(e) => setRecall({ ...recall, to: e.target.value })} className={`${inp} font-mono`} /></div>
            <div><Label className="text-xs">…or lot number</Label><Input data-testid="recall-lot" value={recall.lot} onChange={(e) => setRecall({ ...recall, lot: e.target.value })} className={`${inp} font-mono`} /></div>
          </div>
          <div><Label className="text-xs">Reason</Label><Input data-testid="recall-reason" value={recall.reason} onChange={(e) => setRecall({ ...recall, reason: e.target.value })} className={inp} placeholder="Manufacturer recall: …" /></div>
          <Button data-testid="recall-save" onClick={doRecall} className="bg-rose-600 hover:bg-rose-500 text-black"><ShieldCheck size={14} className="mr-1" /> Flag recall</Button>
          {recallResult && (
            <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 p-3 text-sm" data-testid="recall-result">
              <div className="text-rose-200 text-xs font-semibold mb-1">{recallResult.affected_count} item(s) flagged — who holds them:</div>
              {recallResult.affected_units.map((u) => (
                <div key={u.id} className="flex justify-between text-xs py-0.5">
                  <span className="font-mono text-gray-200">{u.serial}</span>
                  <span className="text-gray-400">{u.status}{u.job_id ? ` • job ${u.job_id}` : ""}{u.customer_id ? ` • customer ${u.customer_id}` : ""}</span>
                </div>
              ))}
              {recallResult.affected_lots.map((l) => (
                <div key={l.id} className="text-xs py-0.5 text-gray-300">Lot {l.lot_number} — {l.quantity_on_hand} on hand</div>
              ))}
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}
