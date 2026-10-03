import type { ClientProfile } from "@/api/types";

export function ClientList({ clients, onSelect }: {
  clients: ClientProfile[];
  onSelect: (clientId: string) => void;
}) {
  return (
    <div className="card">
      <h3>Client list</h3>
      <div className="card-list">
        {clients.length === 0 && <p className="empty">No clients yet</p>}
        {clients.map((client) => (
          <button key={client.client_id} type="button" className="client-row client-button"
            data-client-id={client.client_id} aria-haspopup="dialog"
            onClick={() => onSelect(client.client_id)}>
            <span className="client-name">{client.name}</span>
            <span className="meta">
              {`${client.phone_e164} · ${client.home_size} · ${client.active ? "active" : "inactive"}`}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
