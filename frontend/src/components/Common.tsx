import { cn } from "@/lib/utils";
import type { LotStatus, MovementType, POStatus, SerialStatus, StockStatus, Urgency, InvoiceStatus } from "@/lib/types";

type Tone = "success" | "warning" | "danger" | "info" | "neutral";

const TONES: Record<Tone, string> = {
  success: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 ring-emerald-500/25",
  warning: "bg-amber-500/10 text-amber-700 dark:text-amber-300 ring-amber-500/25",
  danger: "bg-red-500/10 text-red-600 dark:text-red-300 ring-red-500/25",
  info: "bg-blue-500/10 text-blue-600 dark:text-blue-300 ring-blue-500/25",
  neutral: "bg-slate-500/10 text-slate-600 dark:text-slate-300 ring-slate-500/25",
};

export function Pill({ tone, children, testId, dot }: { tone: Tone; children: React.ReactNode; testId?: string; dot?: boolean }) {
  return (
    <span data-testid={testId}
      className={cn("inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset whitespace-nowrap", TONES[tone])}>
      {dot && <span className="size-1.5 rounded-full bg-current animate-pulse-dot" />}
      {children}
    </span>
  );
}

const cap = (s: string) => (s[0].toUpperCase() + s.slice(1)).replace(/_/g, " ");

const STOCK: Record<StockStatus, [Tone, string]> = { in_stock: ["success", "In stock"], low: ["warning", "Low"], out: ["danger", "Out"] };
export const StockBadge = ({ status, testId }: { status: StockStatus; testId?: string }) => (
  <Pill tone={STOCK[status][0]} testId={testId} dot={status !== "in_stock"}>{STOCK[status][1]}</Pill>
);
const PO: Record<POStatus, Tone> = { draft: "neutral", ordered: "info", partial: "warning", received: "success", cancelled: "danger" };
export const POBadge = ({ status, testId }: { status: POStatus; testId?: string }) => <Pill tone={PO[status]} testId={testId}>{status === "partial" ? "Backorder" : cap(status)}</Pill>;
const URG: Record<Urgency, Tone> = { critical: "danger", high: "warning", medium: "info" };
export const UrgencyBadge = ({ urgency, testId }: { urgency: Urgency; testId?: string }) => (
  <Pill tone={URG[urgency]} testId={testId} dot={urgency === "critical"}>{cap(urgency)}</Pill>
);
const MV: Record<MovementType, Tone> = { restock: "success", sale: "info", use: "neutral", adjustment: "warning", return: "info" };
export const MovementBadge = ({ type, testId }: { type: MovementType; testId?: string }) => <Pill tone={MV[type]} testId={testId}>{cap(type)}</Pill>;
const LOT: Record<LotStatus, Tone> = { ok: "success", expiring: "warning", expired: "danger", empty: "neutral" };
export const LotBadge = ({ status, testId }: { status: LotStatus; testId?: string }) => (
  <Pill tone={LOT[status]} testId={testId} dot={status === "expiring" || status === "expired"}>{cap(status)}</Pill>
);
const SER: Record<SerialStatus, Tone> = { in_stock: "success", sold: "info", used: "neutral", damaged: "danger", returned: "warning" };
export const SerialBadge = ({ status, testId }: { status: SerialStatus; testId?: string }) => <Pill tone={SER[status]} testId={testId}>{cap(status)}</Pill>;
const INV: Record<InvoiceStatus, Tone> = { draft: "neutral", sent: "info", paid: "success", void: "danger" };
export const InvoiceBadge = ({ status, testId }: { status: InvoiceStatus; testId?: string }) => <Pill tone={INV[status]} testId={testId}>{cap(status)}</Pill>;

export function PageHeader({ title, subtitle, children, eyebrow }: { title: string; subtitle?: string; children?: React.ReactNode; eyebrow?: string }) {
  return (
    <div className="mb-6 flex flex-col gap-4 md:flex-row md:items-end md:justify-between animate-rise">
      <div>
        {eyebrow && <div className="label-caps mb-1.5">{eyebrow}</div>}
        <h1 data-testid="page-title" className="text-2xl md:text-3xl font-semibold">{title}</h1>
        {subtitle && <p className="mt-1 max-w-2xl text-sm text-muted-foreground">{subtitle}</p>}
      </div>
      {children && <div className="flex flex-wrap items-center gap-2">{children}</div>}
    </div>
  );
}

export function Panel({ className, children, ...rest }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn("rounded-lg border border-border bg-card transition-colors duration-150", className)} {...rest}>
      {children}
    </div>
  );
}

export function Stat({ label, value, sub, testId, tone }: { label: string; value: string; sub?: string; testId: string; tone?: string }) {
  return (
    <Panel className="p-4 md:p-5 animate-rise">
      <div className="label-caps">{label}</div>
      <div data-testid={testId} className={cn("mt-2 font-heading text-2xl md:text-3xl font-semibold tabular-nums tracking-tight", tone)}>{value}</div>
      {sub && <div className="mt-1 text-xs text-muted-foreground">{sub}</div>}
    </Panel>
  );
}

export function EmptyRow({ cols, text }: { cols: number; text: string }) {
  return (
    <tr>
      <td colSpan={cols} className="py-12 text-center text-sm text-muted-foreground">{text}</td>
    </tr>
  );
}

// Native select: fast on phones (OS picker), and trivially testable.
export function NativeSelect({ className, children, ...rest }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn("h-9 w-full rounded-md border border-input bg-background px-2.5 text-sm outline-none transition-[border-color,box-shadow] duration-150 focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/40", className)}
      {...rest}>
      {children}
    </select>
  );
}

export function Field({ label, children, hint, className }: { label: string; children: React.ReactNode; hint?: string; className?: string }) {
  return (
    <label className={cn("block space-y-1.5", className)}>
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      {children}
      {hint && <span className="block text-[11px] text-muted-foreground">{hint}</span>}
    </label>
  );
}
