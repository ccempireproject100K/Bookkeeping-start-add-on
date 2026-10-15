import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, today } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { toast } from "sonner";
import { Plus, Trash2, Printer, X, AlertTriangle } from "lucide-react";
import { useTranslation } from "react-i18next";
const STATUS_CLS = {
  draft: "bg-gray-500/10 text-gray-400 border-gray-500/20",
  approved: "bg-sky-500/10 text-sky-400 border-sky-500/20",
  posted: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
};
const centsToDollars = c => ((c || 0) / 100).toFixed(2);
function printPayslip(p) {
  const fmt = c => ((c || 0) / 100).toLocaleString("en-US", {
    style: "currency",
    currency: "USD"
  });
  const earnRows = (p.earnings || []).map(e => `<tr><td>${e.label}</td><td style="text-align:right">${fmt(e.amount_cents)}</td></tr>`).join("");
  const dedRows = (p.deductions || []).map(d => `<tr><td>${d.label}</td><td style="text-align:right">−${fmt(d.amount_cents)}</td></tr>`).join("");
  const w = window.open("", "_blank", "width=480,height=640");
  if (!w) {
    toast.error(t("payroll.allowPopupsToPrintTheStub"));
    return;
  }
  w.document.write(`<!DOCTYPE html><html><head><title>Pay stub — ${p.employee.name}</title>
    <style>body{font-family:system-ui,sans-serif;max-width:420px;margin:40px auto;padding:0 16px;color:#111}
    table{width:100%;border-collapse:collapse;font-size:14px}td{padding:6px 0;border-bottom:1px solid #eee}
    .total td{border-top:2px solid #111;font-weight:700;font-size:16px}.mono{font-family:ui-monospace,monospace}
    .disc{font-size:11px;color:#666;margin-top:16px}</style></head><body>
    <h2 style="text-align:center;margin-bottom:0">${p.company_name}</h2>
    <p style="text-align:center;margin-top:4px"><strong>Pay Stub</strong></p>
    <p style="text-align:center;color:#666;font-size:12px">${p.period_start} → ${p.period_end} · Pay date ${p.pay_date}</p>
    <h3>${p.employee.name}</h3>
    <p style="color:#666;font-size:12px">${p.employee.pay_type === "hourly" ? `Hourly · ${p.employee.hours}h × ${fmt(p.employee.rate_cents)}/hr` : "Salary"}</p>
    <table>${earnRows}
    <tr><td><strong>Gross pay</strong></td><td class="mono" style="text-align:right"><strong>${fmt(p.gross_cents)}</strong></td></tr>
    ${dedRows}
    <tr class="total"><td>Net pay</td><td class="mono" style="text-align:right">${fmt(p.net_cents)}</td></tr></table>
    ${p.entry_number ? `<p style="font-size:11px;color:#666">Journal entry ${p.entry_number}</p>` : ""}
    <p class="disc">${p.disclaimer}</p>
    <script>window.onload=()=>window.print()</script></body></html>`);
  w.document.close();
}
export default function Payroll() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const canWrite = user?.role === "owner";
  const [schedules, setSchedules] = useState(null);
  const [team, setTeam] = useState(null);
  const [settings, setSettings] = useState(null);
  const [runs, setRuns] = useState(null);
  const [detail, setDetail] = useState(null);
  const [showNew, setShowNew] = useState(false);
  const load = () => {
    api.get("/payroll/schedules").then(setSchedules).catch(() => setSchedules([]));
    api.get("/payroll/team").then(setTeam).catch(() => setTeam([]));
    api.get("/payroll/settings").then(setSettings).catch(() => {});
    api.get("/payroll/runs").then(setRuns).catch(() => setRuns([]));
  };
  useEffect(() => {
    load();
  }, []);

  // ---- new run form ----
  const [schedId, setSchedId] = useState("");
  const [period, setPeriod] = useState({
    start: today(),
    end: today()
  });
  const [rows, setRows] = useState({});
  useEffect(() => {
    if (!team) return;
    const init = {};
    team.forEach(m => {
      const hasSettings = (m.hourly_rate_cents || 0) > 0 || (m.salary_monthly_cents || 0) > 0;
      init[m.id] = {
        include: hasSettings,
        hours: "",
        rate: centsToDollars(m.hourly_rate_cents),
        commission: ""
      };
    });
    setRows(init);
  }, [team]);
  const setRow = (id, patch) => setRows(rs => ({
    ...rs,
    [id]: {
      ...rs[id],
      ...patch
    }
  }));
  const createRun = async () => {
    const member = Object.fromEntries((team || []).map(m => [m.id, m]));
    const items = Object.entries(rows).filter(([, r]) => r.include).map(([id, r]) => {
      const m = member[id];
      return {
        user_id: id,
        name: m.name,
        pay_type: m.salary_monthly_cents > 0 ? "salary" : "hourly",
        hours: parseFloat(r.hours) || 0,
        rate: r.rate ? parseFloat(r.rate) : undefined,
        commission: parseFloat(r.commission) || 0
      };
    });
    if (!items.length) {
      toast.error(t("payroll.selectAtLeastOneTeamMember"));
      return;
    }
    try {
      const run = await api.post("/payroll/runs", {
        schedule_id: schedId || null,
        period_start: period.start,
        period_end: period.end,
        items
      });
      toast.success(`Pay run created — net ${money(run.totals.net_cents)}`);
      setShowNew(false);
      load();
      setDetail(run);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const doApprove = async id => {
    try {
      const r = await api.post(`/payroll/runs/${id}/approve`, {});
      toast.success(t("payroll.runApproved"));
      setDetail(r);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const doPost = async id => {
    try {
      const r = await api.post(`/payroll/runs/${id}/post`, {});
      toast.success(`Posted to journal${r.entry_number ? ` as ${r.entry_number}` : ""}`);
      setDetail(r);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const doDeleteRun = async id => {
    try {
      await api.del(`/payroll/runs/${id}`);
      toast.success(t("payroll.draftRunDeleted"));
      setDetail(null);
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const doPrint = async (runId, itemId) => {
    try {
      const p = await api.get(`/payroll/runs/${runId}/payslips/${itemId}`);
      printPayslip(p);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  // ---- schedules ----
  const [schedForm, setSchedForm] = useState({
    name: "",
    frequency: "biweekly",
    next_run: today()
  });
  const saveSched = async e => {
    e.preventDefault();
    try {
      await api.post("/payroll/schedules", schedForm);
      toast.success(t("payroll.scheduleSaved"));
      setSchedForm({
        name: "",
        frequency: "biweekly",
        next_run: today()
      });
      load();
    } catch (err) {
      toast.error(errMsg(err));
    }
  };
  const delSched = async id => {
    try {
      await api.del(`/payroll/schedules/${id}`);
      toast.success(t("payroll.scheduleDeleted"));
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  // ---- team pay settings ----
  const [edit, setEdit] = useState({});
  useEffect(() => {
    if (!team) return;
    const init = {};
    team.forEach(m => {
      init[m.id] = {
        hourly_rate: centsToDollars(m.hourly_rate_cents),
        salary_monthly: centsToDollars(m.salary_monthly_cents),
        fed_withholding_pct: String(m.fed_withholding_pct ?? ""),
        booth_rent: centsToDollars(m.booth_rent_cents)
      };
    });
    setEdit(init);
  }, [team]);
  const setEditRow = (id, patch) => setEdit(es => ({
    ...es,
    [id]: {
      ...es[id],
      ...patch
    }
  }));
  const saveSettings = async id => {
    const f = edit[id];
    try {
      await api.patch(`/payroll/team/${id}`, {
        hourly_rate: parseFloat(f.hourly_rate) || 0,
        salary_monthly: parseFloat(f.salary_monthly) || 0,
        fed_withholding_pct: parseFloat(f.fed_withholding_pct) || 0,
        booth_rent: parseFloat(f.booth_rent) || 0
      });
      toast.success(t("payroll.paySettingsSaved"));
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const saveDefaultFed = async () => {
    try {
      await api.patch("/payroll/settings", {
        default_fed_withholding_pct: parseFloat(settings.default_fed_withholding_pct) || 0
      });
      toast.success(t("payroll.defaultWithholdingSaved"));
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  if (!schedules || !team || !runs) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("payroll.payroll")} subtitle="Pay schedules, gross-to-net runs, printable stubs" testid="payroll-page">
        {canWrite && <Button data-testid="payroll-new-run" onClick={() => setShowNew(s => !s)} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold"><Plus size={16} className="mr-1" /> {t("payroll.newPayRun")}</Button>}
      </PageHeader>

      <div className="flex gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm mb-6" data-testid="payroll-disclaimer">
        <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-500" />
        <p className="text-gray-300"><strong>{t("payroll.payrollLiteScope")}</strong> {t("payroll.estimatesOnlyItDoes")} <strong>{t("payroll.not")}</strong> {t("payroll.withholdOrFileTaxesAndDoes")} <strong>{t("payroll.not")}</strong> {t("payroll.runDirectDepositForCompliantPayroll")}</p>
      </div>

      {showNew && canWrite && <Card className="mb-6" data-testid="payroll-new-form">
          <h3 className="text-sm font-semibold text-gray-200 mb-3">{t("payroll.newPayRun2")}</h3>
          <div className="flex flex-wrap items-end gap-3 mb-4">
            <div><Label className="text-xs">{t("payroll.schedule")}</Label>
              <Select value={schedId} onValueChange={setSchedId}>
                <SelectTrigger className="bg-[#0F1218] border-[#222838] w-48"><SelectValue placeholder={t("payroll.none")} /></SelectTrigger>
                <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
                  {schedules.filter(s => s.active).map(s => <SelectItem key={s.id} value={s.id}>{s.name} · {s.frequency}</SelectItem>)}
                </SelectContent>
              </Select></div>
            <div><Label className="text-xs">{t("payroll.periodStart")}</Label><Input type="date" value={period.start} onChange={e => setPeriod({
            ...period,
            start: e.target.value
          })} className="bg-[#0F1218] border-[#222838] w-40" /></div>
            <div><Label className="text-xs">{t("payroll.periodEnd")}</Label><Input type="date" value={period.end} onChange={e => setPeriod({
            ...period,
            end: e.target.value
          })} className="bg-[#0F1218] border-[#222838] w-40" /></div>
          </div>
          <table className="w-full text-sm mb-4">
            <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
              <th className="py-2 text-left font-medium">{t("payroll.include")}</th><th className="py-2 text-left font-medium">{t("payroll.name")}</th>
              <th className="py-2 text-left font-medium">{t("payroll.type")}</th><th className="py-2 text-right font-medium">{t("payroll.hours")}</th>
              <th className="py-2 text-right font-medium">{t("payroll.rateHr")}</th><th className="py-2 text-right font-medium">{t("payroll.commission")}</th></tr></thead>
            <tbody>
              {team.map(m => {
            const r = rows[m.id] || {};
            const isSalary = m.salary_monthly_cents > 0;
            return <tr key={m.id} className="border-b border-[#171c26]">
                    <td className="py-2"><input type="checkbox" checked={!!r.include} onChange={e => setRow(m.id, {
                  include: e.target.checked
                })} /></td>
                    <td className="py-2 text-gray-100">{m.name} <span className="text-xs text-gray-500 capitalize">· {m.role}</span></td>
                    <td className="py-2 text-gray-400 text-xs">{isSalary ? "Salary" : "Hourly"}</td>
                    <td className="py-2 text-right">{!isSalary && <Input type="number" step="0.25" min={0} value={r.hours || ""} onChange={e => setRow(m.id, {
                  hours: e.target.value
                })} className="bg-[#0F1218] border-[#222838] w-24 font-mono text-right ml-auto" placeholder="0" />}</td>
                    <td className="py-2 text-right">{!isSalary && <Input type="number" step="0.01" min={0} value={r.rate || ""} onChange={e => setRow(m.id, {
                  rate: e.target.value
                })} className="bg-[#0F1218] border-[#222838] w-24 font-mono text-right ml-auto" placeholder={centsToDollars(m.hourly_rate_cents)} />}</td>
                    <td className="py-2 text-right"><Input type="number" step="0.01" min={0} value={r.commission || ""} onChange={e => setRow(m.id, {
                  commission: e.target.value
                })} className="bg-[#0F1218] border-[#222838] w-24 font-mono text-right ml-auto" placeholder="0" /></td>
                  </tr>;
          })}
            </tbody>
          </table>
          <p className="text-xs text-gray-500 mb-3">{t("payroll.deductionsAreComputedAutomaticallyFederalWithholding")}</p>
          <div className="flex gap-2">
            <Button onClick={createRun} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="payroll-create-run">{t("payroll.createDraftRun")}</Button>
            <Button variant="ghost" onClick={() => setShowNew(false)}>{t("payroll.cancel")}</Button>
          </div>
        </Card>}

      <Tabs defaultValue="runs">
        <TabsList className="bg-[#14171E] border border-[#222838]">
          <TabsTrigger value="runs" data-testid="tab-pay-runs">{t("payroll.payRuns")}</TabsTrigger>
          <TabsTrigger value="schedules" data-testid="tab-pay-schedules">{t("payroll.schedules")}</TabsTrigger>
          <TabsTrigger value="team" data-testid="tab-pay-team">{t("payroll.teamPaySettings")}</TabsTrigger>
        </TabsList>

        <TabsContent value="runs" className="mt-4">
          {detail && <Card className="mb-4" data-testid="payroll-detail">
              <div className="mb-3 flex items-center justify-between">
                <div className="font-semibold text-gray-100">
                  {detail.period_start} → {detail.period_end}{" "}
                  {detail.schedule_name && <span className="text-xs text-gray-500 font-normal">· {detail.schedule_name}</span>}{" "}
                  <span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[detail.status] || ""}`}>{detail.status}</span>
                </div>
                <button onClick={() => setDetail(null)} className="text-gray-500 hover:text-gray-300"><X size={16} /></button>
              </div>
              <table className="w-full text-sm mb-3">
                <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                  <th className="py-2 text-left font-medium">{t("payroll.employee")}</th>
                  <th className="py-2 text-right font-medium">{t("payroll.gross")}</th>
                  <th className="py-2 text-right font-medium">{t("payroll.deductions")}</th>
                  <th className="py-2 text-right font-medium">{t("payroll.net")}</th><th /></tr></thead>
                <tbody>
                  {detail.items.map(it => <tr key={it.id} className="border-b border-[#171c26]">
                      <td className="py-2 text-gray-100">{it.name}{" "}
                        <span className="text-xs text-gray-500">{it.pay_type === "hourly" ? `${it.hours}h × ${money(it.rate_cents)}/hr` : "salary"}</span>
                        <div className="text-xs text-gray-600">{it.deductions.map(d => d.label).join(" · ")}</div></td>
                      <td className="py-2 text-right font-mono text-gray-200">{money(it.gross_cents)}</td>
                      <td className="py-2 text-right font-mono text-gray-400">{money(it.total_deductions_cents)}</td>
                      <td className="py-2 text-right font-mono text-emerald-400 font-medium">{money(it.net_cents)}</td>
                      <td className="py-2 text-right"><Button size="sm" variant="ghost" className="h-7 text-gray-400" onClick={() => doPrint(detail.id, it.id)}><Printer size={14} className="mr-1" />{t("payroll.stub")}</Button></td>
                    </tr>)}
                </tbody>
                <tfoot><tr className="font-semibold text-gray-100">
                  <td className="py-2">{t("payroll.totals")}</td>
                  <td className="py-2 text-right font-mono">{money(detail.totals.gross_cents)}</td>
                  <td className="py-2 text-right font-mono">{money(detail.totals.tax_deductions_cents + detail.totals.booth_rent_cents + detail.totals.other_deductions_cents)}</td>
                  <td className="py-2 text-right font-mono text-emerald-400">{money(detail.totals.net_cents)}</td><td />
                </tr></tfoot>
              </table>
              {detail.notes && <p className="text-xs text-gray-500 mb-2">{detail.notes}</p>}
              {detail.entry_number && <p className="text-xs text-gray-500 mb-2">{t("payroll.journalEntry")} <span className="font-mono">{detail.entry_number}</span></p>}
              <div className="flex gap-2">
                {canWrite && detail.status === "draft" && <Button size="sm" onClick={() => doApprove(detail.id)} className="bg-sky-600 hover:bg-sky-500 text-black">{t("payroll.approveRun")}</Button>}
                {canWrite && detail.status === "approved" && <Button size="sm" onClick={() => doPost(detail.id)} className="bg-emerald-600 hover:bg-emerald-500 text-black">{t("payroll.postToJournal")}</Button>}
                {canWrite && detail.status === "draft" && <Button size="sm" variant="ghost" className="text-rose-400" onClick={() => doDeleteRun(detail.id)}>{t("payroll.deleteDraft")}</Button>}
              </div>
              <p className="text-xs text-gray-600 mt-3">{detail.disclaimer}</p>
            </Card>}
          <Card className="p-0 overflow-hidden" data-testid="payroll-runs-table">
            <table className="w-full text-sm">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-5 py-3 text-left font-medium">{t("payroll.period")}</th>
                <th className="px-5 py-3 text-right font-medium">{t("payroll.gross")}</th>
                <th className="px-5 py-3 text-right font-medium">{t("payroll.deductions")}</th>
                <th className="px-5 py-3 text-right font-medium">{t("payroll.net")}</th>
                <th className="px-5 py-3 text-left font-medium">{t("payroll.status")}</th><th /></tr></thead>
              <tbody>
                {runs.map(r => <tr key={r.id} className="border-b border-[#171c26] hover:bg-[#161b25]">
                    <td className="px-5 py-3 text-gray-100">{r.period_start} → {r.period_end} <span className="text-xs text-gray-500">({r.items.length} {t("payroll.people")}</span></td>
                    <td className="px-5 py-3 text-right font-mono text-gray-200">{money(r.totals.gross_cents)}</td>
                    <td className="px-5 py-3 text-right font-mono text-gray-400">{money(r.totals.tax_deductions_cents + r.totals.booth_rent_cents + r.totals.other_deductions_cents)}</td>
                    <td className="px-5 py-3 text-right font-mono text-emerald-400 font-medium">{money(r.totals.net_cents)}</td>
                    <td className="px-5 py-3"><span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[r.status] || ""}`}>{r.status}</span></td>
                    <td className="px-5 py-3 text-right"><Button size="sm" variant="outline" className="h-7 border-[#222838]" onClick={() => setDetail(r)}>{t("payroll.open")}</Button></td>
                  </tr>)}
                {runs.length === 0 && <tr><td colSpan={6} className="px-5 py-12 text-center text-gray-500">{t("payroll.noPayRunsYet")}</td></tr>}
              </tbody>
            </table>
          </Card>

          <Card className="mt-4" data-testid="payroll-provider">
            <h3 className="text-sm font-semibold text-gray-200 mb-2">{t("payroll.payrollProvider")}</h3>
            <div className="space-y-2 text-sm">
              <div className="flex items-center justify-between rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2">
                <span className="text-gray-200"><strong>{t("payroll.manual")}</strong> <span className="text-gray-500">{t("payroll.thisAppEstimatesOnly")}</span></span>
                <span className="px-2 py-0.5 rounded-full text-xs border bg-emerald-500/10 text-emerald-400 border-emerald-500/20">{t("payroll.active")}</span>
              </div>
              {[["Gusto", "Full-service payroll: withholding, filing, direct deposit"], ["Check", "Embedded payroll API"]].map(([n, d]) => <div key={n} className="flex items-center justify-between rounded-lg border border-[#222838] px-3 py-2 opacity-70">
                  <span className="text-gray-200"><strong>{n}</strong> <span className="text-gray-500">— {d}</span></span>
                  <span className="px-2 py-0.5 rounded-full text-xs border bg-gray-500/10 text-gray-400 border-gray-500/20">{t("payroll.comingSoon")}</span>
                </div>)}
            </div>
            <p className="mt-2 text-xs text-gray-500">{t("payroll.partnershipStubWhenAProviderIs")}</p>
          </Card>
        </TabsContent>

        <TabsContent value="schedules" className="mt-4">
          <Card data-testid="payroll-schedules">
            <div className="space-y-2 text-sm mb-4">
              {schedules.map(s => <div key={s.id} className="flex items-center justify-between gap-2 border-b border-[#171c26] pb-2">
                  <span className="text-gray-200">{s.name} <span className="text-gray-500">· {s.frequency} {t("payroll.next")} {s.next_run}{!s.active && " · inactive"}</span></span>
                  {canWrite && <button onClick={() => delSched(s.id)} className="text-gray-600 hover:text-rose-400"><Trash2 size={14} /></button>}
                </div>)}
              {schedules.length === 0 && <p className="text-gray-500">{t("payroll.noSchedulesYet")}</p>}
            </div>
            {canWrite && <form className="flex flex-wrap items-end gap-2" onSubmit={saveSched}>
                <div><Label className="text-xs">{t("payroll.name")}</Label><Input value={schedForm.name} onChange={e => setSchedForm({
                ...schedForm,
                name: e.target.value
              })} className="bg-[#0F1218] border-[#222838] w-40" required placeholder={t("payroll.eGBarbers")} /></div>
                <div><Label className="text-xs">{t("payroll.frequency")}</Label>
                  <Select value={schedForm.frequency} onValueChange={v => setSchedForm({
                ...schedForm,
                frequency: v
              })}>
                    <SelectTrigger className="bg-[#0F1218] border-[#222838] w-32"><SelectValue /></SelectTrigger>
                    <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
                      <SelectItem value="weekly">{t("payroll.weekly")}</SelectItem><SelectItem value="biweekly">{t("payroll.biweekly")}</SelectItem><SelectItem value="monthly">{t("payroll.monthly")}</SelectItem>
                    </SelectContent>
                  </Select></div>
                <div><Label className="text-xs">{t("payroll.nextRun")}</Label><Input type="date" value={schedForm.next_run} onChange={e => setSchedForm({
                ...schedForm,
                next_run: e.target.value
              })} className="bg-[#0F1218] border-[#222838] w-40" /></div>
                <Button size="sm" type="submit" className="bg-amber-600 hover:bg-amber-500 text-black">{t("payroll.add")}</Button>
              </form>}
          </Card>
        </TabsContent>

        <TabsContent value="team" className="mt-4">
          <Card data-testid="payroll-team">
            {settings && canWrite && <div className="flex flex-wrap items-end gap-2 mb-4 pb-4 border-b border-[#222838]">
                <div><Label className="text-xs">{t("payroll.defaultFederalWithholdingEstimate")}</Label>
                  <Input type="number" step="0.1" min={0} value={settings.default_fed_withholding_pct ?? ""} onChange={e => setSettings({
                ...settings,
                default_fed_withholding_pct: e.target.value
              })} className="bg-[#0F1218] border-[#222838] w-32 font-mono" /></div>
                <Button size="sm" variant="outline" className="border-[#222838]" onClick={saveDefaultFed}>{t("payroll.saveDefault")}</Button>
                <span className="text-xs text-gray-500">{t("payroll.ficaIsAlways")} {settings.fica_pct}{t("payroll.estimate")}</span>
              </div>}
            <table className="w-full text-sm">
              <thead><tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="py-2 text-left font-medium">{t("payroll.member")}</th>
                <th className="py-2 text-right font-medium">{t("payroll.hourlyHr")}</th>
                <th className="py-2 text-right font-medium">{t("payroll.salaryMo")}</th>
                <th className="py-2 text-right font-medium">{t("payroll.fedWH")}</th>
                <th className="py-2 text-right font-medium">{t("payroll.boothRent")}</th>
                <th className="py-2 text-right font-medium">{t("payroll.commRate")}</th><th /></tr></thead>
              <tbody>
                {team.map(m => {
                const f = edit[m.id] || {};
                return <tr key={m.id} className="border-b border-[#171c26]">
                      <td className="py-2 text-gray-100">{m.name} <span className="text-xs text-gray-500 capitalize">· {m.role}</span></td>
                      {canWrite ? <>
                          <td className="py-2 text-right"><Input type="number" step="0.01" min={0} value={f.hourly_rate ?? ""} onChange={e => setEditRow(m.id, {
                        hourly_rate: e.target.value
                      })} className="bg-[#0F1218] border-[#222838] w-24 font-mono text-right ml-auto" placeholder="0" /></td>
                          <td className="py-2 text-right"><Input type="number" step="0.01" min={0} value={f.salary_monthly ?? ""} onChange={e => setEditRow(m.id, {
                        salary_monthly: e.target.value
                      })} className="bg-[#0F1218] border-[#222838] w-28 font-mono text-right ml-auto" placeholder="0" /></td>
                          <td className="py-2 text-right"><Input type="number" step="0.1" min={0} value={f.fed_withholding_pct ?? ""} onChange={e => setEditRow(m.id, {
                        fed_withholding_pct: e.target.value
                      })} className="bg-[#0F1218] border-[#222838] w-20 font-mono text-right ml-auto" placeholder="0" /></td>
                          <td className="py-2 text-right"><Input type="number" step="0.01" min={0} value={f.booth_rent ?? ""} onChange={e => setEditRow(m.id, {
                        booth_rent: e.target.value
                      })} className="bg-[#0F1218] border-[#222838] w-24 font-mono text-right ml-auto" placeholder="0" /></td>
                          <td className="py-2 text-right font-mono text-gray-400">{(m.commission_rate * 100).toFixed(1)}%</td>
                          <td className="py-2 text-right"><Button size="sm" className="h-7 bg-amber-600 hover:bg-amber-500 text-black" onClick={() => saveSettings(m.id)}>{t("payroll.save")}</Button></td>
                        </> : <>
                          <td className="py-2 text-right font-mono text-gray-300">{money(m.hourly_rate_cents)}</td>
                          <td className="py-2 text-right font-mono text-gray-300">{money(m.salary_monthly_cents)}</td>
                          <td className="py-2 text-right font-mono text-gray-300">{m.fed_withholding_pct}%</td>
                          <td className="py-2 text-right font-mono text-gray-300">{money(m.booth_rent_cents)}</td>
                          <td className="py-2 text-right font-mono text-gray-400">{(m.commission_rate * 100).toFixed(1)}%</td><td />
                        </>}
                    </tr>;
              })}
                {team.length === 0 && <tr><td colSpan={7} className="py-12 text-center text-gray-500">{t("payroll.noTeamMembers")}</td></tr>}
              </tbody>
            </table>
            <p className="text-xs text-gray-600 mt-3">Pay settings are stored per team member. Hourly members enter hours per run; salary members are paid their monthly salary ÷ periods per year (52/26/12). Withholding and FICA are planning estimates — not filed taxes.</p>
          </Card>
        </TabsContent>
      </Tabs>
    </div>;
}
