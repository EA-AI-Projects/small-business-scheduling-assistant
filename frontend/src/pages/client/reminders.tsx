import type { ReactNode } from "react";
import { clientLayout, useClientSession } from "@/client/ClientAccess";
import { ClientReminders } from "@/components/client/ClientReminders";

export default function ClientRemindersPage() {
  const { config, token, onSessionEnded } = useClientSession();
  return <ClientReminders config={config} token={token} onSessionEnded={onSessionEnded} />;
}

ClientRemindersPage.getLayout = (page: ReactNode) => clientLayout(page);
