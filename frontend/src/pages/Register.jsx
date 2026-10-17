import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { api, errMsg } from "@/lib/api";
import { asPackList } from "@/lib/packs";
import { asMarketList, readMarketChoice, writeMarketChoice } from "@/lib/markets";
import { ArrowRight } from "lucide-react";

const selectCls =
  "mt-1 w-full bg-[#14171E] border border-[#222838] rounded-md text-sm text-gray-100 px-3 py-2";

/** Public signup: creates the workspace via POST /auth/register.
 *  The niche picker (GET /niches) chooses the trade face; the market
 *  picker (GET /market-packs) chooses the market face — the workspace
 *  stores market_code, /edition composes the two, and the books' base
 *  currency is seeded from the market's currency at creation. The form
 *  preselects the US market (or the visitor's earlier public-pages
 *  choice); what is submitted is what gets stored. */
export default function Register() {
  const { user, register } = useAuth();
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: "", shop_name: "", email: "", password: "" });
  const [nicheId, setNicheId] = useState("");
  const [marketCode, setMarketCode] = useState(() => readMarketChoice() || "US");
  const [niches, setNiches] = useState([]);
  const [markets, setMarkets] = useState([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => { document.title = "Create your workspace — Mind at Ease Solutions"; }, []);
  useEffect(() => { if (user) navigate("/"); }, [user, navigate]);

  useEffect(() => {
    api.get("/niches").then((d) => setNiches(asPackList(d) || [])).catch(() => {});
    api.get("/market-packs").then((d) => setMarkets(asMarketList(d) || [])).catch(() => {});
  }, []);

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await register({
        ...form,
        niche_id: nicheId || null,
        market_code: marketCode || null,
      });
      writeMarketChoice(marketCode);
      toast.success("Welcome to Mind at Ease Solutions");
      navigate("/");
    } catch (err) {
      toast.error(errMsg(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#0B0D10] text-gray-100 flex items-center justify-center p-6">
      <div className="w-full max-w-md animate-rise">
        <div className="flex items-center gap-3 mb-8">
          <div className="w-10 h-10 rounded-lg brand-mark" />
          <span className="font-display font-bold text-xl">Mind at Ease Solutions</span>
        </div>
        <h1 className="font-display text-2xl font-bold mb-1">Create your workspace</h1>
        <p className="text-sm text-gray-500 mb-6">
          Bookkeeping with your trade's face and your market's skin.
        </p>

        <form onSubmit={submit} className="space-y-4">
          <div>
            <Label className="text-xs text-gray-400">Your name</Label>
            <Input data-testid="register-name-input" value={form.name} onChange={set("name")} required
              className="mt-1 bg-[#14171E] border-[#222838]" />
          </div>
          <div>
            <Label className="text-xs text-gray-400">Business name</Label>
            <Input data-testid="register-shop-name-input" value={form.shop_name} onChange={set("shop_name")} required
              className="mt-1 bg-[#14171E] border-[#222838]" />
          </div>
          <div>
            <Label className="text-xs text-gray-400">Email</Label>
            <Input data-testid="register-email-input" type="email" value={form.email} onChange={set("email")} required
              className="mt-1 bg-[#14171E] border-[#222838]" />
          </div>
          <div>
            <Label className="text-xs text-gray-400">Password</Label>
            <Input data-testid="register-password-input" type="password" value={form.password} onChange={set("password")} required
              className="mt-1 bg-[#14171E] border-[#222838]" />
            <p className="text-[11px] text-gray-600 mt-1">12 characters or more.</p>
          </div>
          <div>
            <Label className="text-xs text-gray-400">Your trade</Label>
            <select data-testid="register-niche-select" value={nicheId}
              onChange={(e) => setNicheId(e.target.value)} className={selectCls}>
              <option value="">General bookkeeping (no trade face)</option>
              {niches.map((n) => (
                <option key={n.id} value={n.id}>{n.trade} — {n.product_name}</option>
              ))}
            </select>
          </div>
          <div>
            <Label className="text-xs text-gray-400">Your market</Label>
            <select data-testid="register-market-select" value={marketCode}
              onChange={(e) => setMarketCode(e.target.value)} className={selectCls}>
              <option value="">No market (trade face only)</option>
              {markets.map((m) => (
                <option key={m.code} value={m.code}>
                  {m.flag} {m.name} — {m.currency}
                </option>
              ))}
            </select>
            <p className="text-[11px] text-gray-600 mt-1">
              Sets your edition's accent, language and home currency.
            </p>
          </div>
          <Button data-testid="register-submit-button" type="submit" disabled={busy}
            className="w-full bg-amber-600 hover:bg-amber-500 text-black font-semibold">
            {busy ? "Creating…" : "Create workspace"} <ArrowRight size={16} className="ml-1" />
          </Button>
        </form>

        <div className="mt-8 text-center text-sm text-gray-500">
          Already have a workspace?{" "}
          <Link to="/login" className="text-amber-500 hover:text-amber-400">Sign in</Link>
        </div>
      </div>
    </div>
  );
}
