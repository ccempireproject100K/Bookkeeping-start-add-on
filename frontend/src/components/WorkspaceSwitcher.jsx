import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { api, errMsg } from "@/lib/api";
import { toast } from "sonner";
import { Building2 } from "lucide-react";

/** Hop between workspaces (e.g. an accountant serving many clients).
 *  Only renders when the login can access more than one. */
export default function WorkspaceSwitcher() {
  const { user, switchWorkspace } = useAuth();
  const navigate = useNavigate();
  const [spaces, setSpaces] = useState([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get("/auth/workspaces").then(setSpaces).catch(() => setSpaces([]));
  }, [user?.tenant_id]);

  if (spaces.length < 2) return null;
  const current = user?.tenant_id;

  const hop = async (tid) => {
    if (tid === current) return;
    setBusy(true);
    try {
      await switchWorkspace(tid);
      const target = spaces.find((s) => s.tenant_id === tid);
      toast.success(`Switched to ${target?.tenant_name || "workspace"}`);
      navigate("/", { replace: true });
    } catch (e) { toast.error(errMsg(e)); } finally { setBusy(false); }
  };

  return (
    <div className="hidden md:flex items-center gap-2">
      <Building2 size={15} className="text-amber-400 shrink-0" />
      <Select value={current} onValueChange={hop} disabled={busy}>
        <SelectTrigger data-testid="workspace-switcher-select" className="w-[190px] h-9 bg-[#161b25] border-[#222838] text-sm">
          <SelectValue placeholder="Workspace" />
        </SelectTrigger>
        <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
          {spaces.map((s) => (
            <SelectItem key={s.tenant_id} value={s.tenant_id} data-testid={`workspace-option-${s.tenant_id}`}>
              {s.tenant_name} · {s.role}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
