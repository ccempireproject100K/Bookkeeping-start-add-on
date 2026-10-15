import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { Search } from "lucide-react";

const INPUT_CLS = "bg-[#0F1218] border-[#222838]";

export default function AuditLog() {
  const { user } = useAuth();
  const canView = user?.role === "owner" || user?.role === "accountant";
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [fAction, setFAction] = useState("");
  const [fEntity, setFEntity] = useState("");
  const [fActor, setFActor] = useState("");
  const [fDays, setFDays] = useState("30");

  const load = async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ days: fDays, limit: "200" });
      if (fAction.trim()) params.set("action", fAction.trim());
      if (fEntity.trim()) params.set("entity", fEntity.trim());
      if (fActor.trim()) params.set("actor", fActor.trim());
      const r = await api.get(`/audit?${params.toString()}`);
      setRows(r.rows || []);
    } catch (e) { toast.error(errMsg(e)); } finally { setLoading(false); }
  };

  useEffect(() => { if (canView) load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (!canView) return <Loading />;
  return (
    <div className="animate-rise">
      <PageHeader
        title="Audit Log"
        subtitle="Who did what, when — every change in this workspace"
        testid="audit-page"
      />
      <Card>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
          <div>
            <Label className="text-xs">Action contains</Label>
            <Input value={fAction} onChange={(e) => setFAction(e.target.value)}
              placeholder="e.g. tax, invoice, login" className={INPUT_CLS} data-testid="audit-f-action" />
          </div>
          <div>
            <Label className="text-xs">Entity</Label>
            <Input value={fEntity} onChange={(e) => setFEntity(e.target.value)}
              placeholder="e.g. contractor" className={INPUT_CLS} data-testid="audit-f-entity" />
          </div>
          <div>
            <Label className="text-xs">Actor</Label>
            <Input value={fActor} onChange={(e) => setFActor(e.target.value)}
              placeholder="Name" className={INPUT_CLS} data-testid="audit-f-actor" />
          </div>
          <div>
            <Label className="text-xs">Look back</Label>
            <Select value={fDays} onValueChange={setFDays}>
              <SelectTrigger className={INPUT_CLS} data-testid="audit-f-days">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="7">Last 7 days</SelectItem>
                <SelectItem value="30">Last 30 days</SelectItem>
                <SelectItem value="90">Last 90 days</SelectItem>
                <SelectItem value="365">Last year</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-end">
            <Button onClick={load} className="w-full" data-testid="audit-search">
              <Search className="w-4 h-4 mr-2" /> Search
            </Button>
          </div>
        </div>
        {loading ? <Loading /> : rows.length === 0 ? (
          <p className="text-sm text-gray-500 py-8 text-center">No audit entries match.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="audit-table">
              <thead>
                <tr className="text-left text-gray-500 border-b border-[#1b2230]">
                  <th className="py-2 pr-4 font-medium">When</th>
                  <th className="py-2 pr-4 font-medium">Actor</th>
                  <th className="py-2 pr-4 font-medium">Action</th>
                  <th className="py-2 pr-4 font-medium">Entity</th>
                  <th className="py-2 font-medium">Details</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-b border-[#141821] text-gray-300">
                    <td className="py-2 pr-4 whitespace-nowrap text-gray-500">
                      {fmtDate(r.created_at)}
                    </td>
                    <td className="py-2 pr-4">
                      {r.actor_name || "—"}
                      <span className="text-gray-600 text-xs"> · {r.actor_role || ""}</span>
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs text-sky-300">{r.action}</td>
                    <td className="py-2 pr-4 text-gray-400">{r.entity}{r.entity_id ? ` · ${String(r.entity_id).slice(0, 8)}` : ""}</td>
                    <td className="py-2 font-mono text-xs text-gray-500 max-w-[280px] truncate" title={JSON.stringify(r.details || {})}>
                      {JSON.stringify(r.details || {})}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
