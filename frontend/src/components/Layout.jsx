import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { LayoutDashboard, BookOpen, FileText, Scale, ScrollText, Landmark, TrendingUp, Lock, Scissors, Receipt, Wallet, Package, LogOut, ShieldCheck, Hourglass, Gift, Coins, Briefcase, PieChart, Users, Building2, FileSignature, Waves, Target, BarChart3, ReceiptText, Repeat, DatabaseBackup, Bot, Clock, FileCheck2, Camera, Car, HandCoins, Calculator, Activity, Tag, SlidersHorizontal, Rocket, Gauge, Mic, Layers, Sparkles, ClipboardList, Link2, Store, KeyRound } from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import { errMsg } from "@/lib/api";
import MusicPlayer from "./MusicPlayer";
import WorkspaceSwitcher from "./WorkspaceSwitcher";
import LanguageSwitcher from "./LanguageSwitcher";
import { useNiche } from "@/context/NicheContext";
import { useTranslation } from "react-i18next";
const ALL = ["owner", "accountant", "bookkeeper"];
const NAV = [{
  to: "/",
  labelKey: "nav.dashboard",
  icon: LayoutDashboard,
  testid: "nav-dashboard",
  roles: ["owner", "accountant", "bookkeeper", "barber"],
  groupKey: "navGroup.overview"
}, {
  to: "/jobs",
  labelKey: "nav.jobs",
  icon: Briefcase,
  testid: "nav-jobs",
  roles: ALL,
  groupKey: "navGroup.jobCosting"
}, {
  to: "/job-profitability",
  labelKey: "nav.jobProfitability",
  icon: PieChart,
  testid: "nav-job-profitability",
  roles: ALL,
  groupKey: "navGroup.jobCosting"
}, {
  to: "/customers",
  labelKey: "nav.customers",
  icon: Users,
  testid: "nav-customers",
  roles: ALL,
  groupKey: "navGroup.jobCosting"
}, {
  to: "/estimates",
  labelKey: "nav.estimates",
  icon: FileSignature,
  testid: "nav-estimates",
  roles: ALL,
  groupKey: "navGroup.jobCosting"
}, {
  to: "/accounts",
  labelKey: "nav.chartOfAccounts",
  icon: BookOpen,
  testid: "nav-chart-of-accounts",
  roles: ALL,
  groupKey: "navGroup.ledger"
}, {
  to: "/journal",
  labelKey: "nav.journalEntries",
  icon: FileText,
  testid: "nav-journal-entries",
  roles: ALL,
  groupKey: "navGroup.ledger"
}, {
  to: "/general-ledger",
  labelKey: "nav.generalLedger",
  icon: ScrollText,
  testid: "nav-general-ledger",
  roles: ALL,
  groupKey: "navGroup.ledger"
}, {
  to: "/banking",
  labelKey: "nav.banking",
  icon: Building2,
  testid: "nav-banking",
  roles: ALL,
  groupKey: "navGroup.banking"
}, {
  to: "/trial-balance",
  labelKey: "nav.trialBalance",
  icon: Scale,
  testid: "nav-trial-balance",
  roles: ALL,
  groupKey: "navGroup.reports"
}, {
  to: "/balance-sheet",
  labelKey: "nav.balanceSheet",
  icon: Landmark,
  testid: "nav-balance-sheet",
  roles: ALL,
  groupKey: "navGroup.reports"
}, {
  to: "/profit-loss",
  labelKey: "nav.profitLoss",
  icon: TrendingUp,
  testid: "nav-profit-loss",
  roles: ALL,
  groupKey: "navGroup.reports"
}, {
  to: "/cash-flow",
  labelKey: "nav.cashFlow",
  icon: Waves,
  testid: "nav-cash-flow",
  roles: ALL,
  groupKey: "navGroup.reports"
}, {
  to: "/budget-vs-actual",
  labelKey: "nav.budgetVsActual",
  icon: Target,
  testid: "nav-budget",
  roles: ALL,
  groupKey: "navGroup.reports"
}, {
  to: "/business-reports",
  labelKey: "nav.businessReports",
  icon: BarChart3,
  testid: "nav-business-reports",
  roles: ALL,
  groupKey: "navGroup.reports"
}, {
  to: "/aging",
  labelKey: "nav.aRAPAging",
  icon: Hourglass,
  testid: "nav-aging",
  roles: ALL,
  groupKey: "navGroup.reports"
}, {
  to: "/periods",
  labelKey: "nav.fiscalPeriods",
  icon: Lock,
  testid: "nav-fiscal-periods",
  roles: ALL,
  groupKey: "navGroup.reports"
}, {
  to: "/invoices",
  labelKey: "nav.invoices",
  icon: Receipt,
  testid: "nav-invoices",
  roles: ALL,
  groupKey: "navGroup.operations"
}, {
  to: "/bills",
  labelKey: "nav.bills",
  icon: ReceiptText,
  testid: "nav-bills",
  roles: ALL,
  groupKey: "navGroup.operations"
}, {
  to: "/expenses",
  labelKey: "nav.expensesVendors",
  icon: Coins,
  testid: "nav-expenses",
  roles: ALL,
  groupKey: "navGroup.operations"
}, {
  to: "/receipts",
  labelKey: "nav.receiptInbox",
  icon: Camera,
  testid: "nav-receipts",
  roles: ALL,
  groupKey: "navGroup.operations"
}, {
  to: "/mileage",
  labelKey: "nav.mileageLog",
  icon: Car,
  testid: "nav-mileage",
  roles: ALL,
  groupKey: "navGroup.operations"
}, {
  to: "/collections",
  labelKey: "nav.collections",
  icon: HandCoins,
  testid: "nav-collections",
  roles: ALL,
  groupKey: "navGroup.operations"
}, {
  to: "/portal-links",
  labelKey: "nav.portalLinks",
  icon: Link2,
  testid: "nav-portal-links",
  roles: ALL,
  groupKey: "navGroup.operations"
}, {
  to: "/simulator",
  labelKey: "nav.canIAffordIt",
  icon: Calculator,
  testid: "nav-simulator",
  roles: ALL,
  groupKey: "navGroup.planning"
}, {
  to: "/sub-compliance",
  labelKey: "nav.subCompliance",
  icon: ShieldCheck,
  testid: "nav-sub-compliance",
  roles: ALL,
  groupKey: "navGroup.planning"
}, {
  to: "/cpa-package",
  labelKey: "nav.cPAPackage",
  icon: Package,
  testid: "nav-cpa-package",
  roles: ALL,
  groupKey: "navGroup.planning"
}, {
  to: "/pulse",
  labelKey: "nav.profitPulse",
  icon: Activity,
  testid: "nav-pulse",
  roles: ALL,
  groupKey: "navGroup.overview"
}, {
  to: "/pricing",
  labelKey: "nav.priceGuidance",
  icon: Tag,
  testid: "nav-pricing",
  roles: ALL,
  groupKey: "navGroup.planning"
}, {
  to: "/tax-forecast",
  labelKey: "nav.taxForecast",
  icon: Landmark,
  testid: "nav-tax-forecast",
  roles: ALL,
  groupKey: "navGroup.planning"
}, {
  to: "/playground",
  labelKey: "nav.scenarioPlayground",
  icon: SlidersHorizontal,
  testid: "nav-playground",
  roles: ALL,
  groupKey: "navGroup.planning"
}, {
  to: "/combo",
  labelKey: "nav.empireCombo",
  icon: Rocket,
  testid: "nav-combo",
  roles: ALL,
  groupKey: "navGroup.growth"
}, {
  to: "/kpis",
  labelKey: "nav.tradeKPIs",
  icon: Gauge,
  testid: "nav-kpis",
  roles: ALL,
  groupKey: "navGroup.overview"
}, {
  to: "/benchmarks",
  labelKey: "nav.benchmarks",
  icon: Users,
  testid: "nav-benchmarks",
  roles: ALL,
  groupKey: "navGroup.overview"
}, {
  to: "/voice",
  labelKey: "nav.voiceBooks",
  icon: Mic,
  testid: "nav-voice",
  roles: ALL,
  groupKey: "navGroup.overview"
}, {
  to: "/firm",
  labelKey: "nav.firmDashboard",
  icon: Building2,
  testid: "nav-firm",
  roles: ["owner", "accountant"],
  groupKey: "navGroup.growth"
}, {
  to: "/entities",
  labelKey: "nav.entities",
  icon: Layers,
  testid: "nav-entities",
  roles: ALL,
  groupKey: "navGroup.overview"
}, {
  to: "/cleanup",
  labelKey: "nav.booksCleanup",
  icon: Sparkles,
  testid: "nav-cleanup",
  roles: ALL,
  groupKey: "navGroup.tools"
}, {
  to: "/cleanup-console",
  labelKey: "nav.cleanupConsole",
  icon: DatabaseBackup,
  testid: "nav-cleanup-console",
  roles: ALL,
  groupKey: "navGroup.tools"
}, {
  to: "/dataclean-api",
  labelKey: "nav.datacleanApi",
  icon: KeyRound,
  testid: "nav-dataclean-api",
  roles: ALL,
  groupKey: "navGroup.tools"
}, {
  to: "/dataclean-studio",
  labelKey: "nav.datacleanStudio",
  icon: Sparkles,
  testid: "nav-dataclean-studio",
  roles: ALL,
  groupKey: "navGroup.tools"
}, {
  to: "/welcome-gift",
  labelKey: "nav.welcomeGift",
  icon: Gift,
  testid: "nav-welcome-gift",
  roles: ALL,
  groupKey: "navGroup.tools"
}, {
  to: "/purchase-orders",
  labelKey: "nav.purchaseOrders",
  icon: ClipboardList,
  testid: "nav-pos",
  roles: ALL,
  groupKey: "navGroup.procurement"
}, {
  to: "/quick-sell",
  labelKey: "nav.quickSell",
  icon: Scissors,
  testid: "nav-quick-sell",
  roles: ["owner", "barber"],
  groupKey: "navGroup.operations"
}, {
  to: "/inventory",
  labelKey: "nav.inventory",
  icon: Package,
  testid: "nav-inventory",
  roles: ALL,
  groupKey: "navGroup.operations"
}, {
  to: "/gift-cards",
  labelKey: "nav.giftCards",
  icon: Gift,
  testid: "nav-gift-cards",
  roles: ALL,
  groupKey: "navGroup.operations"
}, {
  to: "/commissions",
  labelKey: "nav.commissions",
  icon: Coins,
  testid: "nav-commissions",
  roles: ALL,
  groupKey: "navGroup.operations"
}, {
  to: "/cash-close",
  labelKey: "nav.cashClose",
  icon: Wallet,
  testid: "nav-cash-close",
  roles: ["owner"],
  groupKey: "navGroup.operations"
}, {
  to: "/recurring-invoices",
  labelKey: "nav.recurringInvoices",
  icon: Repeat,
  testid: "nav-recurring-invoices",
  roles: ALL,
  groupKey: "navGroup.operations"
}, {
  to: "/time",
  labelKey: "nav.timeTracking",
  icon: Clock,
  testid: "nav-time",
  roles: ["owner", "accountant", "bookkeeper", "barber"],
  groupKey: "navGroup.operations"
}, {
  to: "/payroll",
  labelKey: "nav.payroll",
  icon: Users,
  testid: "nav-payroll",
  roles: ["owner", "accountant"],
  groupKey: "navGroup.operations"
}, {
  to: "/copilot",
  labelKey: "nav.aICopilot",
  icon: Bot,
  testid: "nav-copilot",
  roles: ["owner"],
  groupKey: "navGroup.operations"
}, {
  to: "/budget",
  labelKey: "nav.budgetForecast",
  icon: TrendingUp,
  testid: "nav-budget-forecast",
  roles: ALL,
  groupKey: "navGroup.reports"
}, {
  to: "/tax",
  labelKey: "nav.tax1099s",
  icon: FileCheck2,
  testid: "nav-tax",
  roles: ALL,
  groupKey: "navGroup.reports"
}, {
  to: "/backup",
  labelKey: "nav.backupRestore",
  icon: DatabaseBackup,
  testid: "nav-backup",
  roles: ["owner"],
  groupKey: "navGroup.operations"
}, {
  to: "/audit",
  labelKey: "nav.auditLog",
  icon: FileText,
  testid: "nav-audit",
  roles: ["owner", "accountant"],
  groupKey: "navGroup.operations"
}, {
  to: "/referral-payouts",
  labelKey: "nav.referralPayouts",
  icon: FileText,
  testid: "nav-referral-payouts",
  roles: ["owner"],
  groupKey: "navGroup.operations"
}, {
  to: "/settings",
  labelKey: "nav.settings",
  icon: ShieldCheck,
  testid: "nav-settings",
  roles: ["owner"],
  groupKey: "navGroup.operations"
}, {
  to: "/marketplace",
  labelKey: "nav.marketplace",
  icon: Store,
  testid: "nav-marketplace",
  roles: ALL,
  groupKey: "navGroup.operations"
}, {
  to: "/developers",
  labelKey: "nav.developers",
  icon: KeyRound,
  testid: "nav-developers",
  roles: ["owner"],
  groupKey: "navGroup.operations"
}];
const ROLE_LABEL_STATIC = {
  owner: "roles.owner",
  accountant: "Accountant",
  bookkeeper: "Bookkeeper"
};
export default function Layout() {
  const {
    user,
    logout,
    switchRole
  } = useAuth();
  const {
    pack,
    t
  } = useNiche();
  const navigate = useNavigate();
  const [switching, setSwitching] = useState(false);
  const brand = pack?.branding || {};
  const accent = brand.accent_color || pack?.colors?.accent || "#f59e0b";
  const brandName = brand.firm_name || pack?.product_name || "Mind at Ease Solutions";
  const brandTag = brand.tagline || pack?.tagline || "Bookkeeping";
  const ROLE_LABEL = {
    ...ROLE_LABEL_STATIC,
    barber: "roles.barber"
  };
  const role = user?.role;
  const groups = [...new Set(NAV.filter(n => n.roles.includes(role)).map(n => n.groupKey))];
  const handleSwitch = async r => {
    setSwitching(true);
    try {
      await switchRole(r);
      toast.success(t('layout.viewingAs', { role: t(ROLE_LABEL[r]) }));
      navigate("/");
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setSwitching(false);
    }
  };
  return <div className="min-h-screen flex">
      {/* Sidebar */}
      <aside className="w-64 shrink-0 border-e border-[#1b2230] bg-[#0D0F14] hidden lg:flex flex-col">
        <div className="h-16 flex items-center gap-3 px-5 border-b border-[#1b2230]">
          {brand.logo_url ? <img src={brand.logo_url} alt={brandName} className="w-9 h-9 rounded-lg object-contain bg-white/5" /> : <div className="w-9 h-9 rounded-lg brand-mark shadow-inner" />}
          <div>
            <div className="font-display font-bold text-[15px] leading-tight text-gray-50">{brandName}</div>
            <div className="text-[10px] uppercase tracking-widest" style={{
            color: `${accent}cc`
          }}>{brandTag}</div>
          </div>
        </div>
        <nav className="flex-1 overflow-y-auto py-4 px-3 space-y-5">
          {groups.map(g => <div key={g}>
              <div className="px-2 mb-2 text-[10px] uppercase tracking-widest text-gray-500 font-semibold">{t(g)}</div>
              <div className="space-y-1">
                {NAV.filter(n => n.groupKey === g && n.roles.includes(role)).map(n => <NavLink key={n.to} to={n.to} end={n.to === "/"} data-testid={n.testid} className={({
              isActive
            }) => `flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors ${isActive ? "bg-amber-500/10 text-amber-400 border border-amber-500/30" : "text-gray-400 hover:text-gray-100 hover:bg-[#161b25] border border-transparent"}`}>
                    <n.icon size={17} />
                    <span>{t(n.labelKey)}</span>
                  </NavLink>)}
              </div>
            </div>)}
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
            <LanguageSwitcher compact />
            <div className="hidden sm:flex items-center gap-2 text-xs text-gray-400">
              <ShieldCheck size={15} className="text-emerald-400" />
              <span>{t("layout.previewRole")}</span>
            </div>
            <Select value={role} onValueChange={handleSwitch} disabled={switching}>
              <SelectTrigger data-testid="role-switcher-select" className="w-[170px] h-9 bg-[#161b25] border-[#222838] text-sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-[#14171E] border-[#222838] text-gray-100">
                {Object.entries(ROLE_LABEL).map(([k, v]) => <SelectItem key={k} value={k} data-testid={`role-option-${k}`}>{t(v)}</SelectItem>)}
              </SelectContent>
            </Select>
            <button data-testid="logout-btn" onClick={logout} className="h-9 w-9 grid place-items-center rounded-lg text-gray-400 hover:text-rose-400 hover:bg-rose-500/10 transition-colors" title={t("layout.signOut")}>
              <LogOut size={17} />
            </button>
          </div>
        </header>
        <main className="flex-1 p-4 sm:p-6 lg:p-8 overflow-x-hidden">
          <Outlet />
        </main>
      </div>
      <MusicPlayer />
    </div>;
}
