import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { errMsg } from "@/lib/api";
import { Scissors, ArrowRight, Fingerprint } from "lucide-react";
import { loginWithPasskey, passkeysSupported } from "@/lib/webauthn";
import { useTranslation } from "react-i18next";
const DEMOS = [{
  role: "Owner",
  email: "onelovellc@outlook.com",
  password: "Owner123!"
}, {
  role: "Accountant",
  email: "accountant@fadeco.com",
  password: "Books123!"
}, {
  role: "Bookkeeper",
  email: "bookkeeper@fadeco.com",
  password: "Books123!"
}, {
  role: "Technician",
  email: "marcus@fadeco.com",
  password: "Staff123!"
}];
export default function Login() {
  const {
    t
  } = useTranslation();
  const {
    user,
    login,
    refresh
  } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [totpNeeded, setTotpNeeded] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (user) navigate("/");
  }, [user, navigate]);
  const passkeyLogin = async () => {
    if (!email.trim()) {
      toast.error(t("login.enterYourEmailFirstThenUse"));
      return;
    }
    setBusy(true);
    try {
      await loginWithPasskey(email.trim());
      await refresh();
      toast.success(t("login.welcomeBack"));
      navigate("/");
    } catch (err) {
      toast.error(errMsg(err));
    } finally {
      setBusy(false);
    }
  };
  const submit = async (e, creds) => {
    e?.preventDefault?.();
    setBusy(true);
    try {
      await login(creds?.email || email, creds?.password || password, totpCode);
      toast.success(t("login.welcomeBack"));
      setTotpNeeded(false);
      setTotpCode("");
      navigate("/");
    } catch (err) {
      if (err?.response?.data?.detail === "TOTP_REQUIRED") {
        setTotpNeeded(true);
        toast.error(t("login.twoFactorCodeRequiredEnterThe"));
      } else {
        setTotpNeeded(false);
        toast.error(errMsg(err));
      }
    } finally {
      setBusy(false);
    }
  };
  return <div className="min-h-screen grid lg:grid-cols-2">
      {/* Brand side */}
      <div className="hidden lg:flex relative flex-col justify-between p-12 overflow-hidden">
        <img src="https://images.unsplash.com/photo-1698479603408-1a66a6d9e80f?crop=entropy&cs=srgb&fm=jpg&q=85&w=1400" alt={t("login.hvacEquipment")} className="absolute inset-0 w-full h-full object-cover opacity-30" />
        <div className="absolute inset-0 bg-gradient-to-br from-[#0B0D10] via-[#0B0D10]/85 to-[#0B0D10]/40" />
        <div className="relative flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg brand-mark" />
          <span className="font-display font-bold text-xl">{t("login.mindAtEaseSolutionsHVAC")}</span>
        </div>
        <div className="relative max-w-md">
          <h1 className="font-display text-4xl font-bold leading-tight mb-4">{t("login.knowYourProfitOn")} <span className="text-amber-500">{t("login.everyJob")}</span>.
          </h1>
          <p className="text-gray-400 leading-relaxed">{t("login.contractorJobCostingWiredStraightInto")}</p>
        </div>
        <div className="relative text-xs text-gray-500 font-mono">{t("login.revenueJobCostsGrossProfit")}</div>
      </div>

      {/* Form side */}
      <div className="flex items-center justify-center p-6 sm:p-10">
        <div className="w-full max-w-sm animate-rise">
          <div className="lg:hidden flex items-center gap-3 mb-8">
            <div className="w-9 h-9 rounded-lg brand-mark" />
            <span className="font-display font-bold text-lg">{t("login.mindAtEaseHVAC")}</span>
          </div>
          <h2 className="font-display text-2xl font-bold mb-1">{t("login.signIn")}</h2>
          <p className="text-sm text-gray-500 mb-6">{t("login.accessYourWorkspace")}</p>

          <form onSubmit={submit} className="space-y-4">
            <div>
              <Label className="text-xs text-gray-400">{t("login.email")}</Label>
              <Input data-testid="login-email" type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder={t("login.youShopCom")} className="mt-1 bg-[#14171E] border-[#222838]" />
            </div>
            <div>
              <Label className="text-xs text-gray-400">{t("login.password")}</Label>
              <Input data-testid="login-password" type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="••••••••" className="mt-1 bg-[#14171E] border-[#222838]" />
            </div>
            {totpNeeded && <div>
                <Label className="text-xs text-gray-400">{t("login.authenticatorCode")}</Label>
                <Input data-testid="login-totp" inputMode="numeric" value={totpCode} onChange={e => setTotpCode(e.target.value)} placeholder="123456" maxLength={8} autoFocus className="mt-1 bg-[#14171E] border-[#222838] font-mono tracking-widest" />
              </div>}
            <Button data-testid="login-submit" type="submit" disabled={busy} className="w-full bg-amber-600 hover:bg-amber-500 text-black font-semibold">
              {busy ? "Signing in…" : "Sign in"} <ArrowRight size={16} className="ml-1" />
            </Button>
          </form>

          {passkeysSupported() && <Button data-testid="login-passkey" onClick={passkeyLogin} disabled={busy} variant="outline" className="w-full mt-3 border-[#222838]">
              <Fingerprint size={16} className="mr-2" /> {t("login.signInWithPasskey")}</Button>}

          <div className="mt-8">
            <div className="text-[10px] uppercase tracking-widest text-gray-500 mb-3 flex items-center gap-2">
              <Scissors size={12} /> {t("login.demoLogins")}</div>
            <div className="grid grid-cols-2 gap-2">
              {DEMOS.map(d => <button key={d.role} data-testid={`demo-login-${d.role.toLowerCase()}`} onClick={e => submit(e, d)} disabled={busy} className="text-left px-3 py-2 rounded-lg bg-[#14171E] border border-[#222838] hover:border-amber-500/40 transition-colors">
                  <div className="text-sm font-medium text-gray-100">{d.role}</div>
                  <div className="text-[10px] text-gray-500 truncate">{d.email}</div>
                </button>)}
            </div>
          </div>
        </div>
      </div>
    </div>;
}
