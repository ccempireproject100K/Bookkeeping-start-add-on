import { useNavigate } from "react-router-dom";
import { Lock } from "lucide-react";
import { Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { usePlan, requiredPlanFor } from "@/hooks/usePlan";

/** Wraps a plan-gated feature. Renders children when the workspace's
 *  effective plan unlocks `feature`; otherwise shows an upgrade banner
 *  (no payment integration — billing lives outside this app).
 *  With `inline`, a locked feature renders a compact lock badge instead of
 *  the full banner — for header action slots.
 */
export default function PlanGate({ feature, children, inline }) {
  const { plan, loading, can, effectivePlanName } = usePlan();
  const navigate = useNavigate();
  if (loading || !plan) return children; // don't flash-lock while loading
  if (can(feature)) return children;

  const need = requiredPlanFor(plan, feature);
  const needName = plan.plans?.[need]?.name || "a higher plan";
  const label = plan.features_by_plan?.[need]?.[feature] || "This feature";
  if (inline) {
    return (
      <button
        className="inline-flex items-center gap-1.5 text-xs text-amber-300/90 border border-amber-500/30 rounded-md px-2.5 py-1.5 hover:bg-amber-500/10"
        title={`${label} needs ${needName} — you're on ${effectivePlanName}`}
        onClick={() => navigate("/settings")}
      >
        <Lock className="w-3.5 h-3.5" /> {needName}
      </button>
    );
  }
  return (
    <Card>
      <div className="flex items-start gap-3">
        <Lock className="w-5 h-5 text-amber-400 mt-0.5 shrink-0" />
        <div className="flex-1">
          <h3 className="font-semibold text-gray-100">Upgrade required</h3>
          <p className="text-sm text-gray-400 mt-1">
            {label} is available on <span className="text-gray-200 font-medium">{needName}</span> and
            up — this workspace is on <span className="text-gray-200">{effectivePlanName}</span>.
          </p>
          <p className="text-xs text-gray-500 mt-1">
            Billing is handled by your provider — upgrade there, then the feature unlocks here automatically.
          </p>
          <Button className="mt-3" size="sm" onClick={() => navigate("/settings")}>
            View plans in Settings
          </Button>
        </div>
      </div>
    </Card>
  );
}
