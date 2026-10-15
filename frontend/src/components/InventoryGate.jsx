import { useNavigate } from "react-router-dom";
import { Package } from "lucide-react";
import { useAuth } from "@/context/AuthContext";

/** Route guard: with the inventory add-on off, the inventory page doesn't exist.
 *  Shows an explanatory empty state (instead of a silent redirect) with a path
 *  to enable it. Existing data is kept and comes back when it's turned on. */
import { useTranslation } from "react-i18next";
export default function InventoryGate({
  children
}) {
  const {
    t
  } = useTranslation();
  const {
    user
  } = useAuth();
  const nav = useNavigate();
  if (!user) return null;
  if (!user.tenant?.settings?.inventory_enabled) {
    return <div className="mx-auto max-w-lg px-4 py-16 text-center" data-testid="inventory-gate">
        <div className="rounded-xl border border-[#1b2230] bg-[#12151d] p-8">
          <Package className="mx-auto size-8 text-gray-500" />
          <h2 className="mt-3 text-lg font-semibold text-gray-100">{t("inventorygate.inventoryAddOnIsOff")}</h2>
          <p className="mx-auto mt-1 max-w-sm text-sm text-gray-400">{t("inventorygate.productsAndStockLevelsLiveHere")}</p>
          <button type="button" onClick={() => nav("/settings")} data-testid="inventory-gate-settings-button" className="mt-4 rounded-lg bg-amber-500 px-4 py-2 text-sm font-medium text-black transition-colors hover:bg-amber-400">{t("inventorygate.openSettings")}</button>
        </div>
      </div>;
  }
  return <>{children}</>;
}
