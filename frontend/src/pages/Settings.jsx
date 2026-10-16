import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageHeader, Card } from "@/components/Page";
import { Button } from "@/components/ui/button";
import { api, errMsg } from "@/lib/api";
import { toast } from "sonner";
import { LogOut, Package } from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import TotpSettings from "@/components/TotpSettings";
import PasskeySettings from "@/components/PasskeySettings";
import SecurityAlerts from "@/components/SecurityAlerts";
import TeamInvites from "@/components/TeamInvites";
import NicheSwitcher from "@/components/NicheSwitcher";
import OpeningBalances from "@/components/OpeningBalances";
import BrandingCard from "@/components/BrandingCard";
import CurrencyPicker from "@/components/CurrencyPicker";
import { usePlan } from "@/hooks/usePlan";
import { Globe, Crown } from "lucide-react";
import { useTranslation } from "react-i18next";
import LanguageSwitcher from "@/components/LanguageSwitcher";
function BaseCurrencyCard() {
  const {
    t
  } = useTranslation();
  const {
    user,
    refresh
  } = useAuth();
  const [busy, setBusy] = useState(false);
  if (user?.role !== "owner") return null;
  const current = user?.tenant?.settings?.base_currency || user?.tenant?.settings?.currency || "USD";
  const save = async code => {
    setBusy(true);
    try {
      await api.patch("/auth/tenant/settings", {
        base_currency: code
      });
      await refresh();
      toast.success(`Home currency set to ${code} — reports translate into it`);
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  return <Card>
      <h3 className="font-semibold text-gray-100 mb-1 flex items-center gap-2">
        <Globe className="w-4 h-4" /> {t("settings.homeCurrency")}</h3>
      <p className="text-sm text-gray-500 mb-4">{t("settings.reportsTranslateForeignCurrencyActivityInto")} <span className="font-mono text-gray-300">{current}</span>.
      </p>
      <div className="max-w-xs opacity-100">
        <CurrencyPicker value={current} onChange={save} testid="settings-base-currency" label="" />
      </div>
      {busy && <p className="text-xs text-gray-500 mt-2">{t("settings.saving")}</p>}
    </Card>;
}
function InventoryCard() {
  const {
    t
  } = useTranslation();
  const {
    user,
    refresh
  } = useAuth();
  const [busy, setBusy] = useState(false);
  if (user?.role !== "owner") return null;
  const enabled = !!user?.tenant?.settings?.inventory_enabled;
  const toggle = async () => {
    setBusy(true);
    try {
      await api.patch("/auth/tenant/settings", {
        inventory_enabled: !enabled
      });
      await refresh();
      toast.success(enabled ? "Inventory add-on turned off" : "Inventory add-on turned on");
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  return <Card>
      <h3 className="font-semibold text-gray-100 mb-1 flex items-center gap-2">
        <Package className="w-4 h-4" /> {t("settings.inventoryAddOn")}</h3>
      <p className="text-sm text-gray-500 mb-4">
        {enabled ? "Inventory tracking is on — products and stock levels are available." : "Inventory tracking is off — the Inventory page shows an explanatory message instead. Your data is kept and comes back when you turn it on."}
      </p>
      <Button variant={enabled ? "destructive" : "default"} onClick={toggle} disabled={busy} data-testid="settings-inventory-toggle">
        {busy ? "Working…" : enabled ? "Turn inventory off" : "Turn inventory on"}
      </Button>
    </Card>;
}
function SessionsCard() {
  const {
    t
  } = useTranslation();
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  const revokeAll = async everywhere => {
    if (!window.confirm(everywhere ? "Sign out EVERYWHERE, including this device? You will need to log back in." : "Sign out all other devices? This device stays signed in.")) return;
    setBusy(true);
    try {
      await api.post("/auth/sessions/revoke-all", {
        everywhere
      });
      if (everywhere) {
        nav("/login", {
          replace: true
        });
        return;
      }
      toast.success(t("settings.allOtherDevicesSignedOut"));
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  return <Card>
      <h3 className="font-semibold text-gray-100 mb-1">{t("settings.activeSessions")}</h3>
      <p className="text-sm text-gray-500 mb-4">{t("settings.ifYouEverLoseADevice")}</p>
      <div className="flex flex-wrap gap-2">
        <Button variant="destructive" onClick={() => revokeAll(false)} disabled={busy} data-testid="settings-revoke-all">
          <LogOut className="w-4 h-4 mr-2" />
          {busy ? "Working…" : "Sign out all other devices"}
        </Button>
        <Button variant="outline" onClick={() => revokeAll(true)} disabled={busy} data-testid="settings-revoke-everywhere">
          <LogOut className="w-4 h-4 mr-2" />{t("settings.signOutEverywhereInclThisDevice")}</Button>
      </div>
    </Card>;
}
function LanguageCard() {
  const { t } = useTranslation();
  return <Card>
    <h3 className="font-semibold mb-1">{t("settings.language")}</h3>
    <p className="text-sm text-gray-500 mb-3">{t("settings.languageDesc")}</p>
    <LanguageSwitcher />
  </Card>;
}
function PlanCard() {
  // Current plan, trial countdown, and unlocked features. Plain-English copy
  // (not i18n keys) — billing copy changes faster than translations.
  const { user } = useAuth();
  const { plan, loading, effectivePlanName, trialActive, trialDays } = usePlan();
  const [busy, setBusy] = useState(false);
  if (loading || !plan) return null;
  const isOwner = user?.role === "owner";
  const change = async (p) => {
    setBusy(true);
    try {
      await api.post("/plans/change", { plan: p });
      toast.success(`Plan set to ${plan.plans[p].name}. Billing is handled by your provider.`);
      window.location.reload();
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  return <Card>
    <h3 className="font-semibold text-gray-100 mb-1 flex items-center gap-2">
      <Crown className="w-4 h-4" /> Plan — {effectivePlanName}
    </h3>
    {trialActive && (
      <p className="text-sm text-amber-300 mb-2">
        Trial: {trialDays} day{trialDays === 1 ? "" : "s"} left of full Medium features — then this
        workspace moves to {plan.plans[plan.plan]?.name || "its plan"}.
      </p>
    )}
    <p className="text-sm text-gray-500 mb-3">
      Unlocked on this workspace: {Object.values(plan.features || {}).join(", ")}.
    </p>
    {isOwner && (
      <div className="flex items-center gap-2">
        <span className="text-xs text-gray-500">Set plan (recorded entitlement; charges happen in billing):</span>
        {["light", "medium", "heavy"].map(p => (
          <Button key={p} size="sm" variant={plan.plan === p ? "default" : "outline"}
                  disabled={busy || plan.plan === p} onClick={() => change(p)}>
            {plan.plans[p].name}
          </Button>
        ))}
      </div>
    )}
  </Card>;
}
export default function Settings() {
  const {
    t
  } = useTranslation();
  return <div className="animate-rise">
      <PageHeader title={t("settings.settings")} subtitle="Workspace security and preferences" testid="settings-page" />
      <div className="space-y-4">
        <PlanCard />
        <SecurityAlerts />
        <TotpSettings />
        <PasskeySettings />
        <TeamInvites />
        <BrandingCard />
        <NicheSwitcher />
        <BaseCurrencyCard />
        <LanguageCard />
        <OpeningBalances />
        <InventoryCard />
        <SessionsCard />
      </div>
    </div>;
}
