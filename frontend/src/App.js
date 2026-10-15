import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { AuthProvider, useAuth } from "@/context/AuthContext";
import { NicheProvider } from "@/context/NicheContext";
import Layout from "@/components/Layout";
import InventoryGate from "@/components/InventoryGate";
import Login from "@/pages/Login";
import Dashboard from "@/pages/Dashboard";
import ChartOfAccounts from "@/pages/ChartOfAccounts";
import JournalEntries from "@/pages/JournalEntries";
import GeneralLedger from "@/pages/GeneralLedger";
import TrialBalance from "@/pages/TrialBalance";
import BalanceSheet from "@/pages/BalanceSheet";
import ProfitLoss from "@/pages/ProfitLoss";
import FiscalPeriods from "@/pages/FiscalPeriods";
import Inventory from "@/pages/Inventory";
import QuickSell from "@/pages/QuickSell";
import Invoices from "@/pages/Invoices";
import CashClose from "@/pages/CashClose";
import Aging from "@/pages/Aging";
import GiftCards from "@/pages/GiftCards";
import Commissions from "@/pages/Commissions";
import Jobs from "@/pages/Jobs";
import JobProfitability from "@/pages/JobProfitability";
import Customers from "@/pages/Customers";
import Banking from "@/pages/Banking";
import Bills from "@/pages/Bills";
import Expenses from "@/pages/Expenses";
import Receipts from "@/pages/Receipts";
import Estimates from "@/pages/Estimates";
import CashFlow from "@/pages/CashFlow";
import BudgetVsActual from "@/pages/BudgetVsActual";
import BusinessReports from "@/pages/BusinessReports";
import RecurringInvoices from "@/pages/RecurringInvoices";
import Backup from "@/pages/Backup";
import Copilot from "@/pages/Copilot";
import Time from "@/pages/Time";
import PortalView from "@/pages/PortalView";
import Payroll from "@/pages/Payroll";
import Budget from "@/pages/Budget";
import Tax from "@/pages/Tax";
import Settings from "@/pages/Settings";
import AuditLog from "@/pages/AuditLog";
import InviteAccept from "@/pages/InviteAccept";

function Protected({ children }) {
  const { user, loading } = useAuth();
  if (loading || user === null)
    return <div className="app-bg min-h-screen flex items-center justify-center text-amber-500 font-display">Loading…</div>;
  if (!user) return <Navigate to="/login" replace />;
  return children;
}

function App() {
  return (
    <div className="App app-bg">
      <AuthProvider>
        <NicheProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/invite" element={<InviteAccept />} />
            <Route path="/p/:token" element={<PortalView />} />
            <Route path="/" element={<Protected><Layout /></Protected>}>
              <Route index element={<Dashboard />} />
              <Route path="jobs" element={<Jobs />} />
              <Route path="job-profitability" element={<JobProfitability />} />
              <Route path="customers" element={<Customers />} />
              <Route path="estimates" element={<Estimates />} />
              <Route path="accounts" element={<ChartOfAccounts />} />
              <Route path="journal" element={<JournalEntries />} />
              <Route path="general-ledger" element={<GeneralLedger />} />
              <Route path="banking" element={<Banking />} />
              <Route path="trial-balance" element={<TrialBalance />} />
              <Route path="balance-sheet" element={<BalanceSheet />} />
              <Route path="profit-loss" element={<ProfitLoss />} />
              <Route path="cash-flow" element={<CashFlow />} />
              <Route path="budget-vs-actual" element={<BudgetVsActual />} />
              <Route path="business-reports" element={<BusinessReports />} />
              <Route path="periods" element={<FiscalPeriods />} />
              <Route path="inventory" element={<InventoryGate><Inventory /></InventoryGate>} />
              <Route path="quick-sell" element={<QuickSell />} />
              <Route path="invoices" element={<Invoices />} />
              <Route path="bills" element={<Bills />} />
              <Route path="expenses" element={<Expenses />} />
              <Route path="receipts" element={<Receipts />} />
              <Route path="aging" element={<Aging />} />
              <Route path="gift-cards" element={<GiftCards />} />
              <Route path="commissions" element={<Commissions />} />
              <Route path="cash-close" element={<CashClose />} />
              <Route path="recurring-invoices" element={<RecurringInvoices />} />
              <Route path="backup" element={<Backup />} />
              <Route path="copilot" element={<Copilot />} />
              <Route path="time" element={<Time />} />
              <Route path="payroll" element={<Payroll />} />
              <Route path="budget" element={<Budget />} />
              <Route path="tax" element={<Tax />} />
              <Route path="audit" element={<AuditLog />} />
              <Route path="settings" element={<Settings />} />
            </Route>
          </Routes>
        </BrowserRouter>
        </NicheProvider>
        <Toaster position="top-right" theme="dark" richColors />
      </AuthProvider>
    </div>
  );
}

export default App;
