import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errMsg } from "@/lib/api";
import { toast } from "sonner";
import { UserPlus, CheckCircle2, XCircle } from "lucide-react";
import LanguageSwitcher from "@/components/LanguageSwitcher";

/** Public invite acceptance: /invite?token=... — join a workspace. */
import { useTranslation } from "react-i18next";
export default function InviteAccept() {
  const {
    t
  } = useTranslation();
  const {
    user,
    refresh
  } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const token = params.get("token") || "";
  const [peek, setPeek] = useState(null);
  const [failed, setFailed] = useState(false);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  useEffect(() => {
    if (!token) {
      setFailed(true);
      return;
    }
    api.get(`/invites/peek/${token}`).then(setPeek).catch(() => setFailed(true));
  }, [token]);
  const accept = async e => {
    e.preventDefault();
    if (!name.trim() || !user && !password) {
      toast.error("Enter your name" + (user ? "" : " and a password"));
      return;
    }
    setBusy(true);
    try {
      await api.post("/invites/accept", {
        token,
        name: name.trim(),
        password
      });
      await refresh();
      setDone(true);
      toast.success(t("inviteaccept.welcomeAboard"));
      setTimeout(() => navigate("/", {
        replace: true
      }), 1200);
    } catch (err) {
      toast.error(errMsg(err));
    } finally {
      setBusy(false);
    }
  };
  return <div className="min-h-screen flex items-center justify-center p-6 app-bg">
      <div className="w-full max-w-sm animate-rise">
        <div className="flex items-center justify-between mb-8">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg brand-mark" />
            <span className="font-display font-bold text-lg text-gray-50">{t("inviteaccept.workspaceInvite")}</span>
          </div>
          <LanguageSwitcher compact />
        </div>

        {failed && <div className="rounded-xl border border-[#1b2230] bg-[#12151d] p-6 text-center">
            <XCircle className="mx-auto text-rose-400" size={28} />
            <h2 className="mt-2 font-semibold text-gray-100">{t("inviteaccept.inviteNotAvailable")}</h2>
            <p className="text-sm text-gray-500 mt-1">{t("inviteaccept.thisLinkIsInvalidExpiredOr")}</p>
          </div>}

        {done && <div className="rounded-xl border border-[#1b2230] bg-[#12151d] p-6 text-center">
            <CheckCircle2 className="mx-auto text-emerald-400" size={28} />
            <h2 className="mt-2 font-semibold text-gray-100">{t("inviteaccept.youReIn")}</h2>
            <p className="text-sm text-gray-500 mt-1">{t("inviteaccept.takingYouToTheWorkspace")}</p>
          </div>}

        {!failed && !done && peek && <div className="rounded-xl border border-[#1b2230] bg-[#12151d] p-6">
            <UserPlus className="text-amber-400" size={24} />
            <h2 className="mt-2 font-display text-xl font-bold text-gray-50">{t("inviteaccept.join")} {peek.tenant_name}</h2>
            <p className="text-sm text-gray-500 mt-1 mb-5">{t("inviteaccept.youVeBeenInvitedAs")} <span className="text-gray-200 font-medium">{peek.role}</span>
              {user ? <> {t("inviteaccept.acceptingAdds")} <span className="text-gray-200 font-medium">{peek.tenant_name}</span> {t("inviteaccept.toYourExistingAccount")}{user.email}).</> : <> {t("inviteaccept.for")} <span className="text-gray-200 font-medium">{peek.email}</span>{t("inviteaccept.setYourNameAndPasswordTo")}</>}
            </p>
            <form onSubmit={accept} className="space-y-4">
              <div>
                <Label className="text-xs text-gray-400">{t("inviteaccept.yourName")}</Label>
                <Input value={name} onChange={e => setName(e.target.value)} placeholder={t("inviteaccept.jordanLee")} className="mt-1 bg-[#14171E] border-[#222838]" data-testid="invite-name" />
              </div>
              {!user && <div>
                  <Label className="text-xs text-gray-400">{t("inviteaccept.password")}</Label>
                  <Input type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="••••••••" className="mt-1 bg-[#14171E] border-[#222838]" data-testid="invite-password" />
                </div>}
              <Button type="submit" disabled={busy} className="w-full bg-amber-600 hover:bg-amber-500 text-black font-semibold" data-testid="invite-accept">
                {busy ? "Joining…" : `Join as ${peek.role}`}
              </Button>
            </form>
          </div>}

        {!failed && !done && !peek && <p className="text-center text-gray-500 text-sm">{t("inviteaccept.loadingInvite")}</p>}
      </div>
    </div>;
}
