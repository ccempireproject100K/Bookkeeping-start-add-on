import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import FaceIcon from "@/components/FaceIcon";

/** Public index of all trade editions. Data-driven from the niche packs
 *  (GET /niches) — a new pack file on the backend is a new card here. */
export default function Industries() {
  const [packs, setPacks] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    document.title = "Industries — Mind at Ease Solutions";
    api.get("/niches").then(setPacks).catch(() => setErr("Could not load the industry list."));
  }, []);

  return (
    <div className="min-h-screen bg-[#0B0D10] text-gray-100">
      <div className="max-w-6xl mx-auto px-6 py-12">
        <div className="flex items-center justify-between mb-12">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg brand-mark" />
            <span className="font-display font-bold text-xl">Mind at Ease Solutions</span>
          </div>
          <Link to="/login" className="text-sm text-amber-500 hover:text-amber-400">
            Sign in <ArrowRight size={14} className="inline ml-1" />
          </Link>
        </div>

        <h1 className="font-display text-4xl font-bold leading-tight mb-4">
          Bookkeeping that speaks <span className="text-amber-500">your trade</span>.
        </h1>
        <p className="text-gray-400 leading-relaxed max-w-2xl mb-12">
          One double-entry platform, {packs ? packs.length : ""} trade editions.
          Pick your industry — the branding, terminology, chart of accounts and
          setup checklist all shape themselves around how your business talks.
        </p>

        {err && <p className="text-rose-400 text-sm">{err}</p>}
        {!packs && !err && <p className="text-gray-500 text-sm">Loading industries…</p>}

        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {(packs || []).map((p) => (
            <Link
              key={p.id}
              to={`/industries/${p.id}`}
              data-testid={`industry-card-${p.id}`}
              className="group rounded-xl bg-[#14171E] border border-[#222838] hover:border-amber-500/40 transition-colors p-5"
            >
              <div
                className="w-11 h-11 rounded-lg flex items-center justify-center mb-4"
                style={{ backgroundColor: `${p.colors?.accent || "#f59e0b"}22`, color: p.colors?.accent || "#f59e0b" }}
              >
                <FaceIcon name={p.icon} size={22} />
              </div>
              <div className="font-display font-semibold text-lg text-gray-100">{p.trade}</div>
              <div className="text-xs text-gray-500 mb-2">{p.product_name}</div>
              <p className="text-sm text-gray-400 leading-relaxed">{p.tagline}</p>
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}
