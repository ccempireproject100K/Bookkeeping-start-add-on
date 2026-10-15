import { useEffect, useState } from "react";
import { Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, errMsg } from "@/lib/api";
import { toast } from "sonner";
import { Fingerprint, Trash2 } from "lucide-react";
import { passkeysSupported, registerPasskey } from "@/lib/webauthn";

/** Manage passkeys: register a new one, list and remove existing ones. */
export default function PasskeySettings() {
  const [keys, setKeys] = useState([]);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      const r = await api.get("/webauthn/credentials");
      setKeys(r.credentials || []);
    } catch { /* passkeys unavailable */ }
  };
  useEffect(() => { load(); }, []);

  const add = async () => {
    setBusy(true);
    try {
      await registerPasskey(name.trim());
      setName("");
      toast.success("Passkey added — you can now sign in without a password");
      load();
    } catch (e) { toast.error(e.message || errMsg(e)); } finally { setBusy(false); }
  };

  const remove = async (id, label) => {
    if (!window.confirm(`Remove passkey "${label}"?`)) return;
    try {
      await api.del(`/webauthn/credentials/${id}`);
      toast.success("Passkey removed");
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  return (
    <Card>
      <h3 className="font-semibold text-gray-100 mb-1 flex items-center gap-2">
        <Fingerprint className="w-4 h-4" /> Passkeys
      </h3>
      <p className="text-sm text-gray-500 mb-4">
        Sign in with your fingerprint, face, or a hardware key — phishing-proof
        and faster than a password. Works alongside your existing sign-in methods.
      </p>
      {!passkeysSupported() && (
        <p className="text-sm text-amber-400 mb-3">
          This browser doesn't support passkeys. Try Chrome, Edge, or Safari.
        </p>
      )}
      {keys.length > 0 && (
        <ul className="mb-4 space-y-2">
          {keys.map((k) => (
            <li key={k.id} className="flex items-center justify-between rounded-lg bg-[#14171E] border border-[#222838] px-3 py-2">
              <div>
                <div className="text-sm text-gray-100">{k.name}</div>
                <div className="text-[11px] text-gray-500">
                  Added {k.created_at ? new Date(k.created_at).toLocaleDateString() : "—"}
                </div>
              </div>
              <button
                type="button" onClick={() => remove(k.id, k.name)}
                className="text-gray-500 hover:text-rose-400 transition-colors"
                title="Remove passkey" data-testid={`passkey-remove-${k.id}`}
              >
                <Trash2 size={16} />
              </button>
            </li>
          ))}
        </ul>
      )}
      {passkeysSupported() && (
        <div className="flex gap-2">
          <Input
            value={name} onChange={(e) => setName(e.target.value)}
            placeholder="Label e.g. MacBook Touch ID"
            className="bg-[#14171E] border-[#222838]"
            data-testid="passkey-name"
          />
          <Button onClick={add} disabled={busy} data-testid="passkey-add">
            {busy ? "Follow the prompt…" : "Add passkey"}
          </Button>
        </div>
      )}
    </Card>
  );
}
