import { Input } from "@/components/ui/input";
import { useTranslation } from "react-i18next";
export default function InternationalFields({
  value,
  onChange,
  disabled = false,
  currencyLocked = false
}: {
  value: {
    currency?: string;
    timezone?: string;
    locale?: string;
  };
  onChange: (key: "currency" | "timezone" | "locale", value: string) => void;
  disabled?: boolean;
  currencyLocked?: boolean;
}) {
  const {
    t
  } = useTranslation();
  return <div className="space-y-3">
    <label className="block text-sm">{t("internationalfields.currency")}<select className="block w-full rounded border p-2 bg-background" value={value.currency ?? "USD"} disabled={disabled || currencyLocked} onChange={e => onChange("currency", e.target.value)}>{["USD", "CAD", "GBP", "EUR", "AUD", "NZD", "SGD"].map(c => <option key={c}>{c}</option>)}</select></label>
    <p className="text-xs text-muted-foreground">{t("internationalfields.chooseCurrencyWhenCreatingTheWorkspace")}</p>
    <label className="block text-sm">{t("internationalfields.businessTimeZone")}<Input disabled={disabled} value={value.timezone ?? "UTC"} onChange={e => onChange("timezone", e.target.value)} placeholder={t("internationalfields.americaChicago")} /></label>
    <label className="block text-sm">{t("internationalfields.numberAndDateFormat")}<select className="block w-full rounded border p-2 bg-background" disabled={disabled} value={value.locale ?? "en-US"} onChange={e => onChange("locale", e.target.value)}>{["en-US", "en-CA", "en-GB", "en-AU", "en-IE", "en-NZ", "en-SG"].map(c => <option key={c}>{c}</option>)}</select></label>
  </div>;
}
