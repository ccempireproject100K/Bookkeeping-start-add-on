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

export default function Backup() {
  const { user } = useAuth();
  const isAdmin = user?.role === "owner";
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState(null);
  const [confirm, setConfirm] = useState(false);

  const doExport = async () => {
    setBusy(true);
    try {
      const res = await client.post("/backup/export", {}, { responseType: "blob" });
      const url = window.URL.createObjectURL(new Blob([res.data], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `bookkeeping-backup-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
      toast.success("Backup downloaded");
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };

  const doImport = async () => {
    if (!file) { toast.error("Choose a backup file first"); return; }
    if (!confirm) { toast.error("Tick the confirmation checkbox to proceed"); return; }
    let dump;
    try {
      dump = JSON.parse(await file.text());
    } catch {
      toast.error("That file is not valid JSON");
      return;
    }
    if (dump.format !== "bookkeeping-backup" || !dump.collections) {
      toast.error("Not a bookkeeping backup file");
      return;
    }
    if (!window.confirm("Restore this backup? ALL current business data will be replaced. This can't be undone.")) return;
    setBusy(true);
    try {
      const r = await api.post("/backup/import", { confirm: true, dump: dump.collections });
      toast.success(`Restored ${r.restored_documents} record(s) across ${r.collections} collection(s)`);
      setFile(null); setConfirm(false);
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };

  if (!isAdmin) return <Loading />;  // page is admin-only; nav hides it for others
  return (
    <div className="animate-rise">
      <PageHeader title="Backup & Restore" subtitle="Snapshot and replace workspace business data" testid="backup-page" />

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="p-5">
          <h3 className="text-sm font-semibold text-gray-100">Export backup</h3>
          <p className="mt-1 text-xs text-gray-500">
            Downloads a JSON snapshot of invoices, bills, expenses, customers, vendors,
            products, jobs, ledger entries and more. Users and audit history are not included.
          </p>
          <Button data-testid="backup-export" onClick={doExport} disabled={busy} className="mt-4 bg-amber-600 hover:bg-amber-500 text-black font-semibold">
            <Download size={15} className="mr-1" /> {busy ? "Working…" : "Export backup"}
          </Button>
        </Card>

        <Card className="p-5">
          <h3 className="text-sm font-semibold text-gray-100">Restore from backup</h3>
          <p className="mt-1 text-xs text-gray-500">
            Replaces current business data in the covered collections with the contents of
            an exported backup file. The server refuses the restore unless confirmed.
          </p>
          <div className="mt-4 space-y-3">
            <div><Label className="text-xs">Backup file</Label>
              <Input type="file" accept=".json,application/json" data-testid="backup-file"
                onChange={(e) => setFile(e.target.files?.[0] || null)} className="bg-[#0F1218] border-[#222838] text-gray-300" /></div>
            <label className="flex items-start gap-2 text-xs text-gray-400">
              <Checkbox checked={confirm} onCheckedChange={setConfirm} data-testid="backup-confirm" className="mt-0.5" />
              <span>I understand this <span className="text-rose-400 font-medium">replaces all current business data</span> with the backup contents.</span>
            </label>
            <Button data-testid="backup-import" onClick={doImport} disabled={busy || !file} variant="destructive">
              <Upload size={15} className="mr-1" /> Restore backup
            </Button>
          </div>
        </Card>
      </div>
    </div>
  );
}
