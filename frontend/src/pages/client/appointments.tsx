import { useRouter } from "next/router";
import type { ReactNode } from "react";

import { ClientHome } from "@/components/client/ClientHome";
import { clientLayout, useClientSession } from "@/client/ClientAccess";

/** Appointments: the client's own bookings, with Cancel, Withdraw and Move. Move continues in the Calendar's Day view. */
export default function ClientAppointmentsPage() {
  const { config, token, zone, onSessionEnded, calendar, move } = useClientSession();
  const router = useRouter();
  return <ClientHome config={config} token={token} zone={zone} onSessionEnded={onSessionEnded} move={move}
    onOpenCalendar={() => { calendar.setView("day"); calendar.goToday(); void router.push("/client/"); }} />;
}

ClientAppointmentsPage.getLayout = (page: ReactNode) => clientLayout(page);
