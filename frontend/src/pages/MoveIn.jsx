import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { Upload, FlaskConical, CheckCircle2, AlertTriangle, Paperclip, BookOpen, Loader2 } from "lucide-react";

const money = (c) => `$${((c || 0) / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

/**
 * Move in from QuickBooks: drop QBO CSV exports, read the dry-run report,
 * adjust the account map, then apply. Practice run first if you want —
 * it lands in a throwaway workspace and never touches the real books.
 */
export default function MoveIn() {
  const { user } = useAuth();
  const canManage = user?.role === "owner";
  const fileRef = useRef(null);
  const attRef = useRef(null);
  const [files, setFiles] = useState([]);
  const [report, setReport] = useState(null);
  const [runId, setRunId] = useState(null);
  const [accountMap, setAccountMap] = useState([]);
  const [applied, setApplied] = useState(null);
  const [attachments, setAttachments] = useState(null);
  const [reportsMap, setReportsMap] = useState(null);
  const [working, setWorking] = useState("");

  useEffect(() => { api.get("/migration/reports-map").then(setReportsMap).catch(() => {}); }, []);

  const readFiles = async (list, setter) => {
    const out = [];
    for (const f of list) out.push({ filename: f.name, content: await f.text() });
    setter(out);
    if (out.length) toast.success(`${out.length} file${out.length > 1 ? "s" : ""} ready`);
  };

  const dryRun = async () => {
    setWorking("dry");
    try {
      const r = await api.post("/migration/qbo/dry-run", { files });
      setReport(r); setRunId(r.run_id); setAccountMap(r.account_map || []);
      setApplied(null); setAttachments(null);
    } catch (e) { toast.error(errMsg(e)); }
    finally { setWorking(""); }
  };

  const saveMapping = async () => {
    setWorking("map");
    try {
      await api.post(`/migration/qbo/runs/${runId}/mapping`, { account_map: accountMap });
      toast.success("Account map saved");
    } catch (e) { toast.error(errMsg(e)); }
    finally { setWorking(""); }
  };

  const apply = async () => {
    if (!confirm("Bring this data into your real books now? The dry-run report above is exactly what will post.")) return;
    setWorking("apply");
    try {
      const r = await api.post(`/migration/qbo/runs/${runId}/apply`, { practice: false });
      setApplied(r);
      toast.success(`Moved in: ${r.result.journal_entries} journal entries, ${r.result.customers} customers, ${r.result.vendors} vendors`);
    } catch (e) { toast.error(errMsg(e)); }
    finally { setWorking(""); }
  };

  const practice = async () => {
    setWorking("practice");
    try {
      const r = await api.post("/migration/qbo/practice-run", {});
      setApplied(r); setRunId(r.run_id);
      toast.success("Practice move-in finished — your real books were not touched");
    } catch (e) { toast.error(errMsg(e)); }
    finally { setWorking(""); }
  };

  const attach = async (list) => {
    const payload = [];
    for (const f of list) {
      const buf = await f.arrayBuffer();
      let bin = ""; const bytes = new Uint8Array(buf);
      for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      payload.push({ filename: f.name, content_base64: btoa(bin) });
    }
    setWorking("attach");
    try {
      const r = await api.post(`/migration/qbo/runs/${runId}/attachments`, { files: payload });
      setAttachments(r);
      toast.success(`${r.summary.attached} attached, ${r.summary.unmatched} waiting in the unmatched pile`);
    } catch (e) { toast.error(errMsg(e)); }
    finally { setWorking(""); }
  };

  const tb = report?.trial_balance;

  return (
    <div className="space-y-6 max-w-4xl mx-auto">
      <PageHeader title="Move in from QuickBooks"
        subtitle="Export your QuickBooks Online lists and history as CSVs, drop them here, and read the full report before a single number posts."
        actions={canManage && (
          <Button variant="outline" onClick={practice} disabled={!!working} data-testid="movein-practice">
            {working === "practice" ? <Loader2 size={14} className="mr-1 animate-spin" /> : <FlaskConical size={14} className="mr-1" />}
            Practice run first
          </Button>)} />

      <Card className="p-5 space-y-3">
        <div className="font-medium">1 · Your QuickBooks exports</div>
        <p className="text-sm text-gray-400">Chart of Accounts, Customers, Vendors, Products/Services, and a Transaction List or General Ledger export. Each file is identified for you — and named plainly if it isn't recognized.</p>
        <input ref={fileRef} type="file" multiple accept=".csv" className="hidden"
          onChange={(e) => readFiles(e.target.files, setFiles)} />
        <div className="flex items-center gap-3">
          <Button variant="outline" onClick={() => fileRef.current?.click()}>
            <Upload size={14} className="mr-1" /> Choose CSV files
          </Button>
          <span className="text-sm text-gray-400">{files.length ? files.map((f) => f.filename).join(", ") : "No files yet"}</span>
          {canManage && files.length > 0 && (
            <Button onClick={dryRun} disabled={!!working} data-testid="movein-dryrun">
              {working === "dry" ? <Loader2 size={14} className="mr-1 animate-spin" /> : null} Dry-run report
            </Button>)}
        </div>
      </Card>

      {report && (
        <Card className="p-5 space-y-4">
          <div className="font-medium">2 · The dry-run report — nothing has posted yet</div>
          <div className="grid grid-cols-3 sm:grid-cols-6 gap-3 text-center">
            {[["Accounts", report.counts.accounts], ["Customers", report.counts.customers],
              ["Vendors", report.counts.vendors], ["Items", report.counts.items],
              ["Entries", report.counts.transaction_entries], ["Lines", report.counts.transaction_lines]
            ].map(([label, n]) => (
              <div key={label}><div className="text-2xl font-semibold">{n}</div>
                <div className="text-xs text-gray-500">{label}</div></div>))}
          </div>
          <div className="text-sm space-y-1">
            {report.files.map((f) => (
              <div key={f.filename} className="flex gap-2">
                {f.kind ? <CheckCircle2 size={15} className="text-emerald-400 mt-0.5" /> : <AlertTriangle size={15} className="text-amber-400 mt-0.5" />}
                <span><b>{f.filename}</b> — {f.kind ? `${f.kind}, ${f.rows} rows` : f.reason}</span>
              </div>))}
          </div>
          {tb && (
            <div className={`text-sm rounded-md p-3 ${tb.balanced ? "bg-emerald-500/10 text-emerald-300" : "bg-rose-500/10 text-rose-300"}`}>
              Trial balance: debits {money(tb.total_debit_cents)} · credits {money(tb.total_credit_cents)} —{" "}
              {tb.balanced ? "balanced, safe to apply." : `OUT OF BALANCE by ${money(Math.abs(tb.difference_cents))}. An unbalanced set is refused — nothing half-posts.`}
            </div>)}
          {(report.unmapped || []).length > 0 && (
            <div className="text-sm space-y-1">
              <div className="font-medium text-amber-300">Needs your eyes ({report.unmapped.length})</div>
              {report.unmapped.slice(0, 12).map((p, i) => <div key={i} className="text-gray-300">• {p.reason}</div>)}
            </div>)}
          <div>
            <div className="font-medium mb-2">Account map — QuickBooks → Books (adjust the code if you want a different home)</div>
            <div className="space-y-1">
              {accountMap.map((m, i) => (
                <div key={m.qbo_name} className="flex items-center gap-2 text-sm">
                  <span className="flex-1">{m.qbo_name} <span className="text-gray-500">({m.type})</span></span>
                  <span className="text-gray-500">{m.action === "map" ? "use existing" : "create"}</span>
                  <input className="w-24 bg-transparent border border-gray-700 rounded px-2 py-1" value={m.books_code}
                    onChange={(e) => { const next = [...accountMap]; next[i] = { ...m, books_code: e.target.value }; setAccountMap(next); }} />
                </div>))}
            </div>
            {accountMap.some((m) => m.note) && (
              <div className="text-xs text-gray-500 mt-2">{accountMap.filter((m) => m.note).map((m) => `${m.qbo_name}: ${m.note}`).join(" · ")}</div>)}
            {canManage && (
              <div className="flex gap-2 mt-3">
                <Button variant="outline" onClick={saveMapping} disabled={!!working}>Save map</Button>
                <Button onClick={apply} disabled={!!working || !tb?.balanced} data-testid="movein-apply">
                  {working === "apply" ? <Loader2 size={14} className="mr-1 animate-spin" /> : null}
                  Bring it into my books
                </Button>
              </div>)}
          </div>
        </Card>
      )}

      {applied && (
        <Card className="p-5 space-y-2">
          <div className="font-medium flex items-center gap-2"><CheckCircle2 size={16} className="text-emerald-400" />
            {applied.status === "applied_practice" ? "Practice move-in complete — real books untouched" : "Move-in complete"}</div>
          <div className="text-sm text-gray-300">
            {applied.result.accounts_created} accounts created · {applied.result.customers} customers ·{" "}
            {applied.result.vendors} vendors · {applied.result.items} items ·{" "}
            {applied.result.journal_entries} journal entries
            {applied.result.skipped_existing > 0 && ` · ${applied.result.skipped_existing} already here (skipped, no duplicates)`}
          </div>
          {applied.tidy_findings && applied.tidy_findings.count != null && (
            <div className="text-sm text-gray-400">DataClean tidy scan: {applied.tidy_findings.count} finding{applied.tidy_findings.count === 1 ? "" : "s"} over the imported data — see Books Cleanup.</div>)}
          <div className="pt-2">
            <div className="font-medium mb-1">3 · Bring your attachments over</div>
            <p className="text-sm text-gray-400 mb-2">Upload your old invoice and receipt files. They attach themselves to the matching record by number or name; anything that can't be matched waits in the pile — nothing is dropped.</p>
            <input ref={attRef} type="file" multiple className="hidden" onChange={(e) => attach(e.target.files)} />
            <Button variant="outline" onClick={() => attRef.current?.click()} disabled={!!working}>
              <Paperclip size={14} className="mr-1" /> Upload attachments
            </Button>
            {attachments && (
              <div className="text-sm mt-2 space-y-1">
                <div>{attachments.summary.attached} attached · {attachments.summary.unmatched} unmatched</div>
                {attachments.attachments.filter((a) => a.status === "unmatched").map((a) => (
                  <div key={a.id} className="text-amber-300">• {a.filename} — waiting to be placed</div>))}
              </div>)}
          </div>
        </Card>
      )}

      {reportsMap && (
        <Card className="p-5 space-y-2">
          <div className="font-medium flex items-center gap-2"><BookOpen size={16} /> Where did my reports go?</div>
          <p className="text-sm text-gray-400">{reportsMap.note}</p>
          <div className="text-sm space-y-1">
            {reportsMap.reports.map((r) => (
              <div key={r.qbo} className="flex gap-2"><span className="flex-1"><b>{r.qbo}</b> → {r.books}</span></div>))}
          </div>
        </Card>
      )}
    </div>
  );
}
