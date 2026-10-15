import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { fmtDate, today } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { toast } from "sonner";
import { Play, Square, Plus, Clock } from "lucide-react";

const STATUS_CLS = {
  open: "bg-sky-500/10 text-sky-400 border-sky-500/20",
  logged: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  approved: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  closed: "bg-gray-500/10 text-gray-400 border-gray-500/20",
};

function elapsed(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 0) return "0h 00m";
  const h = Math.floor(ms / 3600000), m = Math.floor((ms % 3600000) / 60000);
  return `${h}h ${String(m).padStart(2, "0")}m`;
}

const IN = "bg-[#0F1218] border-[#222838]";

export default function Time() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [entries, setEntries] = useState(null);
  const [sheet, setSheet] = useState(null);
  const [period, setPeriod] = useState(today().slice(0, 7));
  const [team, setTeam] = useState([]);
  const [tick, setTick] = useState(0);
  const [clockNote, setClockNote] = useState("");
  const [m, setM] = useState({ date: today(), hours: "", client_name: "", notes: "", billable: true });

  const load = () => {
    api.get("/time/entries").then(setEntries).catch(() => setEntries([]));
    api.get(`/time/timesheet?period=${period}`).then(setSheet).catch(() => setSheet(null));
  };
  const loadTeam = (p) => {
    if (!isOwner) return;
    const [y, mo] = p.split("-");
    const last = new Date(Number(y), Number(mo), 0).getDate();
    api.get(`/time/entries?date_from=${p}-01&date_to=${p}-${last}`)
      .then(setTeam).catch(() => setTeam([]));
  };
  useEffect(() => { load(); }, []);
  useEffect(() => {
    api.get(`/time/timesheet?period=${period}`).then(setSheet).catch(() => setSheet(null));
    loadTeam(period);
  }, [period]); // eslint-disable-line react-hooks/exhaustive-deps

  const open = entries?.find((e) => e.status === "open");
  useEffect(() => {
    if (!open) return;
    const t = setInterval(() => setTick((x) => x + 1), 30000);
    return () => clearInterval(t);
  }, [open?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const clockIn = async () => {
    try { await api.post("/time/clock-in", { notes: clockNote }); setClockNote(""); toast.success("Clocked in"); load(); }
    catch (e) { toast.error(errMsg(e)); }
  };
  const clockOut = async () => {
    try { const r = await api.post("/time/clock-out", {}); toast.success(`Clocked out — ${r.hours}h logged`); load(); loadTeam(period); }
    catch (e) { toast.error(errMsg(e)); }
  };
  const logManual = async () => {
    try {
      await api.post("/time/entries", { ...m, hours: parseFloat(m.hours) });
      toast.success("Time logged");
      setM({ date: today(), hours: "", client_name: "", notes: "", billable: true });
      load(); loadTeam(period);
    } catch (e) { toast.error(errMsg(e)); }
  };
  const approve = async (id) => {
    try { await api.post(`/time/entries/${id}/approve`, {}); toast.success("Approved"); load(); loadTeam(period); }
    catch (e) { toast.error(errMsg(e)); }
  };
  const closeEntry = async (id) => {
    try { await api.post(`/time/entries/${id}/close`, {}); toast.success("Entry closed"); load(); loadTeam(period); }
    catch (e) { toast.error(errMsg(e)); }
  };

  if (!entries) return <Loading />;

  const byUser = {};
  team.forEach((e) => {
    const k = e.user_name || e.user_id;
    (byUser[k] = byUser[k] || { name: k, hours: 0, entries: [] });
    byUser[k].hours += e.hours || 0;
    byUser[k].entries.push(e);
  });

  const entryTable = (rows, showWho, actions) => (
    <Card className="p-0 overflow-hidden">
      <table className="w-full text-sm">
        <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
          <th className="px-5 py-3 text-left font-medium">Date</th>
          {showWho && <th className="px-5 py-3 text-left font-medium">Who</th>}
          <th className="px-5 py-3 text-left font-medium">Client</th>
          <th className="px-5 py-3 text-left font-medium">In → Out</th>
          <th className="px-5 py-3 text-right font-medium">Hours</th>
          <th className="px-5 py-3 text-left font-medium">Notes</th>
          <th className="px-5 py-3 text-left font-medium">Status</th>
          {actions && <th className="px-5 py-3 text-right font-medium">Actions</th>}
        </tr></thead>
        <tbody>
          {rows.map((e) => (
            <tr key={e.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
              <td className="px-5 py-3 text-gray-300 whitespace-nowrap">{fmtDate(e.date)}</td>
              {showWho && <td className="px-5 py-3 text-gray-100">{e.user_name}</td>}
              <td className="px-5 py-3 text-gray-400">{e.client_name || "—"}</td>
              <td className="px-5 py-3 text-gray-400 text-xs">
                {e.clock_in ? `${new Date(e.clock_in).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} → ${e.clock_out ? new Date(e.clock_out).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "…"}` : "manual"}
              </td>
              <td className="px-5 py-3 text-right font-mono text-gray-200">
                {e.status === "open" ? elapsed(e.clock_in) : `${e.hours}h`}
              </td>
              <td className="px-5 py-3 text-gray-500 text-xs max-w-[200px] truncate">{e.notes || "—"}</td>
              <td className="px-5 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[e.status] || ""}`}>{e.status}</span></td>
              {actions && (
                <td className="px-5 py-3 text-right">
                  <div className="flex gap-1 justify-end">
                    {e.status === "logged" && <Button size="sm" className="h-7 bg-emerald-600 hover:bg-emerald-500 text-black" onClick={() => approve(e.id)}>Approve</Button>}
                    {e.status === "approved" && <Button size="sm" variant="outline" className="h-7" onClick={() => closeEntry(e.id)}>Close</Button>}
                  </div>
                </td>
              )}
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={8} className="px-5 py-12 text-center text-gray-500">No entries</td></tr>}
        </tbody>
      </table>
    </Card>
  );

  return (
    <div className="animate-rise">
      <PageHeader title="Time Tracking" subtitle="Clock in/out, log hours, and review timesheets" testid="time-page" />

      <div className="grid gap-4 md:grid-cols-3 mb-6">
        <Card data-testid="time-clock-card">
          <div className="text-xs uppercase tracking-wider text-gray-500 mb-3">Time clock</div>
          {open ? (
            <div className="space-y-3">
              <div className="text-3xl font-mono font-semibold text-sky-400 tabular-nums" data-testid="time-elapsed">
                {elapsed(open.clock_in)}<span className="hidden">{tick}</span>
              </div>
              <div className="text-xs text-gray-500">Clocked in{open.client_name ? ` · ${open.client_name}` : ""}</div>
              <Button onClick={clockOut} className="bg-rose-600 hover:bg-rose-500 text-black font-semibold" data-testid="time-clock-out">
                <Square size={16} className="mr-1" /> Clock out
              </Button>
            </div>
          ) : (
            <div className="space-y-3">
              <p className="text-sm text-gray-500">You're clocked out.</p>
              <div><Label className="text-xs">Note (optional)</Label>
                <Input value={clockNote} onChange={(e) => setClockNote(e.target.value)} className={IN} placeholder="Shift notes…" /></div>
              <Button onClick={clockIn} className="bg-emerald-600 hover:bg-emerald-500 text-black font-semibold" data-testid="time-clock-in">
                <Play size={16} className="mr-1" /> Clock in
              </Button>
            </div>
          )}
        </Card>
        <Card>
          <div className="text-xs uppercase tracking-wider text-gray-500 mb-3">My timesheet — {period}</div>
          <div className="text-3xl font-mono font-semibold text-gray-100 tabular-nums">{sheet?.total_hours ?? "—"}h</div>
          <div className="text-xs text-gray-500 mt-1">
            {sheet ? Object.entries(sheet.hours_by_status).map(([s, h]) => `${s}: ${h}h`).join(" · ") : "—"}
          </div>
          <div className="mt-3"><Label className="text-xs">Period</Label>
            <Input type="month" value={period} onChange={(e) => setPeriod(e.target.value)} className={`${IN} w-44`} /></div>
        </Card>
        <Card data-testid="time-manual">
          <div className="text-xs uppercase tracking-wider text-gray-500 mb-3">Log time manually</div>
          <div className="space-y-2">
            <div className="flex gap-2">
              <div className="flex-1"><Label className="text-xs">Date</Label><Input type="date" value={m.date} onChange={(e) => setM({ ...m, date: e.target.value })} className={IN} /></div>
              <div className="w-24"><Label className="text-xs">Hours</Label><Input type="number" step="0.25" min="0.25" max="24" value={m.hours} onChange={(e) => setM({ ...m, hours: e.target.value })} className={`${IN} font-mono`} /></div>
            </div>
            <div><Label className="text-xs">Client (optional)</Label><Input value={m.client_name} onChange={(e) => setM({ ...m, client_name: e.target.value })} className={IN} /></div>
            <div><Label className="text-xs">Notes</Label><Input value={m.notes} onChange={(e) => setM({ ...m, notes: e.target.value })} className={IN} placeholder="What did you work on?" /></div>
            <Button size="sm" onClick={logManual} disabled={!m.hours || !m.date} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold">
              <Plus size={14} className="mr-1" /> Log time
            </Button>
          </div>
        </Card>
      </div>

      <Tabs defaultValue={isOwner ? "team" : "mine"}>
        <TabsList className="bg-[#14171E] border border-[#222838]">
          {isOwner && <TabsTrigger value="team" data-testid="tab-team-timesheet">Team Timesheet</TabsTrigger>}
          <TabsTrigger value="mine" data-testid="tab-my-entries">My Entries</TabsTrigger>
        </TabsList>
        {isOwner && (
          <TabsContent value="team" className="mt-4 space-y-6">
            {Object.values(byUser).map((u) => (
              <div key={u.name}>
                <div className="flex items-baseline gap-3 mb-2">
                  <h3 className="text-lg font-semibold text-gray-100">{u.name}</h3>
                  <span className="text-sm text-gray-500 font-mono">{u.hours.toFixed(2)}h · {period}</span>
                </div>
                {entryTable(u.entries, false, true)}
              </div>
            ))}
            {Object.keys(byUser).length === 0 && <Card><p className="text-sm text-gray-500 text-center py-8">No time entries in {period}.</p></Card>}
          </TabsContent>
        )}
        <TabsContent value="mine" className="mt-4">
          {entryTable(entries, false, false)}
        </TabsContent>
      </Tabs>
      <p className="flex items-center gap-1 text-xs text-gray-600 mt-4"><Clock size={12} /> Approved entries can be closed to lock them; clocking is audited.</p>
    </div>
  );
}
