import type { ReactNode } from "react";

import { ClientHome } from "@/components/client/ClientHome";
import { clientLayout, useClientSession } from "@/client/ClientAccess";

/** Calendar: the client's own appointments and requests in Day, Week, Month, Schedule and Year views. */
export default function ClientCalendarPage() {
  const { config, token, zone, horizonDays, calendar, move, onSessionEnded } = useClientSession();
  return <ClientHome config={config} token={token} zone={zone} horizonDays={horizonDays} calendar={calendar}
    move={move} onSessionEnded={onSessionEnded} />;
}

ClientCalendarPage.getLayout = (page: ReactNode) => clientLayout(page);
