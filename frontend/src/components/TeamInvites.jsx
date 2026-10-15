import { useEffect, useState } from "react";
import { Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, errMsg } from "@/lib/api";
import { toast } from "sonner";
import { UserPlus, Copy, X, Trash2 } from "lucide-react";
import { useAuth } from "@/context/AuthContext";

const ROLES = [
  { value: "accountant", label: "Accountant / Tax pro" },
  { value: "bookkeeper", label: "Bookkeeper" },
  { value: "barber", label: "Technician" },
];

/** Owner-only: invite accountants/bookkeepers, manage pending invites + team. */
export default function TeamInvites() {
  const { user } = useAuth();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("accountant");
  const [invites, setInvites] = useState([]);
  const [team, setTeam] = useState([]);
  const [busy, setBusy] = useState(false);
  const isOwner = user?.role === "owner";

  const load = async () => {
    try {
      const [i, t] = await Promise.all([api.get("/invites"), api.get("/auth/team")]);
      setInvites(i || []);
      setTeam((t || []).filter((m) => m.email !== user.email));
    } catch { /* team unavailable */ }
  };
  useEffect(() => { if (isOwner) load(); }, [isOwner]);

  const invite = async () => {
    if (!email.trim()) { toast.error("Enter an email address"); return; }
    setBusy(true);
    try {
      const inv = await api.post("/invites", { email: email.trim(), role });
      setEmail("");
      toast.success(`Invite created for ${inv.email}`);
      load();
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };

  const copyLink = async (url) => {
    try { await navigator.clipboard.writeText(url); toast.success("Invite link copied — send it to them"); }
    catch { toast.error("Couldn't copy — long-press the link instead"); }
  };

  const revoke = async (id) => {
    try { await api.post(`/invites/${id}/revoke`, {}); toast.success("Invite revoked"); load(); }
    catch (e) { toast.error(errMsg(e)); }
  };

  const remove = async (memberEmail) => {
    if (!window.confirm(`Remove ${memberEmail} from this workspace? They'll lose access immediately.`)) return;
    try {
      await api.del(`/invites/members/${encodeURIComponent(memberEmail)}`);
      toast.success("Team member removed");
      load();
    } catch (e) { toast.error(errMsg(e)); }
  };

  const pending = invites.filter((i) => i.status === "pending");

  if (!isOwner) return null;

  return (
    <Card>
      <h3 className="font-semibold text-gray-100 mb-1 flex items-center gap-2">
        <UserPlus className="w-4 h-4" /> Team & accountant access
      </h3>
      <p className="text-sm text-gray-500 mb-4">
        Invite your accountant or tax pro — they join with the right permissions
        and can switch between all the workspaces they serve.
      </p>

      <div className="flex flex-col sm:flex-row gap-2 mb-4">
        <Input
          type="email" value={email} onChange={(e) => setEmail(e.target.value)}
          placeholder="cpa@firm.com" className="bg-[#14171E] border-[#222838] flex-1"
          data-testid="invite-email"
        />
        <select
          value={role} onChange={(e) => setRole(e.target.value)}
          className="bg-[#14171E] border border-[#222838] rounded-lg px-3 h-10 text-sm text-gray-100"
          data-testid="invite-role"
        >
          {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
        </select>
        <Button onClick={invite} disabled={busy} data-testid="invite-send">
          {busy ? "Sending…" : "Send invite"}
        </Button>
      </div>

      {pending.length > 0 && (
        <div className="mb-4">
          <div className="text-xs uppercase tracking-widest text-gray-500 mb-2">Pending invites</div>
          <ul className="space-y-2">
            {pending.map((i) => (
              <li key={i.id} className="flex items-center justify-between gap-2 rounded-lg bg-[#14171E] border border-[#222838] px-3 py-2">
                <div className="min-w-0">
                  <div className="text-sm text-gray-100 truncate">{i.email}</div>
                  <div className="text-[11px] text-gray-500">as {i.role}</div>
                </div>
                <div className="flex gap-1 shrink-0">
                  <Button variant="ghost" size="sm" onClick={() => copyLink(i.accept_url)} title="Copy invite link" data-testid={`invite-copy-${i.id}`}>
                    <Copy size={15} />
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => revoke(i.id)} title="Revoke invite" data-testid={`invite-revoke-${i.id}`}>
                    <X size={15} />
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {team.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-widest text-gray-500 mb-2">Team</div>
          <ul className="space-y-2">
            {team.map((m) => (
              <li key={m.id || m.email} className="flex items-center justify-between gap-2 rounded-lg bg-[#14171E] border border-[#222838] px-3 py-2">
                <div className="min-w-0">
                  <div className="text-sm text-gray-100 truncate">{m.name || m.email}</div>
                  <div className="text-[11px] text-gray-500">{m.email} · {m.role}</div>
                </div>
                <button
                  type="button" onClick={() => remove(m.email)}
                  className="text-gray-500 hover:text-rose-400 transition-colors shrink-0"
                  title="Remove from workspace" data-testid={`member-remove-${m.email}`}
                >
                  <Trash2 size={16} />
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
