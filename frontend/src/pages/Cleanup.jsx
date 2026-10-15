import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { Sparkles, AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";

/** One-click books cleanup: scan for mess, fix what's safe, flag the rest. */
export default function Cleanup() {
  const { user } = useAuth();
  const canManage = user?.role === "owner";
  const [loaded, setLoaded] = useState(false);
  const [scan, setScan] = useState(null);
  const [working, setWorking] = useState(false);

  const run = () => api.get("/cleanup/scan").then(setScan).catch((e) => toast.error(errMsg(e)));
  useEffect(() => { run(); setLoaded(true); }, []);

  const apply = async () => {
    if (!confirm(`Apply all ${scan.safe_count} safe fixes? Nothing is deleted; every fix is audited.`)) return;
    setWorking(true);
    try {
      const r = await api.post("/cleanup/apply", { apply_all_safe: true });
      toast.success(`Fixed ${r.applied.length}${r.failed.length ? `, ${r.failed.length} failed` : ""} — ${r.remaining_review} still need your eyes`);
      run();
    } catch (e) { toast.error(errMsg(e)); }
    finally { setWorking(false); }
  };

  const SEV = { high: "text-rose-400", medium: "text-amber-400", low: "text-gray-400" };

  if (!loaded) return <Loading />;
  return (
    <div className="space-y-6 max-w-3xl mx-auto">
      <PageHeader title="Books cleanup" subtitle="Point it at a messy import — it fixes what's safe and flags the rest."
        actions={
          <div className="flex gap-2">
            <Button variant="outline" onClick={run}>Re-scan</Button>
            {canManage && (scan?.safe_count || 0) > 0 && (
              <Button onClick={apply} disabled={working} data-testid="cleanup-apply">
                {working ? <Loader2 size={14} className="mr-1 animate-spin" /> : <Sparkles size={14} className="mr-1" />}
                Fix {scan.safe_count} safe {scan.safe_count === 1 ? "issue" : "issues"}
              </Button>
            )}
          </div>
        } />
      {scan && (
        <div className="grid grid-cols-3 gap-3">
          <Card className="p-4"><div className="text-xs text-gray-500">Issues found</div><div className="text-2xl font-semibold">{scan.issues.length}</div></Card>
          <Card className="p-4"><div className="text-xs text-gray-500">Safe to auto-fix</div><div className="text-2xl font-semibold text-emerald-400">{scan.safe_count}</div></Card>
          <Card className="p-4"><div className="text-xs text-gray-500">Need your eyes</div><div className="text-2xl font-semibold text-amber-400">{scan.review_count}</div></Card>
        </div>
      )}
      <div className="space-y-2">
        {(scan?.issues || []).map((i) => (
          <Card key={i.id} className="p-4 flex items-start gap-3">
            {i.fix_kind === "safe"
              ? <CheckCircle2 size={16} className="text-emerald-400 mt-0.5 shrink-0" />
              : <AlertTriangle size={16} className="text-amber-400 mt-0.5 shrink-0" />}
            <div className="flex-1">
              <p className="text-sm">{i.title}</p>
              <p className="text-xs text-gray-500 mt-0.5">
                {i.fix_kind === "safe" ? `Auto-fix: ${i.fix}` : `Needs you: ${i.fix}`}
              </p>
            </div>
            <span className={`text-[10px] uppercase ${SEV[i.severity]}`}>{i.severity}</span>
          </Card>
        ))}
      </div>
      {scan && !scan.issues.length && (
        <Card className="p-10 text-center text-gray-500 text-sm">Books are clean. Nothing to fix.</Card>
      )}
      <p className="text-[11px] text-gray-600">Safe fixes: rule-based categorization and duplicate marking. Nothing is ever deleted; every fix is written to the audit log.</p>
    </div>
  );
}
