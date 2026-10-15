import { createContext, useContext, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useAuth } from "./AuthContext";

const NicheCtx = createContext(null);
export const useNiche = () => useContext(NicheCtx);

/** Workspace niche pack (brand face, terminology, colors) from /edition.
 *  Refreshes when the workspace changes or the owner switches niche. */
export function NicheProvider({ children }) {
  const { user } = useAuth();
  const [pack, setPack] = useState(null);

  const refresh = () => {
    if (!user) { setPack(null); return Promise.resolve(); }
    return api.get("/edition").then(setPack).catch(() => setPack(null));
  };

  useEffect(() => { refresh(); }, [user?.tenant_id]);

  const terms = pack?.terms || {};
  const t = (key, fallback) => terms[key] || fallback;

  return (
    <NicheCtx.Provider value={{ pack, refresh, terms, t }}>
      {children}
    </NicheCtx.Provider>
  );
}
