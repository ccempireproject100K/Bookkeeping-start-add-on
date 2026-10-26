import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errMsg } from "@/lib/api";
import { toast } from "sonner";
import { CheckCircle2, XCircle, KeyRound } from "lucide-react";
import LanguageSwitcher from "@/components/LanguageSwitcher";
import { RESET_PASSWORD } from "@/constants/testIds";
import { useTranslation } from "react-i18next";

/** Public password reset: /reset-password?token=... — the emailed link.
 * The token is checked server-side (peek) before the form is shown, and
 * again when the new password is submitted; a dead link lands on the
 * invalid state, never on a form that cannot succeed. */
export default function ResetPassword() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const token = params.get("token") || "";
  const [state, setState] = useState("checking"); // checking | ready | invalid | done
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!token) {
      setState("invalid");
      return;
    }
    api.get(`/auth/password-reset/peek/${token}`)
      .then(() => setState("ready"))
      .catch(() => setState("invalid"));
  }, [token]);
  const submit = async (e) => {
    e.preventDefault();
    if (password.length < 12) {
      toast.error(t("passwordreset.tooShort"));
      return;
    }
    if (password !== confirm) {
      toast.error(t("passwordreset.mismatch"));
      return;
    }
    setBusy(true);
    try {
      await api.post("/auth/password-reset/confirm", { token, password });
      setState("done");
      toast.success(t("passwordreset.doneTitle"));
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
            <span className="font-display font-bold text-lg text-gray-50">{t("passwordreset.setNewTitle")}</span>
          </div>
          <LanguageSwitcher compact />
        </div>

        {state === "checking" && <p className="text-center text-gray-500 text-sm">{t("passwordreset.verifyingLink")}</p>}

        {state === "invalid" && <div className="rounded-xl border border-[#1b2230] bg-[#12151d] p-6 text-center" data-testid={RESET_PASSWORD.invalidMessage}>
            <XCircle className="mx-auto text-rose-400" size={28} />
            <h2 className="mt-2 font-semibold text-gray-100">{t("passwordreset.invalidTitle")}</h2>
            <p className="text-sm text-gray-500 mt-1">{t("passwordreset.invalidBody")}</p>
            <div className="mt-5 space-y-2">
              <div>
                <Link to="/forgot-password" data-testid={RESET_PASSWORD.requestLink} className="text-sm text-amber-500 hover:text-amber-400">{t("passwordreset.requestNewLink")}</Link>
              </div>
              <div>
                <Link to="/login" data-testid={RESET_PASSWORD.loginLink} className="text-xs text-gray-500 hover:text-amber-400">{t("passwordreset.backToSignIn")}</Link>
              </div>
            </div>
          </div>}

        {state === "done" && <div className="rounded-xl border border-[#1b2230] bg-[#12151d] p-6 text-center" data-testid={RESET_PASSWORD.doneMessage}>
            <CheckCircle2 className="mx-auto text-emerald-400" size={28} />
            <h2 className="mt-2 font-semibold text-gray-100">{t("passwordreset.doneTitle")}</h2>
            <p className="text-sm text-gray-500 mt-1">{t("passwordreset.doneBody")}</p>
            <Button data-testid={RESET_PASSWORD.loginLink} onClick={() => navigate("/login")} className="w-full mt-5 bg-amber-600 hover:bg-amber-500 text-black font-semibold">
              {t("passwordreset.continueToSignIn")}
            </Button>
          </div>}

        {state === "ready" && <div className="rounded-xl border border-[#1b2230] bg-[#12151d] p-6">
            <KeyRound className="text-amber-400" size={24} />
            <h2 className="mt-2 font-display text-xl font-bold text-gray-50">{t("passwordreset.setNewTitle")}</h2>
            <p className="text-sm text-gray-500 mt-1 mb-5">{t("passwordreset.setNewSubtitle")}</p>
            <form onSubmit={submit} className="space-y-4">
              <div>
                <Label className="text-xs text-gray-400">{t("passwordreset.newPassword")}</Label>
                <Input data-testid={RESET_PASSWORD.passwordInput} type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="••••••••" className="mt-1 bg-[#14171E] border-[#222838]" />
                <p className="text-[11px] text-gray-600 mt-1">{t("passwordreset.passwordHint")}</p>
              </div>
              <div>
                <Label className="text-xs text-gray-400">{t("passwordreset.confirmPassword")}</Label>
                <Input data-testid={RESET_PASSWORD.confirmInput} type="password" value={confirm} onChange={e => setConfirm(e.target.value)} placeholder="••••••••" className="mt-1 bg-[#14171E] border-[#222838]" />
              </div>
              <Button data-testid={RESET_PASSWORD.submitButton} type="submit" disabled={busy} className="w-full bg-amber-600 hover:bg-amber-500 text-black font-semibold">
                {busy ? t("passwordreset.updating") : t("passwordreset.updateButton")}
              </Button>
            </form>
          </div>}
      </div>
    </div>;
}
