import { useEffect, useState } from "react";
import { api, errMsg } from "@/lib/api";
import { money } from "@/lib/format";
import { PageHeader, Card, Loading } from "@/components/Page";
import { useAuth } from "@/context/AuthContext";
import { toast } from "sonner";
import { Building2 } from "lucide-react";

/** Accountant firm view: every client workspace, with a health snapshot. */
export default function FirmDashboard() {
  const { user } = useAuth();
  const [loaded, setLoaded] = useState(false);
  const [clients, setClients] = useState([]);

  useEffect(() => {
    if (!["owner", "accountant"].includes(user?.role)) { setLoaded(true); return; }
    api.get("/whitelabel/clients").then(setClients).catch((e) => toast.error(errMsg(e)));
    setLoaded(true);
  }, [user]);

  if (!loaded) return <Loading />;
  if (!["owner", "accountant"].includes(user?.role))
    return <Card className="p-10 text-center text-gray-500 text-sm">Accountants and owners only.</Card>;

  return (
    <div className="space-y-6">
      <PageHeader title="Firm dashboard" subtitle={`${clients.length} client workspace${clients.length === 1 ? "" : "s"} under your care.`} />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {clients.map((c) => (
          <Card key={c.tenant_id} className="p-5">
            <div className="flex items-center gap-2 mb-3">
              <Building2 size={16} className="text-blue-400" />
              <div className="font-medium">{c.name}</div>
            </div>
            <div className="grid grid-cols-3 gap-2 text-center">
              <div className="rounded-md bg-[#0F1218] p-2.5">
                <div className="text-xs text-gray-500">Open AR</div>
                <div className="font-semibold">{money(c.ar_cents)}</div>
              </div>
              <div className="rounded-md bg-[#0F1218] p-2.5">
                <div className="text-xs text-gray-500">Overdue</div>
                <div className={`font-semibold ${c.overdue_invoices ? "text-rose-400" : ""}`}>{c.overdue_invoices}</div>
              </div>
              <div className="rounded-md bg-[#0F1218] p-2.5">
                <div className="text-xs text-gray-500">Open AP</div>
                <div className="font-semibold">{money(c.ap_cents)}</div>
              </div>
            </div>
            {c.last_activity && <p className="text-[10px] text-gray-600 mt-2">Last activity {c.last_activity.slice(0, 10)}</p>}
          </Card>
        ))}
      </div>
      {!clients.length && (
        <Card className="p-10 text-center text-gray-500 text-sm">No client workspaces yet — invite clients from Settings → Team.</Card>
      )}
    </div>
  );
}
