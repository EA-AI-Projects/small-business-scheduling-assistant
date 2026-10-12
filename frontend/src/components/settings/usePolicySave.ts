import { useRef, useState } from "react";

import type { AvailabilityPolicy, PolicyBody, PolicyEditBody, PolicyState } from "@/api/types";
import { ApiError } from "@/lib/api";
import { useOwner } from "@/owner/OwnerContext";

export type SaveFailure = { kind: "conflict" | "stale" | "other"; message: string } | null;

/**
 * One policy write for a Settings form. The caller keeps its own unsaved input; this hook only
 * sends the edit against the policy version and calendar revision currently loaded, reuses one
 * idempotency key for an unchanged retry, and classifies a rejection so the form can explain it.
 */
export function usePolicySave(success: string) {
  const { api, data, refresh, notify } = useOwner();
  const [failure, setFailure] = useState<SaveFailure>(null);
  const attempt = useRef<{ signature: string; key: string } | null>(null);

  async function save(build: (policy: AvailabilityPolicy) => PolicyBody): Promise<boolean> {
    setFailure(null);
    const state: PolicyState | null = data.policy;
    if (!state) {
      setFailure({ kind: "other", message: "Load the pilot policy before editing it." });
      return false;
    }
    const body: PolicyEditBody = { expected_revision: state.calendar_revision,
      expected_version: state.record.version, policy: build(state.record.policy) };
    const signature = JSON.stringify(body);
    if (attempt.current?.signature !== signature) attempt.current = { signature, key: crypto.randomUUID() };
    try {
      await api.request("/policy", { method: "PUT", body, idempotencyKey: attempt.current.key });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (error instanceof ApiError && error.status === 409 && error.code === "STALE_VERSION") {
        // The saved policy or calendar changed; show the newer values and keep the owner's input.
        await refresh(true).catch(() => undefined);
        setFailure({ kind: "stale", message });
      } else if (error instanceof ApiError && error.status === 409 && error.code === "SLOT_CONFLICT") {
        setFailure({ kind: "conflict", message });
      } else {
        setFailure({ kind: "other", message });
      }
      notify("Nothing was saved: the scheduling policy is unchanged", true);
      return false;
    }
    attempt.current = null;
    try {
      await refresh();
      notify(success);
    } catch (error) {
      notify(`${success}, but the latest view could not load: ${
        error instanceof Error ? error.message : String(error)}`, true);
    }
    return true;
  }

  return { save, failure, clearFailure: () => setFailure(null) };
}
