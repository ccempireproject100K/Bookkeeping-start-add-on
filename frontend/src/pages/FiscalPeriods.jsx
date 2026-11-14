import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { fmtDate, money } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { toast } from "sonner";
import { Lock, LockOpen, Clock, Camera, History, Eye } from "lucide-react";
import { useTranslation } from "react-i18next";

const STATUS = {
  open: { label: "Open", cls: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20", icon: LockOpen },
  soft_closed: { label: "Soft Closed", cls: "bg-amber-500/10 text-amber-400 border-amber-500/20", icon: Clock },
  locked: { label: "Locked", cls: "bg-rose-500/10 text-rose-400 border-rose-500/20", icon: Lock },
  reopened: { label: "Reopened", cls: "bg-violet-500/10 text-violet-400 border-violet-500/20", icon: LockOpen },
};

const fmtDT = (s) => (s ? s.slice(0, 16).replace("T", " ") : "—");

export default function FiscalPeriods() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const [periods, setPeriods] = useState(null);
  const [settings, setSettings] = useState(null);
  const [closeKey, setCloseKey] = useState(null);
  const [checklist, setChecklist] = useState(null);
  const [ack, setAck] = useState({});
  const [reopenKey, setReopenKey] = useState(null);
  const [reason, setReason] = useState("");
  const [windowHours, setWindowHours] = useState(24);
  const [snapKey, setSnapKey] = useState(null);
  const [snapshots, setSnapshots] = useState(null);
  const [cacPeriod, setCacPeriod] = useState("");
  const [cac, setCac] = useState(null);

  const isOwner = user?.role === "owner";
  const canReopen = user?.role === "owner" || user?.role === "accountant";
  const canAudit = canReopen;

  const load = () => api.get("/ledger/periods").then(setPeriods).catch(() => setPeriods([]));
  const loadSettings = () => api.get("/ledger/period-settings").then((s) => { setSettings(s); setWindowHours(s.reopen_window_hours); }).catch(() => {});
  useEffect(() => { load(); loadSettings(); }, []);

  const saveSettings = async (patch) => {
    try {
      const s = await api.put("/ledger/period-settings", patch);
      setSettings(s);
      toast.success("Period-close settings saved");
    } catch (e) { toast.error(errMsg(e)); loadSettings(); }
  };

  const openClose = async (key) => {
    setCloseKey(key); setChecklist(null); setAck({});
    try { setChecklist(await api.get(`/ledger/periods/${key}/checklist`)); }
    catch (e) { toast.error(errMsg(e)); setCloseKey(null); }
  };

  const blockingOpen = (checklist?.items || []).filter((i) => i.blocking && i.count > 0);
  const allAcked = blockingOpen.every((i) => ack[i.key]);

  const doClose = async () => {
    try {
      await api.post(`/ledger/periods/${closeKey}/close`, { acknowledge: Object.keys(ack).filter((k) => ack[k]) });
      toast.success(`Period ${closeKey} closed`);
      setCloseKey(null); load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const doLock = async (key) => {
    try { await api.post(`/ledger/periods/${key}/lock`, {}); toast.success(`Period ${key} locked`); load(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const doReopen = async () => {
    try {
      await api.post(`/ledger/periods/${reopenKey}/reopen`, {
        reason: settings?.reopen_apparatus ? reason : undefined,
        window_hours: settings?.reopen_apparatus ? Number(windowHours) || undefined : undefined,
      });
      toast.success(`Period ${reopenKey} reopened`);
      setReopenKey(null); setReason(""); load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const doEndReopen = async (key) => {
    try { await api.post(`/ledger/periods/${key}/reopen/end`, {}); toast.success(`Reopen of ${key} ended — period re-locked`); load(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const openSnapshots = async (key) => {
    setSnapKey(key); setSnapshots(null);
    try { setSnapshots(await api.get(`/ledger/periods/${key}/snapshots`)); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const loadCac = async (key) => {
    try { setCac(await api.get(`/audit/changes-after-close${key ? `?period=${key}` : ""}`)); }
    catch (e) { toast.error(errMsg(e)); }
  };
  useEffect(() => { if (canAudit) loadCac(""); }, []); // eslint-disable-line

  if (!periods) return <Loading />;

  const statusOf = (p) => (p.reopen?.status === "active" ? STATUS.reopened : STATUS[p.status] || STATUS.open);

  return (
    <div className="animate-rise">
      <PageHeader title={t("fiscalperiods.fiscalPeriods")} subtitle="Close months with a checklist and a frozen snapshot — locked books stay locked" testid="fiscal-periods-page" />

      {settings && (
        <Card className="p-5 mb-5">
          <div className="flex items-center gap-2 mb-1"><Camera size={15} className="text-amber-400" /><h3 className="font-display text-sm">Period-close settings</h3></div>
          <p className="text-xs text-gray-500 mb-4">Each enhancement is its own switch. They all come on; turn one off and only that piece goes away — closing a period is always a deliberate act.</p>
          <div className="grid md:grid-cols-3 gap-4 text-sm">
            <label className="flex gap-2.5 items-start cursor-pointer">
              <Checkbox data-testid="switch-snapshot" checked={settings.snapshot_on_close} disabled={!isOwner} onCheckedChange={(v) => saveSettings({ snapshot_on_close: !!v })} />
              <span><span className="text-gray-200">Close snapshot</span><br /><span className="text-xs text-gray-500">Freeze the trial balance, P&amp;L and balance sheet at close — provable forever, even after a reopen.</span></span>
            </label>
            <label className="flex gap-2.5 items-start cursor-pointer">
              <Checkbox data-testid="switch-soft-stage" checked={settings.soft_close_stage} disabled={!isOwner} onCheckedChange={(v) => saveSettings({ soft_close_stage: !!v })} />
              <span><span className="text-gray-200">Soft-close stage</span><br /><span className="text-xs text-gray-500">Close softly first (postings warn and need a reason), hard-lock after the grace days. Off = close goes straight to hard lock.</span></span>
            </label>
            <label className="flex gap-2.5 items-start cursor-pointer">
              <Checkbox data-testid="switch-reopen-apparatus" checked={settings.reopen_apparatus} disabled={!isOwner} onCheckedChange={(v) => saveSettings({ reopen_apparatus: !!v })} />
              <span><span className="text-gray-200">Reopen safeguards</span><br /><span className="text-xs text-gray-500">Reopening needs a written reason, runs on a timer, notifies owner + accountant, and every change lands in Changes After Close.</span></span>
            </label>
          </div>
          <div className="flex flex-wrap gap-5 mt-4 text-sm">
            <div className="flex items-center gap-2">
              <Label className="text-xs text-gray-400">Grace days before hard lock</Label>
              <Input data-testid="input-grace-days" type="number" min={0} max={30} className="w-20 bg-[#0F1218] border-[#222838] h-8"
                value={settings.grace_days} disabled={!isOwner}
                onChange={(e) => setSettings({ ...settings, grace_days: e.target.value })}
                onBlur={(e) => saveSettings({ grace_days: Number(e.target.value) })} />
            </div>
            <div className="flex items-center gap-2">
              <Label className="text-xs text-gray-400">Reopen window (hours)</Label>
              <Input data-testid="input-reopen-window" type="number" min={1} max={168} className="w-20 bg-[#0F1218] border-[#222838] h-8"
                value={settings.reopen_window_hours} disabled={!isOwner}
                onChange={(e) => setSettings({ ...settings, reopen_window_hours: e.target.value })}
                onBlur={(e) => saveSettings({ reopen_window_hours: Number(e.target.value) })} />
            </div>
          </div>
        </Card>
      )}

      <Card className="p-0 overflow-hidden">
        <table className="w-full text-sm" data-testid="table-periods">
          <thead>
            <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
              <th className="px-5 py-3 text-left font-medium">Period</th>
              <th className="px-5 py-3 text-left font-medium">Range</th>
              <th className="px-5 py-3 text-left font-medium">Status</th>
              <th className="px-5 py-3 text-end font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {periods.map((p) => {
              const s = statusOf(p);
              const reopened = p.reopen?.status === "active";
              return (
                <tr key={p.key} className="border-b border-[#171c26] hover:bg-[#161b25]" data-testid={`period-${p.key}`}>
                  <td className="px-5 py-3 font-mono text-amber-400">{p.key}</td>
                  <td className="px-5 py-3 text-gray-400">{fmtDate(p.start_date)} – {fmtDate(p.end_date)}</td>
                  <td className="px-5 py-3">
                    <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs border ${s.cls}`}>
                      <s.icon size={12} />{s.label}
                    </span>
                    {p.status === "soft_closed" && !reopened && p.hard_lock_at && (
                      <span className="block text-[11px] text-gray-500 mt-1">auto-locks {fmtDate(p.hard_lock_at)}</span>
                    )}
                    {reopened && (
                      <span className="block text-[11px] text-violet-300/70 mt-1">until {fmtDT(p.reopen.closes_at)} — {p.reopen.reason}</span>
                    )}
                    {p.status === "locked" && p.locked_by && (
                      <span className="block text-[11px] text-gray-500 mt-1">by {p.locked_by}</span>
                    )}
                  </td>
                  <td className="px-5 py-3 text-end space-x-1.5 whitespace-nowrap">
                    <Button size="sm" variant="ghost" className="h-7 text-gray-300 hover:bg-white/5" onClick={() => openSnapshots(p.key)} data-testid={`snapshots-${p.key}`}><Camera size={12} className="mr-1" />Snapshots</Button>
                    {isOwner && p.status === "open" && !reopened && (
                      <Button size="sm" variant="ghost" className="h-7 text-amber-400 hover:bg-amber-500/10" onClick={() => openClose(p.key)} data-testid={`close-${p.key}`}>Close…</Button>
                    )}
                    {isOwner && p.status === "soft_closed" && !reopened && (
                      <Button size="sm" variant="ghost" className="h-7 text-rose-400 hover:bg-rose-500/10" onClick={() => doLock(p.key)} data-testid={`lock-${p.key}`}>Lock now</Button>
                    )}
                    {canReopen && p.status !== "open" && !reopened && (
                      <Button size="sm" variant="ghost" className="h-7 text-violet-400 hover:bg-violet-500/10" onClick={() => { setReopenKey(p.key); setReason(""); }} data-testid={`reopen-${p.key}`}>Reopen…</Button>
                    )}
                    {canReopen && reopened && (
                      <Button size="sm" variant="ghost" className="h-7 text-emerald-400 hover:bg-emerald-500/10" onClick={() => doEndReopen(p.key)} data-testid={`end-reopen-${p.key}`}>End reopen</Button>
                    )}
                  </td>
                </tr>
              );
            })}
            {periods.length === 0 && <tr><td colSpan={4} className="px-5 py-12 text-center text-gray-500">No periods yet</td></tr>}
          </tbody>
        </table>
      </Card>

      {canAudit && (
        <Card className="p-5 mt-5">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2"><Eye size={15} className="text-violet-400" /><h3 className="font-display text-sm">Changes after close</h3></div>
            <div className="flex items-center gap-2">
              <Input data-testid="input-cac-period" placeholder="YYYY-MM (blank = all)" className="w-44 bg-[#0F1218] border-[#222838] h-8 text-xs"
                value={cacPeriod} onChange={(e) => setCacPeriod(e.target.value)} />
              <Button size="sm" variant="ghost" className="h-8 text-gray-300 hover:bg-white/5" onClick={() => loadCac(cacPeriod)} data-testid="btn-cac-load">Show</Button>
            </div>
          </div>
          <p className="text-xs text-gray-500 mb-3">Every entry created or posted while a closed period was reopened — with the reason, the person, and the window. Nothing that happens in a reopen can hide.</p>
          <div data-testid="changes-after-close">
            {!cac && <div className="text-sm text-gray-500">Loading…</div>}
            {cac && cac.reopen_count === 0 && <div className="text-sm text-gray-500">No reopen windows on record.</div>}
            {cac && cac.periods.map((g) => (
              <div key={g.period_key} className="mb-4">
                <div className="font-mono text-amber-400 text-sm mb-2">{g.period_key}</div>
                {g.reopens.map((r) => (
                  <div key={r.reopen.id} className="border border-[#222838] rounded-lg p-3 mb-2 bg-[#0F1218]">
                    <div className="text-sm text-gray-200">&ldquo;{r.reopen.reason}&rdquo;</div>
                    <div className="text-xs text-gray-500">{r.reopen.by_name} ({r.reopen.by_role}) • {fmtDT(r.reopen.opened_at)} → {fmtDT(r.reopen.closes_at)} • {r.reopen.status}{r.reopen.end_reason ? ` (${r.reopen.end_reason})` : ""}</div>
                    {r.entries.length === 0 && <div className="text-xs text-gray-600 mt-2">No entries were made in this window.</div>}
                    {r.entries.length > 0 && (
                      <table className="w-full text-xs mt-2">
                        <tbody>
                          {r.entries.map((e) => (
                            <tr key={e.id} className="border-t border-[#171c26]">
                              <td className="py-1.5 pr-3 font-mono text-gray-300">{e.number || "Draft"}</td>
                              <td className="py-1.5 pr-3 text-gray-400">{fmtDate(e.entry_date)}</td>
                              <td className="py-1.5 pr-3 text-gray-400">{e.memo || "—"}</td>
                              <td className="py-1.5 pr-3 text-end font-mono text-gray-300">{money(e.total_cents)}</td>
                              <td className="py-1.5 text-gray-500">{e.posted_by || e.created_by}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* Close dialog: checklist first, exceptions acknowledged by name */}
      <Dialog open={!!closeKey} onOpenChange={(o) => !o && setCloseKey(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-xl" data-testid="close-dialog">
          <DialogHeader><DialogTitle className="font-display">Close period <span className="font-mono text-amber-400">{closeKey}</span></DialogTitle></DialogHeader>
          {!checklist && <div className="text-sm text-gray-500 py-4">Running the close checklist…</div>}
          {checklist && (
            <div className="space-y-2.5 text-sm">
              {checklist.items.map((i) => (
                <div key={i.key} className="flex items-start justify-between gap-3 border border-[#222838] rounded-lg px-3 py-2 bg-[#0F1218]">
                  <span className="text-gray-300">{i.label}
                    {i.amount_cents != null && i.count > 0 && <span className="text-gray-500"> — {money(i.amount_cents)} open</span>}
                  </span>
                  <span className="flex items-center gap-2 shrink-0">
                    <span className={`font-mono ${i.count > 0 && i.blocking ? "text-amber-400" : "text-gray-400"}`}>{i.count}</span>
                    {i.blocking && i.count > 0 && (
                      <label className="flex items-center gap-1.5 text-xs text-gray-400 cursor-pointer">
                        <Checkbox data-testid={`ack-${i.key}`} checked={!!ack[i.key]} onCheckedChange={(v) => setAck({ ...ack, [i.key]: !!v })} /> close anyway
                      </label>
                    )}
                  </span>
                </div>
              ))}
              {checklist.notes.map((n, ix) => <p key={ix} className="text-xs text-gray-500">{n}</p>)}
              <p className="text-xs text-gray-400 border-t border-[#222838] pt-2.5">
                {settings?.snapshot_on_close
                  ? "A snapshot of the books at close will be frozen."
                  : "Snapshot switch is OFF — no snapshot will be taken."}{" "}
                {settings?.soft_close_stage && settings?.grace_days > 0
                  ? `The period soft-closes first and hard-locks after ${settings.grace_days} grace days.`
                  : "The period goes straight to a hard lock."}
              </p>
            </div>
          )}
          <DialogFooter className="gap-2">
            <Button variant="outline" className="border-[#2a3140]" onClick={() => setCloseKey(null)}>Cancel</Button>
            <Button data-testid="btn-do-close" disabled={!checklist || !allAcked} onClick={doClose} className="bg-amber-500 hover:bg-amber-400 text-black disabled:opacity-40">
              {blockingOpen.length > 0 && !allAcked ? "Acknowledge the items above to close" : "Close period"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Reopen dialog */}
      <Dialog open={!!reopenKey} onOpenChange={(o) => !o && setReopenKey(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-md" data-testid="reopen-dialog">
          <DialogHeader><DialogTitle className="font-display">Reopen period <span className="font-mono text-amber-400">{reopenKey}</span></DialogTitle></DialogHeader>
          {settings?.reopen_apparatus ? (
            <div className="space-y-3">
              <div>
                <Label className="text-xs">Reason (required — it goes on the permanent record)</Label>
                <Input data-testid="input-reopen-reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Client sent a missing January invoice" className="bg-[#0F1218] border-[#222838]" />
              </div>
              <div>
                <Label className="text-xs">Window (hours, then it re-locks itself)</Label>
                <Input data-testid="input-reopen-hours" type="number" min={1} max={168} value={windowHours} onChange={(e) => setWindowHours(e.target.value)} className="bg-[#0F1218] border-[#222838] w-28" />
              </div>
              <p className="text-xs text-gray-500">The owner and accountant are notified. Every entry made in the window is tagged and shows under Changes After Close.</p>
            </div>
          ) : (
            <p className="text-sm text-gray-400">The reopen safeguards switch is off, so this is a plain reopen — no reason, no timer. The period simply becomes open again.</p>
          )}
          <DialogFooter className="gap-2">
            <Button variant="outline" className="border-[#2a3140]" onClick={() => setReopenKey(null)}>Cancel</Button>
            <Button data-testid="btn-do-reopen" disabled={settings?.reopen_apparatus && !reason.trim()} onClick={doReopen} className="bg-violet-500 hover:bg-violet-400 text-black disabled:opacity-40">Reopen period</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Snapshots dialog */}
      <Dialog open={!!snapKey} onOpenChange={(o) => !o && setSnapKey(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-xl" data-testid="snapshots-dialog">
          <DialogHeader><DialogTitle className="font-display flex items-center gap-2"><History size={16} className="text-amber-400" />Snapshots — <span className="font-mono text-amber-400">{snapKey}</span></DialogTitle></DialogHeader>
          {!snapshots && <div className="text-sm text-gray-500 py-4">Loading…</div>}
          {snapshots && snapshots.length === 0 && <div className="text-sm text-gray-500 py-2">No snapshots for this period. Either it was never closed, or the snapshot switch was off when it closed.</div>}
          {snapshots && snapshots.map((sn) => (
            <div key={sn.id} className="border border-[#222838] rounded-lg px-3 py-2.5 bg-[#0F1218] text-sm mb-2">
              <div className="flex justify-between">
                <span className="text-gray-200 capitalize">{sn.kind} snapshot #{sn.seq}</span>
                <span className="text-xs text-gray-500">{fmtDT(sn.taken_at)} • {sn.taken_by}</span>
              </div>
              <div className="text-xs text-gray-400 mt-1 font-mono">
                Trial balance {money(sn.trial_balance.total_debit)} Dr / {money(sn.trial_balance.total_credit)} Cr • Net income {money(sn.pnl.net_income)} • Balance sheet {sn.balance_sheet.balanced ? "balanced" : "OUT OF BALANCE"}
              </div>
            </div>
          ))}
        </DialogContent>
      </Dialog>
    </div>
  );
}
