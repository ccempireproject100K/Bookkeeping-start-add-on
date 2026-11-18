import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import client from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { toast } from "sonner";
import { Download, Upload } from "lucide-react";
import { useTranslation } from "react-i18next";
export default function Backup() {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const isAdmin = user?.role === "owner";
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState(null);
  const [confirm, setConfirm] = useState(false);
  const doExport = async () => {
    setBusy(true);
    try {
      // Encrypted by default (Fernet, TOKEN_ENCRYPTION_KEY). The server
      // returns a .enc blob; only it can decrypt it on restore.
      const res = await client.post("/backup/export", {}, {
        responseType: "blob"
      });
      const url = window.URL.createObjectURL(new Blob([res.data], {
        type: "application/octet-stream"
      }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `bookkeeping-backup-${new Date().toISOString().slice(0, 10)}.enc`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
      toast.success(t("backup.encryptedBackupDownloaded"));
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const doImport = async () => {
    if (!file) {
      toast.error(t("backup.chooseABackupFileFirst"));
      return;
    }
    if (!confirm) {
      toast.error(t("backup.tickTheConfirmationCheckboxToProceed"));
      return;
    }
    const text = await file.text();
    let dump = null;
    let isPlaintext = false;
    try {
      const parsed = JSON.parse(text);
      if (parsed.format === "bookkeeping-backup" && parsed.collections) {
        dump = parsed.collections; // legacy plaintext export
        isPlaintext = true;
      }
    } catch {/* not JSON — treat as encrypted below */}
    if (dump === null) {
      // Encrypted .enc file: the server decrypts it (it holds the key).
      dump = text.trim();
    }
    if (!isPlaintext && !window.confirm("Restore this encrypted backup? ALL current business data will be replaced. This can't be undone.")) return;
    if (isPlaintext && !window.confirm("Restore this backup? ALL current business data will be replaced. This can't be undone.")) return;
    setBusy(true);
    try {
      const r = await api.post("/backup/import", {
        confirm: true,
        dump
      });
      toast.success(`Restored ${r.restored_documents} record(s) across ${r.collections} collection(s)`);
      setFile(null);
      setConfirm(false);
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  if (!isAdmin) return <Loading />; // page is admin-only; nav hides it for others
  return <div className="animate-rise">
      <PageHeader title={t("backup.backupRestore")} subtitle="Snapshot and replace workspace business data" testid="backup-page" />

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="p-5">
          <h3 className="text-sm font-semibold text-gray-100">{t("backup.exportBackup")}</h3>
          <p className="mt-1 text-xs text-gray-500">{t("backup.downloadsAJSONSnapshotOfInvoices")}</p>
          <Button data-testid="backup-export" onClick={doExport} disabled={busy} className="mt-4 bg-amber-600 hover:bg-amber-500 text-black font-semibold">
            <Download size={15} className="me-1" /> {busy ? "Working…" : "Export backup"}
          </Button>
        </Card>

        <Card className="p-5">
          <h3 className="text-sm font-semibold text-gray-100">{t("backup.restoreFromBackup")}</h3>
          <p className="mt-1 text-xs text-gray-500">{t("backup.replacesCurrentBusinessDataInThe")}</p>
          <div className="mt-4 space-y-3">
            <div><Label className="text-xs">{t("backup.backupFile")}</Label>
              <Input type="file" accept=".json,application/json" data-testid="backup-file" onChange={e => setFile(e.target.files?.[0] || null)} className="bg-[#0F1218] border-[#222838] text-gray-300" /></div>
            <label className="flex items-start gap-2 text-xs text-gray-400">
              <Checkbox checked={confirm} onCheckedChange={setConfirm} data-testid="backup-confirm" className="mt-0.5" />
              <span>{t("backup.iUnderstandThis")} <span className="text-rose-400 font-medium">{t("backup.replacesAllCurrentBusinessData")}</span> {t("backup.withTheBackupContents")}</span>
            </label>
            <Button data-testid="backup-import" onClick={doImport} disabled={busy || !file} variant="destructive">
              <Upload size={15} className="me-1" /> {t("backup.restoreBackup")}</Button>
          </div>
        </Card>
      </div>

      <FullDataOut />
    </div>
  );
}

function fmtBytes(n) {
  if (!n && n !== 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function FullDataOut() {
  const [summary, setSummary] = useState(null);
  const [backups, setBackups] = useState([]);
  const [settings, setSettings] = useState(null);
  const [busy, setBusy] = useState(false);
  const [rfile, setRfile] = useState(null);
  const [validation, setValidation] = useState(null);
  const [confirmText, setConfirmText] = useState("");
  const [testing, setTesting] = useState(null);

  const load = async () => {
    try {
      const [s, b] = await Promise.all([api.get("/data-out/summary"), api.get("/data-out/backups")]);
      setSummary(s); setBackups(b.backups || []); setSettings(b.settings || null);
    } catch (e) { toast.error(errMsg(e)); }
  };
  useEffect(() => { load(); }, []);

  const downloadBlob = async (path, filename) => {
    const res = await client.get(path, { responseType: "blob" });
    const url = window.URL.createObjectURL(new Blob([res.data], { type: "application/zip" }));
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    window.URL.revokeObjectURL(url);
  };

  const doFullExport = async () => {
    setBusy(true);
    try {
      await downloadBlob("/data-out/export.zip", `bookkeeping-full-export-${new Date().toISOString().slice(0, 10)}.zip`);
      toast.success("Full export downloaded — every record, attachment and the audit trail");
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };

  const doBackupNow = async () => {
    setBusy(true);
    try {
      const r = await api.post("/data-out/backups/now", {});
      toast.success(`Backup saved (${r.documents} records${r.pruned ? `, ${r.pruned} old backup(s) pruned` : ""})`);
      load();
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };

  const saveSettings = async () => {
    setBusy(true);
    try {
      const s = await api.put("/data-out/backups/settings", settings);
      setSettings(s); toast.success("Backup schedule saved"); load();
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };

  const doTest = async (id) => {
    setTesting(id);
    try {
      const r = await api.post(`/data-out/backups/${id}/test`, {});
      if (r.passed) toast.success(`Restore test PASSED — ${r.documents} records verified`);
      else toast.error(`Restore test FAILED — ${r.detail}`);
      load();
    } catch (e) { toast.error(errMsg(e)); } finally { setTesting(null); }
  };

  const doValidate = async () => {
    if (!rfile) return;
    setBusy(true); setValidation(null);
    try {
      const fd = new FormData(); fd.append("file", rfile);
      const r = await client.post("/data-out/restore/validate", fd);
      setValidation(r.data);
      if (!r.data.ok) toast.error("That export does not validate — see the reasons below");
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };

  const doRestore = async () => {
    if (!rfile) return;
    setBusy(true);
    try {
      const fd = new FormData(); fd.append("file", rfile);
      if (confirmText) fd.append("confirm_text", confirmText);
      const r = await client.post("/data-out/restore", fd);
      toast.success(`Restored and verified: ${r.data.restored_documents} records`);
      setRfile(null); setValidation(null); setConfirmText(""); load();
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };

  return (
    <div className="mt-4 space-y-4" data-testid="dataout-panel">
      <Card className="p-5">
        <h3 className="text-sm font-semibold text-gray-100">Download everything</h3>
        <p className="mt-1 text-xs text-gray-500">
          One ZIP with <span className="text-gray-300">everything</span>: all books and ledger data,
          customers, vendors, jobs, invoices, bills, receipts <em>with their files attached</em>,
          settings, and the full audit trail — plus a README that documents every file.
          The format is plain JSON and CSV: <span className="text-gray-300">your data is usable
          without us — no lock-in.</span>
        </p>
        {summary && (
          <p className="mt-2 text-xs text-gray-400" data-testid="dataout-summary">
            Inside: {summary.documents} records across {Object.keys(summary.collections || {}).length} collections,
            {" "}{summary.attachments?.count || 0} attachment file(s) ({fmtBytes(summary.attachments?.bytes)}).
            {" "}Rough size before compression: {fmtBytes(summary.approx_size_bytes)} ({summary.approx_size_note?.toLowerCase()}).
          </p>
        )}
        <Button data-testid="dataout-export" onClick={doFullExport} disabled={busy} className="mt-4 bg-amber-600 hover:bg-amber-500 text-black font-semibold">
          <Download size={15} className="mr-1" /> {busy ? "Working…" : "Download everything"}
        </Button>
      </Card>

      <Card className="p-5">
        <h3 className="text-sm font-semibold text-gray-100">Scheduled backups</h3>
        <p className="mt-1 text-xs text-gray-500">
          Automatic full backups in the same format, kept in the list below. Off by default —
          turn them on when you want them. Older backups past the keep-limit are pruned and shown as gone.
        </p>
        {settings && (
          <div className="mt-3 flex flex-wrap items-end gap-4">
            <label className="flex items-center gap-2 text-xs text-gray-300">
              <Checkbox data-testid="dataout-sched-enabled" checked={!!settings.enabled}
                onCheckedChange={(v) => setSettings({ ...settings, enabled: !!v })} />
              Automatic backups on
            </label>
            <div><Label className="text-xs">Every (days)</Label>
              <Input type="number" min="1" max="90" value={settings.cadence_days}
                onChange={(e) => setSettings({ ...settings, cadence_days: Number(e.target.value) })}
                className="w-20 bg-[#0F1218] border-[#222838] text-gray-300" /></div>
            <div><Label className="text-xs">Keep last</Label>
              <Input type="number" min="1" max="60" value={settings.keep}
                onChange={(e) => setSettings({ ...settings, keep: Number(e.target.value) })}
                className="w-20 bg-[#0F1218] border-[#222838] text-gray-300" /></div>
            <Button data-testid="dataout-sched-save" onClick={saveSettings} disabled={busy} variant="secondary">Save schedule</Button>
            <Button data-testid="dataout-backup-now" onClick={doBackupNow} disabled={busy} className="bg-amber-600 hover:bg-amber-500 text-black font-semibold">Back up now</Button>
          </div>
        )}
        <div className="mt-4 space-y-2">
          {backups.length === 0 && <p className="text-xs text-gray-500">No stored backups yet.</p>}
          {backups.map((b) => (
            <div key={b.id} className="flex flex-wrap items-center gap-3 rounded border border-[#222838] px-3 py-2 text-xs text-gray-300" data-testid="dataout-backup-row">
              <span className="font-medium text-gray-100">{String(b.created_at || "").slice(0, 10)}</span>
              <span>{b.kind}</span>
              <span>{b.documents} records · {fmtBytes(b.size_bytes)}</span>
              <span data-testid="dataout-last-test">
                {b.last_test ? `Restore test ${b.last_test.passed ? "PASSED" : "FAILED"} · ${String(b.last_test.at || "").slice(0, 10)}` : "Never restore-tested"}
              </span>
              <span className="ml-auto flex gap-2">
                <Button size="sm" variant="secondary" onClick={() => downloadBlob(`/data-out/backups/${b.id}/download`, "bookkeeping-full-export.zip").catch((e) => toast.error(errMsg(e)))}>Download</Button>
                <Button size="sm" variant="secondary" data-testid="dataout-test-btn" disabled={testing === b.id} onClick={() => doTest(b.id)}>{testing === b.id ? "Testing…" : "Test this backup"}</Button>
              </span>
            </div>
          ))}
        </div>
      </Card>

      <Card className="p-5">
        <h3 className="text-sm font-semibold text-gray-100">Restore a full export</h3>
        <p className="mt-1 text-xs text-gray-500">
          Upload a "Download everything" ZIP. It is <span className="text-gray-300">validated first —
          if anything does not reconcile, nothing is restored</span> and you are told why in plain words.
          Restoring over a workspace that already has data requires typing the workspace name exactly.
        </p>
        <div className="mt-4 space-y-3">
          <div><Label className="text-xs">Full export ZIP</Label>
            <Input type="file" accept=".zip,application/zip" data-testid="dataout-restore-file"
              onChange={(e) => { setRfile(e.target.files?.[0] || null); setValidation(null); }}
              className="bg-[#0F1218] border-[#222838] text-gray-300" /></div>
          <div className="flex gap-2">
            <Button data-testid="dataout-validate" onClick={doValidate} disabled={busy || !rfile} variant="secondary">Validate only</Button>
            <Button data-testid="dataout-restore" onClick={doRestore} disabled={busy || !rfile} variant="destructive">
              <Upload size={15} className="mr-1" /> Restore
            </Button>
          </div>
          {validation && (
            <div className="rounded border border-[#222838] p-3 text-xs" data-testid="dataout-validation">
              {validation.ok ? (
                <p className="text-emerald-400">
                  Valid ✓ — {validation.manifest?.documents} records from
                  {" "}{validation.manifest?.tenant_name || validation.manifest?.tenant_id},
                  exported {String(validation.manifest?.exported_at || "").slice(0, 10)}.
                  Journal in the manifest: dr {validation.manifest?.journal?.debit_cents}¢ /
                  cr {validation.manifest?.journal?.credit_cents}¢.
                </p>
              ) : (
                <ul className="list-disc pl-4 text-rose-400">
                  {(validation.errors || []).map((e, i) => <li key={i}>{e}</li>)}
                </ul>
              )}
            </div>
          )}
          <div><Label className="text-xs">Workspace name (only needed when restoring over existing data)</Label>
            <Input value={confirmText} onChange={(e) => setConfirmText(e.target.value)}
              data-testid="dataout-confirm-text" placeholder="Type it exactly to confirm replacement"
              className="bg-[#0F1218] border-[#222838] text-gray-300" /></div>
        </div>
      </Card>
    </div>
  );
}
