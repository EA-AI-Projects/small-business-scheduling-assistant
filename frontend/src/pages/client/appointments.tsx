import type { ReactNode } from "react";

import { ClientHome } from "@/components/client/ClientHome";
import { clientLayout, useClientSession } from "@/client/ClientAccess";

/** Appointments: available times and the client's own bookings, with Cancel and Move. */
export default function ClientAppointmentsPage() {
  const { config, token, zone, onSessionEnded } = useClientSession();
  return <ClientHome config={config} token={token} zone={zone} onSessionEnded={onSessionEnded} />;
}

ClientAppointmentsPage.getLayout = (page: ReactNode) => clientLayout(page);
