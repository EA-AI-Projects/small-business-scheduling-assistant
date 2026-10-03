import type { OwnerApi } from "@/lib/api";
import { path } from "@/lib/api";

/** A 204 is the only confirmation of erasure. A transport error leaves the dialog open. */
export async function deleteClientFlow(api: OwnerApi, clientId: string, actions: {
  clearSelection: () => void;
  close: () => void;
  refresh: () => Promise<void>;
  notify: (message: string, error?: boolean) => void;
}): Promise<string | null> {
  try {
    await api.request(path`/clients/${clientId}`, { method: "DELETE", expectedStatus: 204 });
  } catch (error) {
    return `Deletion could not be confirmed: ${error instanceof Error ? error.message : String(error)}. Refresh the client list before retrying if the connection was interrupted.`;
  }
  actions.clearSelection();
  actions.close();
  try {
    await actions.refresh();
    actions.notify("Client deleted");
  } catch (error) {
    actions.notify(`Client deleted, but the latest calendar and client list could not load: ${error instanceof Error ? error.message : String(error)}. Refresh the page.`, true);
  }
  return null;
}
