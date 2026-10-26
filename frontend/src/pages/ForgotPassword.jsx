import { useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errMsg } from "@/lib/api";
import { toast } from "sonner";
import { MailCheck, KeyRound } from "lucide-react";
import LanguageSwitcher from "@/components/LanguageSwitcher";
import { PASSWORD_RESET } from "@/constants/testIds";
import { useTranslation } from "react-i18next";

/** Public password-reset request: /forgot-password.
 * The backend answers uniformly whether or not the email has an
 * account, so the "check your inbox" state below is the only state a
 * successful submission ever shows — it confirms nothing about the
 * address. */
export default function ForgotPassword() {
  const { t } = useTranslation();
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    if (!email.trim()) {
      toast.error(t("passwordreset.enterEmail"));
      return;
    }
    setBusy(true);
    try {
      await api.post("/auth/password-reset/request", { email: email.trim() });
      setSent(true);
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
            <span className="font-display font-bold text-lg text-gray-50">{t("passwordreset.title")}</span>
          </div>
          <LanguageSwitcher compact />
        </div>

        {sent && <div className="rounded-xl border border-[#1b2230] bg-[#12151d] p-6 text-center" data-testid={PASSWORD_RESET.sentMessage}>
            <MailCheck className="mx-auto text-emerald-400" size={28} />
            <h2 className="mt-2 font-semibold text-gray-100">{t("passwordreset.checkInboxTitle")}</h2>
            <p className="text-sm text-gray-500 mt-1">{t("passwordreset.checkInboxBody")}</p>
            <div className="mt-5">
              <Link to="/login" data-testid={PASSWORD_RESET.loginLink} className="text-sm text-amber-500 hover:text-amber-400">{t("passwordreset.backToSignIn")}</Link>
            </div>
          </div>}

        {!sent && <div className="rounded-xl border border-[#1b2230] bg-[#12151d] p-6">
            <KeyRound className="text-amber-400" size={24} />
            <h2 className="mt-2 font-display text-xl font-bold text-gray-50">{t("passwordreset.title")}</h2>
            <p className="text-sm text-gray-500 mt-1 mb-5">{t("passwordreset.subtitle")}</p>
            <form onSubmit={submit} className="space-y-4">
              <div>
                <Label className="text-xs text-gray-400">{t("passwordreset.email")}</Label>
                <Input data-testid={PASSWORD_RESET.emailInput} type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder={t("login.youShopCom")} className="mt-1 bg-[#14171E] border-[#222838]" />
              </div>
              <Button data-testid={PASSWORD_RESET.submitButton} type="submit" disabled={busy} className="w-full bg-amber-600 hover:bg-amber-500 text-black font-semibold">
                {busy ? t("passwordreset.sending") : t("passwordreset.sendLink")}
              </Button>
            </form>
            <div className="mt-5 text-center">
              <Link to="/login" data-testid={PASSWORD_RESET.loginLink} className="text-sm text-amber-500 hover:text-amber-400">{t("passwordreset.backToSignIn")}</Link>
            </div>
          </div>}
      </div>
    </div>;
}
