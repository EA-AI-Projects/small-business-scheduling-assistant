import { useEffect, useState } from "react";

import type { SmsDeliveryFailure } from "@/api/types";
import { ApiError, path } from "@/lib/api";
import { localStamp } from "@/lib/time";
import { errorMessage, useOwner } from "@/owner/OwnerContext";

/** Failed outbound texts, including refusals before a carrier send. */
export function DeliveryFailures() {
  const { api, stamp, data } = useOwner();
  const [failures, setFailures] = useState<SmsDeliveryFailure[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const businessId = data.calendar?.business_id;

  useEffect(() => {
    if (!businessId) return;
    let current = true;
    api.get<SmsDeliveryFailure[]>(path`/sms-delivery-failures`)
      .then((items) => { if (current) { setFailures(items); setProblem(null); setUnavailable(false); } })
      .catch((error: unknown) => {
        if (!current) return;
        setFailures(null);
        setUnavailable(error instanceof ApiError && error.status === 404);
        setProblem(error instanceof ApiError && error.status === 404 ? null : errorMessage(error));
      });
    return () => { current = false; };
  }, [api, businessId, stamp.version]);

  return (
    <section className="card">
      <h3>Text delivery failures</h3>
      {unavailable && <p className="hint">Text delivery failures are not available here.</p>}
      {problem && <p className="hint">{`Could not load text delivery failures: ${problem}`}</p>}
      {failures?.length === 0 && <p className="empty">No failed texts</p>}
      <div className="card-list" aria-label="Text delivery failures">
        {failures?.map((failure) => (
          <div key={failure.outbox_id} className="client-row">
            <div>{failure.recipient === "client" ? "Client" : failure.recipient === "owner" ? "Owner" : failure.recipient}</div>
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
