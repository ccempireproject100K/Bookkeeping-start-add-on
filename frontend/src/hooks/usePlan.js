import { useEffect, useState } from "react";
import { api } from "@/lib/api";

/** Workspace plan info from /api/plans/current.
 *  can(feature) is the client-side mirror of the server's entitlement check —
 *  the server still enforces with 402, this only decides what to render.
 */
export function usePlan() {
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    api.get("/plans/current")
      .then(d => { if (alive) setPlan(d); })
      .catch(() => { /* not signed in or legacy tenant: treat as open */ })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);
  const can = (feature) => {
    if (!plan) return true; // optimistic while loading
    return !!plan.features?.[feature];
  };
  return {
    plan,
    loading,
    can,
    effectivePlan: plan?.effective_plan || "light",
    effectivePlanName: plan?.effective_plan_name || "Books Light",
    trialActive: !!plan?.trial_active,
    trialDays: plan?.trial_days_remaining || 0,
  };
}

/** Lowest plan whose feature map includes `feature`. */
export function requiredPlanFor(planInfo, feature) {
  if (!planInfo?.features_by_plan) return "medium";
  for (const p of ["light", "medium", "heavy"]) {
    if (planInfo.features_by_plan[p]?.[feature]) return p;
  }
  return "heavy";
}
