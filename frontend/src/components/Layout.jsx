import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  LayoutDashboard, BookOpen, FileText, Scale, ScrollText, Landmark,
  TrendingUp, Lock, Scissors, Receipt, Wallet, Package, LogOut, ShieldCheck,
  Hourglass, Gift, Coins, Briefcase, PieChart, Users, Building2, FileSignature,
  Waves, Target, BarChart3, ReceiptText, Repeat, DatabaseBackup, Bot, Clock,
  FileCheck2, Camera, Car, HandCoins, Calculator, Activity, Tag, SlidersHorizontal, Rocket, Gauge, Mic, Layers,
} from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import { errMsg } from "@/lib/api";
import MusicPlayer from "./MusicPlayer";
import WorkspaceSwitcher from "./WorkspaceSwitcher";
import { useNiche } from "@/context/NicheContext";

const ALL = ["owner", "accountant", "bookkeeper"];
const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, testid: "nav-dashboard", roles: ["owner", "accountant", "bookkeeper", "barber"], group: "Overview" },
  { to: "/jobs", label: "Jobs", icon: Briefcase, testid: "nav-jobs", roles: ALL, group: "Job Costing" },
  { to: "/job-profitability", label: "Job Profitability", icon: PieChart, testid: "nav-job-profitability", roles: ALL, group: "Job Costing" },
  { to: "/customers", label: "Customers", icon: Users, testid: "nav-customers", roles: ALL, group: "Job Costing" },
  { to: "/estimates", label: "Estimates", icon: FileSignature, testid: "nav-estimates", roles: ALL, group: "Job Costing" },
  { to: "/accounts", label: "Chart of Accounts", icon: BookOpen, testid: "nav-chart-of-accounts", roles: ALL, group: "Ledger" },
  { to: "/journal", label: "Journal Entries", icon: FileText, testid: "nav-journal-entries", roles: ALL, group: "Ledger" },
  { to: "/general-ledger", label: "General Ledger", icon: ScrollText, testid: "nav-general-ledger", roles: ALL, group: "Ledger" },
  { to: "/banking", label: "Banking", icon: Building2, testid: "nav-banking", roles: ALL, group: "Banking" },
  { to: "/trial-balance", label: "Trial Balance", icon: Scale, testid: "nav-trial-balance", roles: ALL, group: "Reports" },
  { to: "/balance-sheet", label: "Balance Sheet", icon: Landmark, testid: "nav-balance-sheet", roles: ALL, group: "Reports" },
  { to: "/profit-loss", label: "Profit & Loss", icon: TrendingUp, testid: "nav-profit-loss", roles: ALL, group: "Reports" },
  { to: "/cash-flow", label: "Cash Flow", icon: Waves, testid: "nav-cash-flow", roles: ALL, group: "Reports" },
  { to: "/budget-vs-actual", label: "Budget vs Actual", icon: Target, testid: "nav-budget", roles: ALL, group: "Reports" },
  { to: "/business-reports", label: "Business Reports", icon: BarChart3, testid: "nav-business-reports", roles: ALL, group: "Reports" },
  { to: "/aging", label: "AR / AP Aging", icon: Hourglass, testid: "nav-aging", roles: ALL, group: "Reports" },
  { to: "/periods", label: "Fiscal Periods", icon: Lock, testid: "nav-fiscal-periods", roles: ALL, group: "Reports" },
  { to: "/invoices", label: "Invoices", icon: Receipt, testid: "nav-invoices", roles: ALL, group: "Operations" },
  { to: "/bills", label: "Bills", icon: ReceiptText, testid: "nav-bills", roles: ALL, group: "Operations" },
  { to: "/expenses", label: "Expenses & Vendors", icon: Coins, testid: "nav-expenses", roles: ALL, group: "Operations" },
  { to: "/receipts", label: "Receipt Inbox", icon: Camera, testid: "nav-receipts", roles: ALL, group: "Operations" },
  { to: "/mileage", label: "Mileage Log", icon: Car, testid: "nav-mileage", roles: ALL, group: "Operations" },
  { to: "/collections", label: "Collections", icon: HandCoins, testid: "nav-collections", roles: ALL, group: "Operations" },
  { to: "/simulator", label: "Can I Afford It?", icon: Calculator, testid: "nav-simulator", roles: ALL, group: "Planning" },
  { to: "/sub-compliance", label: "Sub Compliance", icon: ShieldCheck, testid: "nav-sub-compliance", roles: ALL, group: "Planning" },
  { to: "/cpa-package", label: "CPA Package", icon: Package, testid: "nav-cpa-package", roles: ALL, group: "Planning" },
  { to: "/pulse", label: "Profit Pulse", icon: Activity, testid: "nav-pulse", roles: ALL, group: "Overview" },
  { to: "/pricing", label: "Price Guidance", icon: Tag, testid: "nav-pricing", roles: ALL, group: "Planning" },
  { to: "/tax-forecast", label: "Tax Forecast", icon: Landmark, testid: "nav-tax-forecast", roles: ALL, group: "Planning" },
  { to: "/playground", label: "Scenario Playground", icon: SlidersHorizontal, testid: "nav-playground", roles: ALL, group: "Planning" },
  { to: "/combo", label: "Empire Combo", icon: Rocket, testid: "nav-combo", roles: ALL, group: "Growth" },
  { to: "/kpis", label: "Trade KPIs", icon: Gauge, testid: "nav-kpis", roles: ALL, group: "Overview" },
  { to: "/benchmarks", label: "Benchmarks", icon: Users, testid: "nav-benchmarks", roles: ALL, group: "Overview" },
  { to: "/voice", label: "Voice books", icon: Mic, testid: "nav-voice", roles: ALL, group: "Overview" },
  { to: "/firm", label: "Firm dashboard", icon: Building2, testid: "nav-firm", roles: ["owner", "accountant"], group: "Growth" },
  { to: "/entities", label: "Entities", icon: Layers, testid: "nav-entities", roles: ALL, group: "Overview" },
  { to: "/quick-sell", label: "Quick Sell", icon: Scissors, testid: "nav-quick-sell", roles: ["owner", "barber"], group: "Operations" },
  { to: "/inventory", label: "Inventory", icon: Package, testid: "nav-inventory", roles: ALL, group: "Operations" },
  { to: "/gift-cards", label: "Gift Cards", icon: Gift, testid: "nav-gift-cards", roles: ALL, group: "Operations" },
  { to: "/commissions", label: "Commissions", icon: Coins, testid: "nav-commissions", roles: ALL, group: "Operations" },
  { to: "/cash-close", label: "Cash Close", icon: Wallet, testid: "nav-cash-close", roles: ["owner"], group: "Operations" },
  { to: "/recurring-invoices", label: "Recurring Invoices", icon: Repeat, testid: "nav-recurring-invoices", roles: ALL, group: "Operations" },
  { to: "/time", label: "Time Tracking", icon: Clock, testid: "nav-time", roles: ["owner", "accountant", "bookkeeper", "barber"], group: "Operations" },
  { to: "/payroll", label: "Payroll", icon: Users, testid: "nav-payroll", roles: ["owner", "accountant"], group: "Operations" },
  { to: "/copilot", label: "AI Copilot", icon: Bot, testid: "nav-copilot", roles: ["owner"], group: "Operations" },
  { to: "/budget", label: "Budget & Forecast", icon: TrendingUp, testid: "nav-budget-forecast", roles: ALL, group: "Reports" },
  { to: "/tax", label: "Tax & 1099s", icon: FileCheck2, testid: "nav-tax", roles: ALL, group: "Reports" },
  { to: "/backup", label: "Backup & Restore", icon: DatabaseBackup, testid: "nav-backup", roles: ["owner"], group: "Operations" },
  { to: "/audit", label: "Audit Log", icon: FileText, testid: "nav-audit", roles: ["owner", "accountant"], group: "Operations" },
  { to: "/settings", label: "Settings", icon: ShieldCheck, testid: "nav-settings", roles: ["owner"], group: "Operations" },
];

const ROLE_LABEL_STATIC = { owner: "Owner / Admin", accountant: "Accountant", bookkeeper: "Bookkeeper" };

export default function Layout() {
  const { user, logout, switchRole } = useAuth();
  const { pack, t } = useNiche();
  const navigate = useNavigate();
  const [switching, setSwitching] = useState(false);
  const brand = pack?.branding || {};
  const accent = brand.accent_color || pack?.colors?.accent || "#f59e0b";
  const brandName = brand.firm_name || pack?.product_name || "Mind at Ease Solutions";
  const brandTag = brand.tagline || pack?.tagline || "Bookkeeping";
  const ROLE_LABEL = { ...ROLE_LABEL_STATIC, barber: t("worker", "Technician") };

  const role = user?.role;
  const groups = [...new Set(NAV.filter((n) => n.roles.includes(role)).map((n) => n.group))];

  const handleSwitch = async (r) => {
    setSwitching(true);
    try {
      await switchRole(r);
      toast.success(`Viewing as ${ROLE_LABEL[r]}`);
      navigate("/");
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setSwitching(false);
    }
  };

  return (
    <div className="min-h-screen flex">
      {/* Sidebar */}
      <aside className="w-64 shrink-0 border-r border-[#1b2230] bg-[#0D0F14] hidden lg:flex flex-col">
        <div className="h-16 flex items-center gap-3 px-5 border-b border-[#1b2230]">
          {brand.logo_url
            ? <img src={brand.logo_url} alt={brandName} className="w-9 h-9 rounded-lg object-contain bg-white/5" />
            : <div className="w-9 h-9 rounded-lg barber-pole shadow-inner" />}
          <div>
            <div className="font-display font-bold text-[15px] leading-tight text-gray-50">{brandName}</div>
            <div className="text-[10px] uppercase tracking-widest" style={{ color: `${accent}cc` }}>{brandTag}</div>
          </div>
        </div>
        <nav className="flex-1 overflow-y-auto py-4 px-3 space-y-5">
          {groups.map((g) => (
            <div key={g}>
              <div className="px-2 mb-2 text-[10px] uppercase tracking-widest text-gray-500 font-semibold">{g}</div>
              <div className="space-y-1">
                {NAV.filter((n) => n.group === g && n.roles.includes(role)).map((n) => (
                  <NavLink
                    key={n.to}
                    to={n.to}
                    end={n.to === "/"}
                    data-testid={n.testid}
                    className={({ isActive }) =>
                      `flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors ${
                        isActive
                          ? "bg-amber-500/10 text-amber-400 border border-amber-500/30"
                          : "text-gray-400 hover:text-gray-100 hover:bg-[#161b25] border border-transparent"
                      }`
                    }
                  >
                    <n.icon size={17} />
                    <span>{n.label}</span>
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>
      </aside>

      {/* Main */}
      <div className="flex-1 flex flex-col min-w-0">
        <header className="h-16 shrink-0 border-b border-[#1b2230] bg-[#0D0F14]/80 backdrop-blur-xl flex items-center justify-between px-4 sm:px-6 sticky top-0 z-20">
          <div className="flex items-center gap-2 min-w-0">
            <span className="font-display font-semibold text-gray-100 truncate">{user?.tenant?.name || "Workspace"}</span>
          </div>
          <div className="flex items-center gap-3">
            <WorkspaceSwitcher />
            <div className="hidden sm:flex items-center gap-2 text-xs text-gray-400">
              <ShieldCheck size={15} className="text-emerald-400" />
              <span>Preview role</span>
            </div>
            <Select value={role} onValueChange={handleSwitch} disabled={switching}>
              <SelectTrigger data-testid="role-switcher-select" className="w-[170px] h-9 bg-[#161b25] border-[#222838] text-sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
                {Object.entries(ROLE_LABEL).map(([k, v]) => (
                  <SelectItem key={k} value={k} data-testid={`role-option-${k}`}>{v}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <button
              data-testid="logout-btn"
              onClick={logout}
              className="h-9 w-9 grid place-items-center rounded-lg text-gray-400 hover:text-rose-400 hover:bg-rose-500/10 transition-colors"
              title="Sign out"
            >
              <LogOut size={17} />
            </button>
          </div>
        </header>
        <main className="flex-1 p-4 sm:p-6 lg:p-8 overflow-x-hidden">
          <Outlet />
        </main>
      </div>
      <MusicPlayer />
    </div>
  );
}
