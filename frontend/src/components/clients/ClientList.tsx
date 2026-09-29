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
          <div key={client.client_id} className="client-row">
            <button type="button" onClick={() => onSelect(client.client_id)}>{client.name}</button>
            <div className="meta">
              {`${client.phone_e164} · ${client.home_size} · ${client.active ? "active" : "inactive"}`}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
