import { useEffect, useState } from "react";
import { api, errMsg, downloadCsv } from "@/lib/api";
import { money, fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { BellRing, Download, Landmark } from "lucide-react";

const NEXT = { "": "prepared", prepared: "reviewed", reviewed: "approved", approved: null };
const NEXT_LABEL = { prepared: "Prepare", reviewed: "Review", approved: "Approve" };
const STATE_CLS = {
  prepared: "bg-sky-500/10 text-sky-400 border-sky-500/20",
  reviewed: "bg-violet-500/10 text-violet-400 border-violet-500/20",
  approved: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
};
const BUCKETS = [
  { key: "due_now", label: "Due now" },
  { key: "due_this_week", label: "Due this week" },
  { key: "upcoming", label: "Upcoming" },
];

function Trail({ approval }) {
  const parts = [];
  if (approval?.prepared) parts.push(`Prepared by ${approval.prepared.by} · ${fmtDate(approval.prepared.at?.slice(0, 10))}`);
  if (approval?.reviewed) parts.push(`Reviewed by ${approval.reviewed.by} · ${fmtDate(approval.reviewed.at?.slice(0, 10))}`);
  if (approval?.approved) parts.push(`Approved by ${approval.approved.by} · ${fmtDate(approval.approved.at?.slice(0, 10))}`);
  if (!parts.length) return null;
  return <div className="text-xs text-gray-500 mt-1" data-testid="approval-trail">{parts.join("  →  ")}</div>;
}

export default function BillPay() {
  const { user } = useAuth();
  const canManage = user?.role === "owner";
  const [q, setQ] = useState(null);
  const [all, setAll] = useState(null);       // unfiltered, for filter option lists
  const [vendor, setVendor] = useState("");
  const [jobId, setJobId] = useState("");
  const [sel, setSel] = useState({});
  const [payBill, setPayBill] = useState(null);
  const [pay, setPay] = useState({ amount: "", payment_method: "bank", reference: "", pay_date: "" });
  const [sched, setSched] = useState({});

  const load = () => {
    const params = {};
    if (vendor) params.vendor = vendor;
    if (jobId) params.job_id = jobId;
    api.get("/billpay/queue", { params }).then(setQ).catch(() => setQ(null));
    api.get("/billpay/queue").then(setAll).catch(() => {});
  };
  useEffect(load, [vendor, jobId]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!q) return <Loading />;

  const selectedIds = Object.keys(sel).filter((k) => sel[k]);
  const act = async (fn, okMsg) => {
    try { await fn(); if (okMsg) toast.success(okMsg); load(); }
    catch (e) { toast.error(errMsg(e)); }
  };
  const transition = (id, target) => act(() => api.post(`/billpay/bills/${id}/transition`, { target }));
  const batch = (target) => act(async () => {
    const r = await api.post("/billpay/batch", { bill_ids: selectedIds, target });
    if (r.failed) toast.error(`${r.failed} bill(s) could not move to ${target}`);
    else toast.success(`${r.succeeded} bill(s) → ${target}`);
    setSel({});
  });
  const doSchedule = (id) => act(() => api.post(`/billpay/bills/${id}/schedule`, { pay_date: sched[id] }), "Pay date set");
  const doMarkPaid = async () => {
    try {
      await api.post(`/billpay/bills/${payBill.id}/mark-paid`, {
        payment_method: pay.payment_method, reference: pay.reference || null,
        pay_date: pay.pay_date || null, amount: pay.amount ? parseFloat(pay.amount) : null,
      });
      toast.success("Payment recorded"); setPayBill(null); load();
    } catch (e) { toast.error(errMsg(e)); }
  };
  const packet = (format) => downloadCsv(
    `/billpay/packet?format=${format}${vendor ? `&vendor=${encodeURIComponent(vendor)}` : ""}${jobId ? `&job_id=${encodeURIComponent(jobId)}` : ""}`,
    `ready-to-pay.${format}`).catch((e) => toast.error(errMsg(e)));

  const vendorOptions = all?.by_vendor?.map((v) => v.vendor) || [];
  const jobOptions = all?.by_job || [];

  return (
    <div className="animate-rise">
      <PageHeader title="Pay Queue" subtitle="Bill-pay prep — approve, schedule, export, record. Books does not send money."
        testid="billpay-page">
        {canManage && <Button variant="outline" data-testid="btn-run-reminders"
          onClick={() => act(async () => { const r = await api.post("/billpay/reminders/run"); toast.success(r.created ? `${r.created} reminder(s)` : "No new reminders"); })}>
          <BellRing size={16} className="mr-1" />Run reminders</Button>}
        <Button variant="outline" data-testid="btn-packet-csv" onClick={() => packet("csv")}>
          <Download size={16} className="mr-1" />Packet CSV</Button>
        <Button variant="outline" data-testid="btn-packet-pdf" onClick={() => packet("pdf")}>
          <Download size={16} className="mr-1" />Packet PDF</Button>
      </PageHeader>

      <Card className="p-4 mb-4 flex gap-3 items-start border-amber-500/20 bg-amber-500/5" data-testid="rail-note">
        <Landmark className="text-amber-400 mt-0.5" size={18} />
        <div className="text-sm text-gray-300">
          <span className="font-medium text-amber-300">No payments partner connected.</span>{" "}
          {q.note} Approved bills go into the ready-to-pay packet — pay them from your bank,
          then mark each one paid here so the books record it.
          <div className="text-xs text-gray-500 mt-1">
            Cash position (recorded in the books, not a live bank feed):{" "}
            <span className="text-gray-300 font-medium">{money(q.cash_position.total_cents)}</span>
            {q.cash_position.accounts.map((a) => (
              <span key={a.gl_code}> · {a.name}: {money(a.recorded_balance_cents)}</span>
            ))}
          </div>
        </div>
      </Card>

      <div className="flex flex-wrap gap-3 mb-4 items-end">
        <div>
          <Label className="text-xs text-gray-500">Vendor</Label>
          <Select value={vendor || "all"} onValueChange={(v) => setVendor(v === "all" ? "" : v)}>
            <SelectTrigger className="w-52" data-testid="filter-vendor"><SelectValue placeholder="All vendors" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All vendors</SelectItem>
              {vendorOptions.map((v) => <SelectItem key={v} value={v}>{v}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label className="text-xs text-gray-500">Job</Label>
          <Select value={jobId || "all"} onValueChange={(v) => setJobId(v === "all" ? "" : v)}>
            <SelectTrigger className="w-52" data-testid="filter-job"><SelectValue placeholder="All jobs" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All jobs</SelectItem>
              {jobOptions.map((j) => <SelectItem key={j.job_label} value={j.job_id || "__none"}>{j.job_label}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
        {canManage && selectedIds.length > 0 && (
          <div className="flex gap-2 items-center" data-testid="batch-bar">
            <span className="text-sm text-gray-400">{selectedIds.length} selected</span>
            {["prepared", "reviewed", "approved"].map((t) => (
              <Button key={t} size="sm" variant="outline" onClick={() => batch(t)}>{NEXT_LABEL[t]} batch</Button>
            ))}
          </div>
        )}
        <div className="ml-auto text-sm text-gray-400">
          Total open: <span className="text-gray-100 font-medium">{money(q.total.balance_cents)}</span> ({q.total.count} bills)
        </div>
      </div>

      {BUCKETS.map(({ key, label }) => {
        const bucket = q.buckets[key];
        if (!bucket || bucket.count === 0) return null;
        return (
          <div key={key} className="mb-5">
            <div className="text-xs uppercase tracking-wider text-gray-500 mb-2">
              {label} — {bucket.count} bill(s) · {money(bucket.balance_cents)}
            </div>
            <Card className="p-0 overflow-hidden">
              <table className="w-full text-sm" data-testid={`bucket-${key}`}>
                <tbody>
                  {bucket.bills.map((b) => {
                    const next = NEXT[b.prep_state || ""];
                    return (
                      <tr key={b.id} className="border-b border-[#222838] last:border-0 align-top">
                        <td className="px-4 py-3 w-8">{canManage && (
                          <Checkbox checked={!!sel[b.id]} onCheckedChange={(v) => setSel({ ...sel, [b.id]: !!v })} />
                        )}</td>
                        <td className="px-2 py-3">
                          <div className="font-medium text-gray-100">{b.vendor}
                            {b.bill_ref && <span className="text-gray-500 font-normal"> · {b.bill_ref}</span>}
                          </div>
                          <Trail approval={b.approval} />
                          {b.discount && (
                            <div className="text-xs mt-1">
                              {b.discount.window_open
                                ? <span className="text-emerald-400">Save {money(b.discount.save_cents)} ({b.discount.percent}% early-pay) if paid by {fmtDate(b.discount.discount_date)} — pay {money(b.discount.pay_cents)}</span>
                                : <span className="text-gray-500">Early-pay window closed {fmtDate(b.discount.discount_date)}</span>}
                            </div>
                          )}
                        </td>
                        <td className="px-2 py-3 text-gray-400 whitespace-nowrap">Due {fmtDate(b.due_date)}</td>
                        <td className="px-2 py-3 text-right font-medium text-gray-100 whitespace-nowrap">{money(b.balance_cents)}</td>
                        <td className="px-2 py-3 whitespace-nowrap">
                          {b.prep_state
                            ? <span className={`text-xs px-2 py-1 rounded border ${STATE_CLS[b.prep_state]}`}>{b.prep_state}</span>
                            : <span className="text-xs text-gray-500">not prepared</span>}
                        </td>
                        <td className="px-4 py-3 whitespace-nowrap text-right">
                          {canManage && next && (
                            <Button size="sm" className="bg-amber-600 hover:bg-amber-500 text-black font-semibold mr-2"
                              data-testid={`btn-next-${b.id}`} onClick={() => transition(b.id, next)}>
                              {NEXT_LABEL[next]}
                            </Button>
                          )}
                          {canManage && b.prep_state === "approved" && (
                            <span className="inline-flex items-center gap-1 mr-2">
                              <Input type="date" className="w-36 h-8" value={sched[b.id] ?? b.pay_date_planned ?? ""}
                                onChange={(e) => setSched({ ...sched, [b.id]: e.target.value })} />
                              <Button size="sm" variant="outline" onClick={() => doSchedule(b.id)}>Set pay date</Button>
                            </span>
                          )}
                          {canManage && (
                            <Button size="sm" variant="outline" onClick={() => { setPayBill(b); setPay({ amount: "", payment_method: "bank", reference: "", pay_date: "" }); }}>
                              Mark paid
                            </Button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </Card>
          </div>
        );
      })}
      {q.total.count === 0 && <Card className="p-8 text-center text-gray-500">No open bills in the queue.</Card>}

      <Dialog open={!!payBill} onOpenChange={(o) => !o && setPayBill(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Record payment — {payBill?.vendor}</DialogTitle></DialogHeader>
          <div className="space-y-3 text-sm text-gray-400">
            <p>This records a payment you already made <b>outside</b> Books. Books does not send money.</p>
            <div><Label>Amount (blank = full balance {payBill ? money(payBill.balance_cents) : ""})</Label>
              <Input type="number" step="0.01" value={pay.amount} onChange={(e) => setPay({ ...pay, amount: e.target.value })} /></div>
            <div><Label>Method</Label>
              <Select value={pay.payment_method} onValueChange={(v) => setPay({ ...pay, payment_method: v })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="bank">Bank transfer</SelectItem>
                  <SelectItem value="card">Card</SelectItem>
                  <SelectItem value="cash">Cash / check</SelectItem>
                </SelectContent>
              </Select></div>
            <div><Label>Reference (check no. / transfer ref)</Label>
              <Input value={pay.reference} onChange={(e) => setPay({ ...pay, reference: e.target.value })} /></div>
            <div><Label>Pay date</Label>
              <Input type="date" value={pay.pay_date} onChange={(e) => setPay({ ...pay, pay_date: e.target.value })} /></div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPayBill(null)}>Cancel</Button>
            <Button className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" onClick={doMarkPaid}>Record payment</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
