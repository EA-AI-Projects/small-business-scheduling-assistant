import { useEffect, useState } from "react";

import type { SmsDeliveryFailure } from "@/api/types";
import { path } from "@/lib/api";
import { localStamp } from "@/lib/time";
import { errorMessage, useOwner } from "@/owner/OwnerContext";

/** Texts the carrier reported as failed, so the owner can follow up by phone. */
export function DeliveryFailures() {
  const { api, stamp, data } = useOwner();
  const [failures, setFailures] = useState<SmsDeliveryFailure[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const businessId = data.calendar?.business_id;

  useEffect(() => {
    if (!businessId) return;
    let current = true;
    api.get<SmsDeliveryFailure[]>(path`/sms-delivery-failures`)
      .then((items) => { if (current) { setFailures(items); setProblem(null); } })
      .catch((error: unknown) => { if (current) setProblem(errorMessage(error)); });
    return () => { current = false; };
  }, [api, businessId, stamp.version]);

  return (
    <section className="card">
      <h3>Text delivery failures</h3>
      {problem && <p className="hint">{`Could not load text delivery failures: ${problem}`}</p>}
      {failures?.length === 0 && <p className="empty">No failed texts</p>}
      <div className="card-list" aria-label="Text delivery failures">
        {failures?.map((failure) => (
          <div key={failure.outbox_id} className="client-row">
            <div>{failure.recipient}</div>
            <div className="meta">
              {`${failure.status}${failure.error_code ? ` (code ${failure.error_code})` : ""} · ${
                localStamp(failure.observed_at, data.zone)}`}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
