import { useState } from "react";
import { useAuth } from "@/context/AuthContext";
import { api, errMsg } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card } from "@/components/Page";
import { toast } from "sonner";
import { ShieldCheck } from "lucide-react";

/**
 * Two-factor auth (TOTP) settings card.
 *
 * Mount inside any logged-in page — e.g. a future /settings route —
 * e.g. <TotpSettings />. No router/nav wiring here (App.js is frozen).
 *
 * Endpoints: POST /totp/setup -> {secret, otpauth_url},
 *            POST /totp/verify {code}, POST /totp/disable {password}.
 */
export default function TotpSettings() {
  const { user, refresh } = useAuth();
  const enabled = !!user?.totp_enabled;
  const [setup, setSetup] = useState(null); // {secret, otpauth_url}
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const startSetup = async () => {
    setBusy(true);
    try {
      const s = await api.post("/totp/setup", {});
      setSetup(s);
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    setBusy(true);
    try {
      await api.post("/totp/verify", { code });
      toast.success("Two-factor auth enabled");
      setSetup(null);
      setCode("");
      await refresh();
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    setBusy(true);
    try {
      await api.post("/totp/disable", { password });
      toast.success("Two-factor auth disabled");
      setPassword("");
      await refresh();
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="p-5">
      <div className="flex items-center gap-2 mb-1">
        <ShieldCheck size={16} className="text-amber-500" />
        <h3 className="font-semibold text-sm">Two-factor authentication</h3>
      </div>
      <p className="text-xs text-gray-500 mb-4">
        {enabled
          ? "Authenticator-app codes are required at sign-in."
          : "Add an authenticator-app code to your sign-in for extra security."}
      </p>

      {!enabled && !setup && (
        <Button data-testid="totp-start" onClick={startSetup} disabled={busy} variant="outline" size="sm">
          Enable two-factor auth
        </Button>
      )}

      {!enabled && setup && (
        <div className="space-y-3">
          <p className="text-xs text-gray-400">
            Enter this key manually in your authenticator app (Google Authenticator, Authy, 1Password, …):
          </p>
          <div className="font-mono text-sm tracking-widest bg-[#14171E] border border-[#222838] rounded-lg px-3 py-2 break-all select-all">
            {setup.secret}
          </div>
          <details className="text-xs text-gray-500">
            <summary className="cursor-pointer">Advanced: otpauth URI</summary>
            <div className="font-mono break-all mt-1 text-[11px]">{setup.otpauth_url}</div>
          </details>
          <div>
            <Label className="text-xs text-gray-400">6-digit code from the app</Label>
            <Input
              data-testid="totp-code"
              inputMode="numeric" value={code} onChange={(e) => setCode(e.target.value)}
              placeholder="123456" maxLength={8}
              className="mt-1 bg-[#14171E] border-[#222838] font-mono tracking-widest max-w-48"
            />
          </div>
          <div className="flex gap-2">
            <Button data-testid="totp-confirm" onClick={confirm} disabled={busy || code.trim().length < 6} size="sm">
              Verify & enable
            </Button>
            <Button onClick={() => setSetup(null)} variant="ghost" size="sm">Cancel</Button>
          </div>
        </div>
      )}

      {enabled && (
        <div className="space-y-3 max-w-64">
          <div>
            <Label className="text-xs text-gray-400">Confirm your password to disable</Label>
            <Input
              data-testid="totp-password"
              type="password" value={password} onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              className="mt-1 bg-[#14171E] border-[#222838]"
            />
          </div>
          <Button data-testid="totp-disable" onClick={disable} disabled={busy || !password} variant="destructive" size="sm">
            Disable two-factor auth
          </Button>
        </div>
      )}
    </Card>
  );
}
