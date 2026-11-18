import { useRef, useState } from "react";
import {
  CloudOff, CloudUpload, CheckCircle2, AlertTriangle, XCircle,
  RefreshCw, Trash2, ChevronRight, Camera, WifiOff,
} from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { useOfflineQueue } from "@/context/OfflineQueueContext";
import { today } from "@/lib/format";

const STATE_META = {
  waiting: { label: "Waiting to send", cls: "bg-amber-500/10 text-amber-400 border-amber-500/20", icon: CloudUpload },
  syncing: { label: "Sending…", cls: "bg-sky-500/10 text-sky-400 border-sky-500/20", icon: RefreshCw },
  attention: { label: "Needs attention", cls: "bg-rose-500/10 text-rose-400 border-rose-500/20", icon: AlertTriangle },
  failed: { label: "Didn't send", cls: "bg-rose-500/10 text-rose-400 border-rose-500/20", icon: XCircle },
};

function capturedWhen(iso) {
  const d = new Date(iso);
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

/**
 * The visible half of the offline queue. The chip lives in the app shell
 * at all times; the panel lists every queued item in plain words with
 * retry / discard, and carries the quick expense-note capture.
 */
export default function SyncStatus() {
  const q = useOfflineQueue();
  const [open, setOpen] = useState(false);
  const [exp, setExp] = useState({ amount: "", note: "", vendor: "" });
  const [photo, setPhoto] = useState(null);
  const photoRef = useRef(null);
  const { counts } = q;
  const pending = counts.waiting + counts.syncing;
  const problems = counts.attention + counts.failed;

  const chipCls = problems > 0
    ? "border-rose-500/40 text-rose-300"
    : pending > 0
      ? "border-amber-500/40 text-amber-300"
      : "border-[#222838] text-gray-400";

  const saveExpense = async () => {
    if (!exp.amount || !(parseFloat(exp.amount) > 0)) { toast.error("Enter an amount"); return; }
    await q.captureExpenseNote({
      amount: exp.amount,
      note: exp.note,
      vendor: exp.vendor,
      date: today(),
      photoBlob: photo || undefined,
      photoName: photo?.name || undefined,
    });
    setExp({ amount: "", note: "", vendor: "" });
    setPhoto(null);
    toast.success(q.online ? "Expense note sent to the queue" : "Saved on this phone — it will send when signal returns");
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        data-testid="sync-status-chip"
        className={`flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-medium bg-[#0F1218] hover:bg-[#161b25] ${chipCls}`}
        title="Field capture sync status"
      >
        {!q.online ? <WifiOff size={13} /> : pending > 0 ? <CloudUpload size={13} /> : <CheckCircle2 size={13} className="text-emerald-400" />}
        {!q.online
          ? `Offline${pending ? ` · ${pending} waiting` : ""}`
          : problems > 0
            ? `${problems} need${problems === 1 ? "s" : ""} attention`
            : pending > 0
              ? `${pending} waiting to send`
              : "Synced"}
        <ChevronRight size={12} className="opacity-60" />
      </button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-lg bg-[#0F1218] border-[#222838] text-gray-100">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {q.online ? <CloudUpload size={18} /> : <CloudOff size={18} />} Field capture — sync status
            </DialogTitle>
          </DialogHeader>

          <p className="text-xs text-gray-500 leading-relaxed">
            {q.online
              ? "You're connected. Anything listed here sends through the normal path; the counts below are the truth, not a promise."
              : "You're offline. Time entries, receipt photos and expense notes you capture are saved on THIS phone and listed here until they send. Sending an invoice or taking a payment still needs signal."}
          </p>

          <div className="flex gap-2 text-xs" data-testid="sync-counts">
            <span className="px-2 py-1 rounded-full border border-[#222838]">Waiting: <strong>{counts.waiting}</strong></span>
            <span className="px-2 py-1 rounded-full border border-[#222838]">Sending: <strong>{counts.syncing}</strong></span>
            <span className="px-2 py-1 rounded-full border border-[#222838]">Needs attention: <strong>{counts.attention + counts.failed}</strong></span>
            {q.online && counts.waiting > 0 && (
              <Button size="sm" variant="outline" className="h-6 ml-auto" onClick={() => q.syncNow()}>
                <RefreshCw size={12} className="mr-1" /> Send now
              </Button>
            )}
          </div>

          <div className="space-y-2 max-h-72 overflow-y-auto pr-1">
            {q.items.length === 0 && (
              <p className="text-sm text-gray-500 text-center py-6" data-testid="sync-empty">
                Nothing waiting. Everything you've captured is on your books.
              </p>
            )}
            {q.items.map((item) => {
              const meta = STATE_META[item.state] || STATE_META.waiting;
              const Icon = meta.icon;
              return (
                <div key={item.id} className="rounded-lg border border-[#222838] bg-[#12151d] px-3 py-2.5" data-testid={`sync-item-${item.id}`}>
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-gray-100 flex-1">{q.describeItem(item)}</span>
                    <span className={`px-2 py-0.5 rounded-full text-[11px] border ${meta.cls}`}>
                      <Icon size={11} className="inline mr-1" />{meta.label}
                    </span>
                  </div>
                  <div className="text-[11px] text-gray-500 mt-1">Captured {capturedWhen(item.created_at)} · saved on this phone until it sends</div>
                  {item.error && <p className="text-xs text-rose-300/90 mt-1.5 leading-relaxed">{item.error}</p>}
                  <div className="flex gap-1.5 mt-2">
                    {(item.state === "failed" || item.state === "attention" || (item.state === "waiting" && q.online)) && (
                      <Button size="sm" variant="outline" className="h-7" onClick={() => q.retry(item.id)}>
                        <RefreshCw size={12} className="mr-1" /> Retry
                      </Button>
                    )}
                    <Button
                      size="sm" variant="ghost" className="h-7 text-rose-300 hover:text-rose-200"
                      onClick={() => { if (window.confirm("Discard this item? It will never be sent.")) q.discard(item.id); }}
                    >
                      <Trash2 size={12} className="mr-1" /> Discard
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>

          <div className="border-t border-[#222838] pt-3">
            <div className="text-xs uppercase tracking-wider text-gray-500 mb-2">Quick expense note</div>
            <div className="flex gap-2">
              <div className="w-28">
                <Label className="text-xs">Amount $</Label>
                <Input type="number" step="0.01" min="0" value={exp.amount} onChange={(e) => setExp({ ...exp, amount: e.target.value })} className="bg-[#0F1218] border-[#222838] font-mono" placeholder="0.00" />
              </div>
              <div className="flex-1">
                <Label className="text-xs">What for</Label>
                <Input value={exp.note} onChange={(e) => setExp({ ...exp, note: e.target.value })} className="bg-[#0F1218] border-[#222838]" placeholder="Duct tape, gas, fittings…" />
              </div>
              <div className="self-end">
                <input ref={photoRef} type="file" accept="image/*" capture="environment" className="hidden" onChange={(e) => setPhoto(e.target.files?.[0] || null)} />
                <Button size="sm" variant="outline" className="h-9" onClick={() => photoRef.current?.click()} title="Attach a photo of the receipt">
                  <Camera size={14} className={photo ? "text-emerald-400" : ""} />
                </Button>
              </div>
              <div className="self-end">
                <Button size="sm" className="h-9 bg-amber-600 hover:bg-amber-500 text-black font-semibold" onClick={saveExpense}>Save</Button>
              </div>
            </div>
            {photo && <p className="text-[11px] text-emerald-400 mt-1">Photo attached: {photo.name} — it lands in the Receipt Inbox when this sends.</p>}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
