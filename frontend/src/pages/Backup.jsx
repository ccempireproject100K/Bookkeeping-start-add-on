import { useState } from "react";
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
            <Download size={15} className="mr-1" /> {busy ? "Working…" : "Export backup"}
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
              <Upload size={15} className="mr-1" /> {t("backup.restoreBackup")}</Button>
          </div>
        </Card>
      </div>
    </div>;
}
