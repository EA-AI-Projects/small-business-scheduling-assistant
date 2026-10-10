import type { ReactNode } from "react";

import { ClientHome } from "@/components/client/ClientHome";
import { clientLayout, useClientSession } from "@/client/ClientAccess";

/** Appointments. For now it shows the same content as Calendar; later work gives each its own view. */
export default function ClientAppointmentsPage() {
  const { config, token, zone, onSessionEnded } = useClientSession();
  return <ClientHome config={config} token={token} zone={zone} onSessionEnded={onSessionEnded} />;
}

ClientAppointmentsPage.getLayout = (page: ReactNode) => clientLayout(page);
