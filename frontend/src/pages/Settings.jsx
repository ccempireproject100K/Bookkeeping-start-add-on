import { useState } from "react";
import { PageHeader, Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { api, errMsg } from "@/lib/api";
import { toast } from "sonner";
import { LogOut } from "lucide-react";
import TotpSettings from "@/components/TotpSettings";

function SessionsCard() {
  const [busy, setBusy] = useState(false);
  const revokeAll = async () => {
    if (!window.confirm("Sign out all other devices? This device stays signed in.")) return;
    setBusy(true);
    try {
      await api.post("/auth/sessions/revoke-all");
      toast.success("All other devices signed out");
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };
  return (
    <Card>
      <h3 className="font-semibold text-gray-100 mb-1">Active sessions</h3>
      <p className="text-sm text-gray-500 mb-4">
        If you ever lose a device or see unfamiliar activity, revoke every
        session at once. This device stays signed in.
      </p>
      <Button variant="destructive" onClick={revokeAll} disabled={busy} data-testid="settings-revoke-all">
        <LogOut className="w-4 h-4 mr-2" />
        {busy ? "Working…" : "Sign out all other devices"}
      </Button>
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
        <TotpSettings />
        <SessionsCard />
      </div>
    </div>
  );
}
