import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { errMsg } from "@/lib/api";
import { Scissors, ArrowRight } from "lucide-react";

const DEMOS = [
  { role: "Owner", email: "onelovellc@outlook.com", password: "Owner123!" },
  { role: "Accountant", email: "accountant@fadeco.com", password: "Books123!" },
  { role: "Bookkeeper", email: "bookkeeper@fadeco.com", password: "Books123!" },
  { role: "Barber", email: "marcus@fadeco.com", password: "Barber123!" },
];

export default function Login() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => { if (user) navigate("/"); }, [user, navigate]);

  const submit = async (e, creds) => {
    e?.preventDefault?.();
    setBusy(true);
    try {
      await login(creds?.email || email, creds?.password || password);
      toast.success("Welcome back");
      navigate("/");
    } catch (err) {
      toast.error(errMsg(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen grid lg:grid-cols-2">
      {/* Brand side */}
      <div className="hidden lg:flex relative flex-col justify-between p-12 overflow-hidden">
        <img
          src="https://images.unsplash.com/photo-1585747860715-2ba37e788b70?crop=entropy&cs=srgb&fm=jpg&q=85&w=1400"
          alt="Barbershop"
          className="absolute inset-0 w-full h-full object-cover opacity-30"
        />
        <div className="absolute inset-0 bg-gradient-to-br from-[#0B0D10] via-[#0B0D10]/85 to-[#0B0D10]/40" />
        <div className="relative flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg barber-pole" />
          <span className="font-display font-bold text-xl">The Barber's Ledger</span>
        </div>
        <div className="relative max-w-md">
          <h1 className="font-display text-4xl font-bold leading-tight mb-4">
            Books that <span className="text-amber-500">balance</span> themselves.
          </h1>
          <p className="text-gray-400 leading-relaxed">
            True double-entry accounting built for barbershops — every cut, product, and
            payout posts a balanced journal entry. Trial balance to the cent, always.
          </p>
        </div>
        <div className="relative text-xs text-gray-500 font-mono">Assets = Liabilities + Equity</div>
      </div>

      {/* Form side */}
      <div className="flex items-center justify-center p-6 sm:p-10">
        <div className="w-full max-w-sm animate-rise">
          <div className="lg:hidden flex items-center gap-3 mb-8">
            <div className="w-9 h-9 rounded-lg barber-pole" />
            <span className="font-display font-bold text-lg">The Barber's Ledger</span>
          </div>
          <h2 className="font-display text-2xl font-bold mb-1">Sign in</h2>
          <p className="text-sm text-gray-500 mb-6">Access your workspace</p>

          <form onSubmit={submit} className="space-y-4">
            <div>
              <Label className="text-xs text-gray-400">Email</Label>
              <Input
                data-testid="login-email"
                type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                placeholder="you@shop.com"
                className="mt-1 bg-[#14171E] border-[#222838]"
              />
            </div>
            <div>
              <Label className="text-xs text-gray-400">Password</Label>
              <Input
                data-testid="login-password"
                type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                className="mt-1 bg-[#14171E] border-[#222838]"
              />
            </div>
            <Button
              data-testid="login-submit" type="submit" disabled={busy}
              className="w-full bg-amber-600 hover:bg-amber-500 text-black font-semibold"
            >
              {busy ? "Signing in…" : "Sign in"} <ArrowRight size={16} className="ml-1" />
            </Button>
          </form>

          <div className="mt-8">
            <div className="text-[10px] uppercase tracking-widest text-gray-500 mb-3 flex items-center gap-2">
              <Scissors size={12} /> Demo logins
            </div>
            <div className="grid grid-cols-2 gap-2">
              {DEMOS.map((d) => (
                <button
                  key={d.role}
                  data-testid={`demo-login-${d.role.toLowerCase()}`}
                  onClick={(e) => submit(e, d)}
                  disabled={busy}
                  className="text-left px-3 py-2 rounded-lg bg-[#14171E] border border-[#222838] hover:border-amber-500/40 transition-colors"
                >
                  <div className="text-sm font-medium text-gray-100">{d.role}</div>
                  <div className="text-[10px] text-gray-500 truncate">{d.email}</div>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
