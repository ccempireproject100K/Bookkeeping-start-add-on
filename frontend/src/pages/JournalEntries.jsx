import { useEffect, useMemo, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money, fmtDate, today } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { Plus, Trash2, FilePlus, RefreshCw, CheckCircle2 } from "lucide-react";
import { useTranslation } from "react-i18next";
const STATUS_CLS = {
  draft: "bg-gray-500/10 text-gray-300 border-gray-500/20",
  posted: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  reversed: "bg-amber-500/10 text-amber-400 border-amber-500/20"
};
function emptyLines() {
  return [{
    account_code: "",
    debit: "",
    credit: ""
  }, {
    account_code: "",
    debit: "",
    credit: ""
  }];
}
export default function JournalEntries() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const [rows, setRows] = useState(null);
  const [accounts, setAccounts] = useState([]);
  const [filter, setFilter] = useState("all");
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState(null);
  const [date, setDate] = useState(today());
  const [memo, setMemo] = useState("");
  const [lines, setLines] = useState(emptyLines());
  const canPost = user?.role === "owner";
  const canDraft = user?.role === "owner" || user?.role === "bookkeeper";
  const load = () => {
    const q = filter === "all" ? "" : `?status=${filter}`;
    api.get(`/ledger/journal-entries${q}`).then(setRows).catch(() => setRows([]));
  };
  useEffect(() => {
    load();
  }, [filter]); // eslint-disable-line
  useEffect(() => {
    api.get("/ledger/accounts").then(setAccounts);
  }, []);
  const totals = useMemo(() => {
    const d = lines.reduce((s, l) => s + (parseFloat(l.debit) || 0), 0);
    const c = lines.reduce((s, l) => s + (parseFloat(l.credit) || 0), 0);
    return {
      d,
      c,
      balanced: Math.abs(d - c) < 0.005 && d > 0
    };
  }, [lines]);
  const reset = () => {
    setDate(today());
    setMemo("");
    setLines(emptyLines());
  };
  const save = async post => {
    const payloadLines = lines.filter(l => l.account_code && ((parseFloat(l.debit) || 0) > 0 || (parseFloat(l.credit) || 0) > 0)).map(l => ({
      account_code: l.account_code,
      debit: parseFloat(l.debit) || 0,
      credit: parseFloat(l.credit) || 0
    }));
    try {
      await api.post("/ledger/journal-entries", {
        entry_date: date,
        memo,
        lines: payloadLines,
        post
      });
      toast.success(post ? "Entry posted" : "Draft saved");
      setOpen(false);
      reset();
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const postDraft = async id => {
    try {
      await api.post(`/ledger/journal-entries/${id}/post`, {});
      toast.success(t("journalentries.posted"));
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const reverse = async id => {
    try {
      await api.post(`/ledger/journal-entries/${id}/reverse`, {});
      toast.success(t("journalentries.reversalPosted"));
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const updateLine = (i, field, val) => {
    const copy = [...lines];
    copy[i] = {
      ...copy[i],
      [field]: val
    };
    setLines(copy);
  };
  if (!rows) return <Loading />;
  return <div className="animate-rise">
      <PageHeader title={t("journalentries.journalEntries")} subtitle="Append-only double-entry postings" testid="journal-entries-page">
        {canDraft && <Button data-testid="btn-new-journal-entry" onClick={() => {
        reset();
        setOpen(true);
      }} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold">
            <Plus size={16} className="me-1" /> {t("journalentries.newEntry")}</Button>}
      </PageHeader>

      <div className="flex gap-2 mb-4">
        {["all", "draft", "posted", "reversed"].map(f => <button key={f} onClick={() => setFilter(f)} data-testid={`je-filter-${f}`} className={`px-3 py-1.5 rounded-lg text-sm capitalize border transition-colors ${filter === f ? "bg-amber-500/10 text-amber-400 border-amber-500/30" : "text-gray-400 border-[#222838] hover:bg-[#161b25]"}`}>
            {f}
          </button>)}
      </div>

      <Card className="p-0 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="table-journal-entries">
            <thead>
              <tr className="text-xs uppercase tracking-wider text-gray-500 border-b border-[#222838]">
                <th className="px-5 py-3 text-start font-medium">{t("journalentries.number")}</th>
                <th className="px-5 py-3 text-start font-medium">{t("journalentries.date")}</th>
                <th className="px-5 py-3 text-start font-medium">{t("journalentries.source")}</th>
                <th className="px-5 py-3 text-start font-medium">{t("journalentries.memo")}</th>
                <th className="px-5 py-3 text-end font-medium">{t("journalentries.amount")}</th>
                <th className="px-5 py-3 text-start font-medium">{t("journalentries.status")}</th>
                <th className="px-5 py-3 text-end font-medium">{t("journalentries.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(e => <tr key={e.id} className="border-b border-[#171c26] hover:bg-[#161b25] cursor-pointer" onClick={() => api.get(`/ledger/journal-entries/${e.id}`).then(setDetail)}>
                  <td className="px-5 py-3 font-mono text-amber-400">{e.number || "—"}</td>
                  <td className="px-5 py-3 text-gray-400">{fmtDate(e.entry_date)}</td>
                  <td className="px-5 py-3 capitalize text-gray-300">{e.source?.replace("_", " ")}</td>
                  <td className="px-5 py-3 text-gray-400 max-w-[240px] truncate">{e.memo || "—"}</td>
                  <td className="px-5 py-3 text-end font-mono text-gray-200">{money(e.total_cents)}</td>
                  <td className="px-5 py-3">
                    <span className={`px-2 py-0.5 rounded-full text-xs border capitalize ${STATUS_CLS[e.status] || ""}`}>{e.status}</span>
                  </td>
                  <td className="px-5 py-3 text-end space-x-1 rtl:space-x-reverse" onClick={ev => ev.stopPropagation()}>
                    {canPost && e.status === "draft" && <Button size="sm" data-testid="btn-post-entry" className="h-7 bg-emerald-600 hover:bg-emerald-500 text-black" onClick={() => postDraft(e.id)}>{t("journalentries.post")}</Button>}
                    {canPost && e.status === "posted" && <Button size="sm" variant="ghost" data-testid="btn-reverse-entry" className="h-7 text-amber-400 hover:bg-amber-500/10" onClick={() => reverse(e.id)}>
                        <RefreshCw size={13} className="me-1" />{t("journalentries.reverse")}</Button>}
                  </td>
                </tr>)}
              {rows.length === 0 && <tr><td colSpan={7} className="px-5 py-12 text-center text-gray-500">{t("journalentries.noEntries")}</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Create modal */}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent data-testid="journal-entry-modal" className="bg-[#14171E] border-[#222838] max-w-2xl">
          <DialogHeader><DialogTitle className="font-display">{t("journalentries.newJournalEntry")}</DialogTitle></DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div><Label className="text-xs">{t("journalentries.date")}</Label>
              <Input data-testid="input-entry-date" type="date" value={date} onChange={e => setDate(e.target.value)} className="bg-[#0F1218] border-[#222838]" /></div>
            <div><Label className="text-xs">{t("journalentries.description")}</Label>
              <Input data-testid="input-entry-description" value={memo} onChange={e => setMemo(e.target.value)} placeholder={t("journalentries.memo")} className="bg-[#0F1218] border-[#222838]" /></div>
          </div>

          <div className="mt-2 space-y-2 max-h-[320px] overflow-y-auto pe-1">
            <div className="grid grid-cols-12 gap-2 text-[10px] uppercase tracking-wider text-gray-500 px-1">
              <span className="col-span-6">{t("journalentries.account")}</span>
              <span className="col-span-3 text-end">{t("journalentries.debit")}</span>
              <span className="col-span-3 text-end">{t("journalentries.credit")}</span>
            </div>
            {lines.map((l, i) => <div key={i} className="grid grid-cols-12 gap-2 items-center">
                <div className="col-span-6">
                  <Select value={l.account_code} onValueChange={v => updateLine(i, "account_code", v)}>
                    <SelectTrigger data-testid={i === 0 ? "input-entry-debit-account" : i === 1 ? "input-entry-credit-account" : `je-line-account-${i}`} className="bg-[#0F1218] border-[#222838] h-9 text-xs"><SelectValue placeholder={t("journalentries.selectAccount")} /></SelectTrigger>
                    <SelectContent className="bg-[#14171E] border-[#222838] max-h-[260px]">
                      {accounts.map(a => <SelectItem key={a.code} value={a.code}>{a.code} — {a.name}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
                <Input data-testid={i === 0 ? "input-entry-debit-amount" : `je-line-debit-${i}`} className="col-span-3 bg-[#0F1218] border-[#222838] h-9 text-end font-mono text-xs" placeholder="0.00" value={l.debit} onChange={e => updateLine(i, "debit", e.target.value)} />
                <div className="col-span-3 flex items-center gap-1">
                  <Input data-testid={i === 1 ? "input-entry-credit-amount" : `je-line-credit-${i}`} className="bg-[#0F1218] border-[#222838] h-9 text-end font-mono text-xs" placeholder="0.00" value={l.credit} onChange={e => updateLine(i, "credit", e.target.value)} />
                  {lines.length > 2 && <button onClick={() => setLines(lines.filter((_, x) => x !== i))} className="text-gray-600 hover:text-rose-400"><Trash2 size={14} /></button>}
                </div>
              </div>)}
            <Button data-testid="btn-add-line" variant="ghost" size="sm" onClick={() => setLines([...lines, {
            account_code: "",
            debit: "",
            credit: ""
          }])} className="text-amber-400 hover:bg-amber-500/10"><FilePlus size={14} className="me-1" /> {t("journalentries.addLine")}</Button>
          </div>

          <div className="flex items-center justify-between py-2 px-3 rounded-lg bg-[#0F1218] border border-[#222838] text-sm font-mono">
            <span className="text-gray-400">{t("journalentries.debits")} <span className="text-emerald-400 ms-1">{totals.d.toFixed(2)}</span></span>
            <span className="text-gray-400">{t("journalentries.credits")} <span className="text-rose-400 ms-1">{totals.c.toFixed(2)}</span></span>
            <span data-testid="je-balance-status" className={`inline-flex items-center gap-1 ${totals.balanced ? "text-emerald-400" : "text-amber-400"}`}>
              {totals.balanced && <CheckCircle2 size={14} />}{totals.balanced ? "Balanced" : "Unbalanced"}
            </span>
          </div>

          <DialogFooter className="gap-2">
            {canDraft && <Button data-testid="btn-save-draft" variant="outline" className="border-[#2a3140]" onClick={() => save(false)}>{t("journalentries.saveDraft")}</Button>}
            {canPost && <Button data-testid="btn-post-entry-modal" disabled={!totals.balanced} onClick={() => save(true)} className="bg-emerald-600 hover:bg-emerald-500 text-black disabled:opacity-40">{t("journalentries.postEntry")}</Button>}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Detail modal */}
      <Dialog open={!!detail} onOpenChange={o => !o && setDetail(null)}>
        <DialogContent className="bg-[#14171E] border-[#222838] max-w-xl">
          {detail && <>
              <DialogHeader><DialogTitle className="font-mono text-amber-400">{detail.number || "Draft"}</DialogTitle></DialogHeader>
              <div className="text-sm text-gray-400 mb-2">{fmtDate(detail.entry_date)} • {detail.memo || "—"}</div>
              <table className="w-full text-sm">
                <thead><tr className="text-xs uppercase text-gray-500 border-b border-[#222838]">
                  <th className="py-2 text-start">{t("journalentries.account")}</th><th className="py-2 text-end">{t("journalentries.debit")}</th><th className="py-2 text-end">{t("journalentries.credit")}</th></tr></thead>
                <tbody>
                  {detail.lines?.map(l => <tr key={l.id} className="border-b border-[#171c26]">
                      <td className="py-2 font-mono text-gray-300">{l.account_code}</td>
                      <td className="py-2 text-end font-mono text-emerald-400">{l.debit ? money(l.debit) : ""}</td>
                      <td className="py-2 text-end font-mono text-rose-400">{l.credit ? money(l.credit) : ""}</td>
                    </tr>)}
                </tbody>
              </table>
            </>}
        </DialogContent>
      </Dialog>
    </div>;
}
