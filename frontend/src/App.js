import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { AuthProvider, useAuth } from "@/context/AuthContext";
import Layout from "@/components/Layout";
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
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/" element={<Protected><Layout /></Protected>}>
              <Route index element={<Dashboard />} />
              <Route path="jobs" element={<Jobs />} />
              <Route path="job-profitability" element={<JobProfitability />} />
              <Route path="customers" element={<Customers />} />
              <Route path="accounts" element={<ChartOfAccounts />} />
              <Route path="journal" element={<JournalEntries />} />
              <Route path="general-ledger" element={<GeneralLedger />} />
              <Route path="trial-balance" element={<TrialBalance />} />
              <Route path="balance-sheet" element={<BalanceSheet />} />
              <Route path="profit-loss" element={<ProfitLoss />} />
              <Route path="periods" element={<FiscalPeriods />} />
              <Route path="inventory" element={<Inventory />} />
              <Route path="quick-sell" element={<QuickSell />} />
              <Route path="invoices" element={<Invoices />} />
              <Route path="aging" element={<Aging />} />
              <Route path="gift-cards" element={<GiftCards />} />
              <Route path="commissions" element={<Commissions />} />
              <Route path="cash-close" element={<CashClose />} />
            </Route>
          </Routes>
        </BrowserRouter>
        <Toaster position="top-right" theme="dark" richColors />
      </AuthProvider>
    </div>
  );
}

export default App;
