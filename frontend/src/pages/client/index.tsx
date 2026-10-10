import type { ReactNode } from "react";

import { ClientHome } from "@/components/client/ClientHome";
import { clientLayout, useClientSession } from "@/client/ClientAccess";

/** Calendar. For now it shows the same content as Appointments; later work gives each its own view. */
export default function ClientCalendarPage() {
  const { config, token, zone, onSessionEnded } = useClientSession();
  return <ClientHome config={config} token={token} zone={zone} onSessionEnded={onSessionEnded} />;
}

ClientCalendarPage.getLayout = (page: ReactNode) => clientLayout(page);
