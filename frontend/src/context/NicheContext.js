import { createContext, useContext, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { asFace } from "@/lib/packs";
import { useAuth } from "./AuthContext";

const NicheCtx = createContext(null);
export const useNiche = () => useContext(NicheCtx);

/** Workspace niche pack (brand face, terminology, colors) from /edition.
 *  Refreshes when the workspace changes or the owner switches niche.
 *  When the workspace has a market, /edition composes the face
 *  (market accent in `colors`, market detail in `market`); the market
 *  accent + flag are surfaced here so any consumer can apply them. */
export function NicheProvider({ children }) {
  const { user } = useAuth();
  const [pack, setPack] = useState(null);

  const refresh = () => {
    if (!user) { setPack(null); return Promise.resolve(); }
    return api.get("/edition").then((d) => {
      const face = asFace(d);
      // Belt-and-braces: if the payload carries a market accent, it is
      // the skin's accent (the backend already composes `colors`, this
      // keeps the invariant if a partial payload ever slips through).
      if (face?.market?.accent) {
        face.colors = { ...(face.colors || {}), accent: face.market.accent };
      }
      setPack(face);
    }).catch(() => setPack(null));
  };

  useEffect(() => { refresh(); }, [user?.tenant_id]);

  const terms = pack?.terms || {};
  const t = (key, fallback) => terms[key] || fallback;
  const market = pack?.market || null;

  return (
    <NicheCtx.Provider value={{ pack, refresh, terms, t, market, flag: market?.flag || null }}>
      {children}
    </NicheCtx.Provider>
  );
}
