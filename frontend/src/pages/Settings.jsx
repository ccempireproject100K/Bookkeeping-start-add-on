import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageHeader, Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { api, errMsg } from "@/lib/api";
import { toast } from "sonner";
import { LogOut, Package } from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import TotpSettings from "@/components/TotpSettings";
import PasskeySettings from "@/components/PasskeySettings";
import SecurityAlerts from "@/components/SecurityAlerts";
import TeamInvites from "@/components/TeamInvites";

function InventoryCard() {
  const { user, refresh } = useAuth();
  const [busy, setBusy] = useState(false);
  if (user?.role !== "owner") return null;
  const enabled = !!user?.tenant?.settings?.inventory_enabled;
  const toggle = async () => {
    setBusy(true);
    try {
      await api.patch("/auth/tenant/settings", { inventory_enabled: !enabled });
      await refresh();
      toast.success(enabled ? "Inventory add-on turned off" : "Inventory add-on turned on");
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };
  return (
    <Card>
      <h3 className="font-semibold text-gray-100 mb-1 flex items-center gap-2">
        <Package className="w-4 h-4" /> Inventory add-on
      </h3>
      <p className="text-sm text-gray-500 mb-4">
        {enabled
          ? "Inventory tracking is on — products and stock levels are available."
          : "Inventory tracking is off — the Inventory page shows an explanatory message instead. Your data is kept and comes back when you turn it on."}
      </p>
      <Button variant={enabled ? "destructive" : "default"} onClick={toggle} disabled={busy} data-testid="settings-inventory-toggle">
        {busy ? "Working…" : enabled ? "Turn inventory off" : "Turn inventory on"}
      </Button>
    </Card>
  );
}

function SessionsCard() {
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  const revokeAll = async (everywhere) => {
    if (!window.confirm(everywhere
      ? "Sign out EVERYWHERE, including this device? You will need to log back in."
      : "Sign out all other devices? This device stays signed in.")) return;
    setBusy(true);
    try {
      await api.post("/auth/sessions/revoke-all", { everywhere });
      if (everywhere) { nav("/login", { replace: true }); return; }
      toast.success("All other devices signed out");
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };
  return (
    <Card>
      <h3 className="font-semibold text-gray-100 mb-1">Active sessions</h3>
      <p className="text-sm text-gray-500 mb-4">
        If you ever lose a device or see unfamiliar activity, revoke every
        session at once.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button variant="destructive" onClick={() => revokeAll(false)} disabled={busy} data-testid="settings-revoke-all">
          <LogOut className="w-4 h-4 mr-2" />
          {busy ? "Working…" : "Sign out all other devices"}
        </Button>
        <Button variant="outline" onClick={() => revokeAll(true)} disabled={busy} data-testid="settings-revoke-everywhere">
          <LogOut className="w-4 h-4 mr-2" />
          Sign out everywhere (incl. this device)
        </Button>
      </div>
    </Card>
  );
}

export default function Settings() {
  return (
    <div className="animate-rise">
      <PageHeader
        title="Settings"
        subtitle="Workspace security and preferences"
        testid="settings-page"
      />
      <div className="space-y-4">
        <SecurityAlerts />
        <TotpSettings />
        <PasskeySettings />
        <TeamInvites />
        <InventoryCard />
        <SessionsCard />
      </div>
    </div>
  );
}
