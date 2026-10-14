import { Input } from "@/components/ui/input";
export default function InternationalFields({ value, onChange, disabled = false, currencyLocked = false }: { value: { currency?: string; timezone?: string; locale?: string }; onChange: (key: "currency" | "timezone" | "locale", value: string) => void; disabled?: boolean; currencyLocked?: boolean }) {
  return <div className="space-y-3">
    <label className="block text-sm">Currency<select className="block w-full rounded border p-2 bg-background" value={value.currency ?? "USD"} disabled={disabled || currencyLocked} onChange={e => onChange("currency", e.target.value)}>{["USD", "CAD", "GBP", "EUR", "AUD", "NZD", "SGD"].map(c => <option key={c}>{c}</option>)}</select></label>
    <p className="text-xs text-muted-foreground">Choose currency when creating the workspace. It stays fixed to protect your records.</p>
    <label className="block text-sm">Business time zone<Input disabled={disabled} value={value.timezone ?? "UTC"} onChange={e => onChange("timezone", e.target.value)} placeholder="America/Chicago" /></label>
    <label className="block text-sm">Number and date format<select className="block w-full rounded border p-2 bg-background" disabled={disabled} value={value.locale ?? "en-US"} onChange={e => onChange("locale", e.target.value)}>{["en-US", "en-CA", "en-GB", "en-AU", "en-IE", "en-NZ", "en-SG"].map(c => <option key={c}>{c}</option>)}</select></label>
  </div>;
}
