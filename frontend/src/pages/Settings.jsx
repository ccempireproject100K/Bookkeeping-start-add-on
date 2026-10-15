import { PageHeader } from "@/components/Page";
import TotpSettings from "@/components/TotpSettings";

export default function Settings() {
  return (
    <div className="animate-rise">
      <PageHeader
        title="Settings"
        subtitle="Workspace security and preferences"
        testid="settings-page"
      />
      <TotpSettings />
    </div>
  );
}
