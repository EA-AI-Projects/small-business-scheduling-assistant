"use strict";

const $ = (id) => document.getElementById(id);
const state = { config: null, token: null, policy: null, calendar: null,
  requests: [], clients: [], selectedClient: null, selectedEvent: null, zone: "UTC",
  generation: 0, notesRequest: 0, eventRequest: 0 };

function node(tag, className = "", value = "") {
  const item = document.createElement(tag);
  if (className) item.className = className;
  item.textContent = value;
  return item;
}

function button(label, action, className = "") {
  const item = node("button", className, label);
  item.type = "button";
  item.addEventListener("click", action);
  return item;
}

function confirmedButton(label, confirmation, action, className = "") {
  const item = button(label, () => {
    if (item.dataset.armed === "yes") return action();
    item.dataset.armed = "yes";
    item.textContent = confirmation;
  }, className);
  return item;
}

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
}

function clear(element) { element.replaceChildren(); }

function base() {
  return `/v1/owner/businesses/${encodeURIComponent(state.config.business_id)}`;
}

async function api(path, options = {}) {
  const headers = { Authorization: `Bearer ${state.token}` };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.idempotent) headers["Idempotency-Key"] = crypto.randomUUID();
  const response = await fetch(`${base()}${path}`, {
    method: options.method || "GET", headers, credentials: "omit",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401) signOut();
    const error = new Error(data?.error?.message || `Server returned ${response.status}`);
    error.status = response.status;
    error.current = data?.current;
    throw error;
  }
  return data;
}

async function change(path, method, body, success, idempotent = true) {
  try {
    await api(path, { method, body, idempotent });
  } catch (error) {
    if (error.status === 409) {
      try {
        await refresh(true);
        notice(`Nothing was saved: ${error.message}. Current state has been refreshed.`, true);
      } catch (refreshError) {
        notice(`Nothing was saved: ${error.message}. Current state could not load: ${refreshError.message}`, true);
      }
    } else {
      notice(`Nothing was saved: ${error.message}`, true);
    }
    return false;
  }
  try {
    await refresh();
    notice(success);
  } catch (error) {
    notice(`${success}, but the latest view could not load: ${error.message}`, true);
  }
  return true;
}

function b64url(bytes) {
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function signIn() {
  if (!globalThis.crypto?.subtle) throw new Error("Secure browser context is required for sign-in");
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = b64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
  const stateValue = b64url(crypto.getRandomValues(new Uint8Array(24)));
  sessionStorage.setItem("owner-pkce-verifier", verifier);
  sessionStorage.setItem("owner-oauth-state", stateValue);
  const url = new URL(state.config.authorize_url);
  url.search = new URLSearchParams({ response_type: "code", client_id: state.config.client_id,
    redirect_uri: state.config.redirect_uri, scope: "openid", state: stateValue,
    code_challenge_method: "S256", code_challenge: challenge }).toString();
  location.assign(url.toString());
}

function signOut() {
  state.generation += 1;
  state.notesRequest += 1;
  state.eventRequest += 1;
  state.token = null;
  state.calendar = null;
  state.policy = null;
  state.requests = [];
  state.clients = [];
  state.selectedClient = null;
  state.selectedEvent = null;
  state.zone = "UTC";
  sessionStorage.removeItem("owner-pkce-verifier");
  sessionStorage.removeItem("owner-oauth-state");
  for (const id of ["calendar-days", "event-detail", "request-list", "client-list",
    "note-list", "policy-summary"]) clear($(id));
  for (const id of ["block-form", "profile-form", "note-form", "exception-form"]) $(id).reset();
  $("profile-form").elements.namedItem("client_id").readOnly = false;
  $("calendar-zone").textContent = "";
  $("request-count").textContent = "";
  $("notice").textContent = "";
  $("workspace").hidden = true;
  $("signed-out").hidden = false;
  $("connection").textContent = "Signed out";
  $("auth-button").textContent = "Sign in";
}

async function completeSignIn() {
  const url = new URL(location.href);
  const code = url.searchParams.get("code");
  const returnedState = url.searchParams.get("state");
  const oauthError = url.searchParams.get("error");
  if (!code && !oauthError) return;
  history.replaceState(null, "", url.pathname);
  if (oauthError) throw new Error(`Sign-in failed: ${oauthError}`);
  const verifier = sessionStorage.getItem("owner-pkce-verifier");
  const expectedState = sessionStorage.getItem("owner-oauth-state");
  sessionStorage.removeItem("owner-pkce-verifier");
  sessionStorage.removeItem("owner-oauth-state");
  if (!verifier || !expectedState || expectedState !== returnedState) {
    throw new Error("Sign-in response did not match this browser session");
  }
  const response = await fetch(state.config.token_url, { method: "POST", credentials: "omit",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", client_id: state.config.client_id,
      code, redirect_uri: state.config.redirect_uri, code_verifier: verifier }) });
  const tokens = await response.json();
  if (!response.ok || !tokens.access_token) throw new Error("Could not complete sign-in");
  state.token = tokens.access_token;
  $("workspace").hidden = false;
  $("signed-out").hidden = true;
  $("connection").textContent = "Owner signed in";
  $("auth-button").textContent = "Sign out";
  await refresh();
}

function dayKey(instant) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: state.zone,
    year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(instant))
    .filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function localTime(instant) {
  return new Intl.DateTimeFormat("en-US", { timeZone: state.zone, hour: "numeric",
    minute: "2-digit" }).format(new Date(instant));
}

function localStamp(instant) { return `${dayKey(instant)} · ${localTime(instant)}`; }

function localInput(instant) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: state.zone,
    hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit" }).formatToParts(new Date(instant))
    .filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

async function resolveLocal(value) {
  if (!value) throw new Error("Choose a local date and time");
  const result = await api(`/local-time?value=${encodeURIComponent(value)}`);
  return result.instant;
}

function datesForView() {
  const selected = $("calendar-date").value;
  const day = new Date(`${selected}T12:00:00Z`);
  if ($("calendar-view").value === "week") day.setUTCDate(day.getUTCDate() - ((day.getUTCDay() + 6) % 7));
  const count = $("calendar-view").value === "week" ? 7 : 1;
  return Array.from({ length: count }, (_, index) => {
    const copy = new Date(day);
    copy.setUTCDate(copy.getUTCDate() + index);
    return copy.toISOString().slice(0, 10);
  });
}

function renderCalendar() {
  const host = $("calendar-days");
  clear(host);
  if (!state.calendar) return;
  $("calendar-zone").textContent = `Times in ${state.zone} · revision ${state.calendar.revision}`;
  for (const date of datesForView()) {
    const day = node("section", "card day");
    const title = new Date(`${date}T12:00:00Z`).toLocaleDateString("en-US", {
      weekday: "long", month: "short", day: "numeric", timeZone: "UTC" });
    day.append(node("h3", "", title));
    const events = state.calendar.events.filter((event) => dayKey(event.start_at) === date)
      .sort((a, b) => a.start_at.localeCompare(b.start_at));
    if (!events.length) day.append(node("p", "empty", "No scheduled items"));
    for (const event of events) {
      const card = button("", () => showEvent(event), `event ${event.status.toLowerCase().split("_")[0]}`);
      card.append(node("strong", "", `${localTime(event.start_at)}–${localTime(event.end_at)}`));
      card.append(node("span", "", event.status.replaceAll("_", " ")));
      day.append(card);
    }
    host.append(day);
  }
}

function field(label, value, type = "text") {
  const wrapper = node("label", "", label);
  const input = document.createElement("input");
  input.type = type;
  input.value = value;
  wrapper.append(input);
  return { wrapper, input };
}

async function showEvent(event) {
  const generation = state.generation;
  const request = ++state.eventRequest;
  const host = $("event-detail");
  clear(host);
  state.selectedEvent = event;
  const block = event.status === "UNAVAILABLE";
  try {
    const detail = await api(block ? `/blocks/${encodeURIComponent(event.event_id)}`
      : `/appointments/${encodeURIComponent(event.event_id)}`);
    if (generation !== state.generation || request !== state.eventRequest) return;
    host.append(node("p", "badge", event.status.replaceAll("_", " ")));
    host.append(node("p", "", `${localStamp(event.start_at)}–${localTime(event.end_at)}`));
    if (!block) host.append(node("p", "meta", `Client ${detail.client_id} · ${detail.duration_minutes} minutes`));
    if (!block) host.append(button("Open client and visit notes", async () => {
      const clickGeneration = state.generation;
      const client = state.clients.find((item) => item.client_id === detail.client_id);
      if (!client) { notice("Client profile is not available", true); return; }
      await selectClient(client);
      if (clickGeneration !== state.generation || state.selectedClient !== client.client_id) return;
      $("note-form").elements.namedItem("appointment_id").value = detail.appointment_id;
      document.querySelector('.tabs button[data-tab="clients"]').click();
    }));
    const start = field("Start", localInput(detail.start_at), "datetime-local");
    host.append(start.wrapper);
    if (block) {
      const end = field("End", localInput(detail.end_at), "datetime-local");
      host.append(end.wrapper);
      const actions = node("div", "row-actions");
      actions.append(button("Move block", async () => {
        try {
          const body = { expected_revision: state.calendar.revision,
            expected_version: detail.version, start_at: await resolveLocal(start.input.value),
            end_at: await resolveLocal(end.input.value) };
          await change(`/blocks/${encodeURIComponent(detail.block_id)}`, "PUT", body, "Block moved");
        } catch (error) { notice(`Nothing was saved: ${error.message}`, true); }
      }));
      actions.append(confirmedButton("Remove block", "Confirm removal", async () => {
        await change(`/blocks/${encodeURIComponent(detail.block_id)}`, "DELETE",
          { expected_revision: state.calendar.revision, expected_version: detail.version }, "Block removed");
      }, "danger"));
      host.append(actions);
    } else if (detail.status === "CONFIRMED") {
      const duration = field("Duration (minutes)", String(detail.duration_minutes), "number");
      duration.input.min = "1";
      host.append(duration.wrapper);
      const actions = node("div", "row-actions");
      actions.append(button("Save appointment", async () => {
        try {
          const body = { expected_version: detail.version, start_at: await resolveLocal(start.input.value),
            duration_minutes: Number(duration.input.value) };
          await change(`/appointments/${encodeURIComponent(detail.appointment_id)}`, "PATCH", body,
            "Appointment saved");
        } catch (error) { notice(`Nothing was saved: ${error.message}`, true); }
      }));
      actions.append(confirmedButton("Cancel appointment", "Confirm cancellation", async () => {
        await change(`/appointments/${encodeURIComponent(detail.appointment_id)}/cancel`, "POST",
          { expected_version: detail.version }, "Appointment cancelled");
      }, "danger"));
      host.append(actions);
    } else if (detail.status === "PENDING_APPROVAL") {
      host.append(node("p", "hint", "Use Requests to approve or decline this exact request."));
    }
  } catch (error) {
    if (generation === state.generation && request === state.eventRequest) notice(error.message, true);
  }
}

function renderRequests() {
  const host = $("request-list");
  clear(host);
  $("request-count").textContent = state.requests.length ? `(${state.requests.length})` : "";
  if (!state.requests.length) host.append(node("p", "empty", "No pending requests"));
  for (const request of state.requests) {
    const card = node("article", "request");
    card.append(node("h3", "", `${localStamp(request.start_at)}–${localTime(request.end_at)}`));
    card.append(node("p", "meta", `Client ${request.client_id} · ${request.duration_minutes} minutes · expires ${localStamp(request.hold_expires_at)}`));
    if (request.replaces_appointment_id) card.append(node("p", "hint", "Replacement request; original visit stays until approval."));
    const actions = node("div", "row-actions");
    for (const action of ["approve", "decline"]) {
      actions.append(confirmedButton(action === "approve" ? "Approve" : "Decline",
        action === "approve" ? "Confirm approval" : "Confirm decline", async () => {
        await change(`/requests/${encodeURIComponent(request.appointment_id)}/${action}`, "POST",
          { expected_version: request.version }, `Request ${action === "approve" ? "approved" : "declined"}`);
      }, action === "approve" ? "primary" : "danger"));
    }
    card.append(actions);
    host.append(card);
  }
}

function renderClients() {
  const host = $("client-list");
  clear(host);
  if (!state.clients.length) host.append(node("p", "empty", "No clients yet"));
  for (const client of state.clients) {
    const row = node("div", "client-row");
    row.append(button(client.name, () => selectClient(client)));
    row.append(node("div", "meta", `${client.phone_e164} · ${client.home_size} · ${client.active ? "active" : "inactive"}`));
    host.append(row);
  }
}

async function selectClient(client) {
  state.selectedClient = client.client_id;
  const form = $("profile-form");
  for (const name of ["client_id", "name", "phone_e164", "service_address", "home_size", "default_duration_minutes"]) {
    form.elements.namedItem(name).value = client[name];
  }
  form.elements.namedItem("client_id").readOnly = true;
  form.elements.namedItem("active").checked = client.active;
  await renderNotes();
}

async function renderNotes() {
  const generation = state.generation;
  const request = ++state.notesRequest;
  const clientId = state.selectedClient;
  const host = $("note-list");
  clear(host);
  if (!clientId) return;
  try {
    const notes = await api(`/clients/${encodeURIComponent(clientId)}/notes`);
    if (generation !== state.generation || request !== state.notesRequest ||
        clientId !== state.selectedClient) return;
    if (!notes.length) host.append(node("p", "empty", "No ordinary notes"));
    for (const note of notes) {
      const row = node("div", "note-row");
      row.append(node("p", "", note.body));
      row.append(node("p", "meta", `${note.appointment_id ? `Visit ${note.appointment_id} · ` : "Client note · "}${localStamp(note.created_at)}`));
      if (note.legal_hold_reason) row.append(node("p", "badge", "Legal hold"));
      row.append(confirmedButton("Delete", "Confirm deletion", async () => {
        await change(`/clients/${encodeURIComponent(note.client_id)}/notes/${encodeURIComponent(note.note_id)}`,
          "DELETE", undefined, "Note deleted", false);
      }, "danger"));
      host.append(row);
    }
  } catch (error) {
    if (generation === state.generation && request === state.notesRequest) notice(error.message, true);
  }
}

function renderPolicy() {
  const host = $("policy-summary");
  clear(host);
  if (!state.policy) { host.append(node("p", "empty", "Policy is not configured")); return; }
  const policy = state.policy.record.policy;
  host.append(node("p", "", `Timezone: ${policy.timezone}`));
  host.append(node("p", "", `Maximum visit: ${policy.maximum_visit_minutes} minutes · Gap: ${policy.minimum_visit_gap_minutes} minutes`));
  host.append(node("p", "meta", `Version ${state.policy.record.version} · calendar revision ${state.policy.calendar_revision}`));
  const entries = Object.entries(policy.date_exceptions || {}).sort(([a], [b]) => a.localeCompare(b));
  if (!entries.length) host.append(node("p", "empty", "No date exceptions"));
  for (const [date, windows] of entries) {
    const text = windows.length ? windows.map((window) => `${window.opens}–${window.closes}`).join(", ") : "Closed";
    host.append(node("p", "meta", `${date}: ${text}`));
  }
}

async function refresh(preserveSelectedEvent = false) {
  const generation = state.generation;
  const selectedEventId = preserveSelectedEvent ? state.selectedEvent?.event_id : null;
  const [calendar, requests, clients, policy] = await Promise.all([
    api("/calendar"), api("/requests"), api("/clients"), api("/policy").catch((error) => {
      if (error.status === 404) return null;
      throw error;
    }),
  ]);
  if (generation !== state.generation) throw new Error("Owner session ended");
  state.calendar = calendar;
  state.requests = requests;
  state.clients = clients;
  state.policy = policy;
  state.zone = policy?.record.policy.timezone || "UTC";
  if (!$("calendar-date").value) $("calendar-date").value = dayKey(new Date().toISOString());
  renderCalendar();
  state.eventRequest += 1;
  state.selectedEvent = null;
  $("event-detail").textContent = "Choose an appointment or block.";
  renderRequests();
  renderClients();
  renderPolicy();
  if (state.selectedClient) {
    const selected = clients.find((client) => client.client_id === state.selectedClient);
    if (selected) await selectClient(selected);
  }
  if (generation !== state.generation) throw new Error("Owner session ended");
  if (selectedEventId) {
    const selected = calendar.events.find((event) => event.event_id === selectedEventId);
    if (selected) await showEvent(selected);
    else $("event-detail").textContent = "Selected item is no longer on the calendar.";
  }
}

function bindForms() {
  $("block-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      const body = { expected_revision: state.calendar.revision,
        start_at: await resolveLocal(form.elements.namedItem("start").value),
        end_at: await resolveLocal(form.elements.namedItem("end").value) };
      if (await change("/blocks", "POST", body, "Unavailable time added")) form.reset();
    } catch (error) { notice(`Nothing was saved: ${error.message}`, true); }
  });

  $("profile-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const id = form.elements.namedItem("client_id").value.trim();
    const current = state.clients.find((client) => client.client_id === id);
    const body = { expected_version: current?.version || 0,
      name: form.elements.namedItem("name").value.trim(),
      phone_e164: form.elements.namedItem("phone_e164").value.trim(),
      service_address: form.elements.namedItem("service_address").value.trim(),
      home_size: form.elements.namedItem("home_size").value,
      default_duration_minutes: Number(form.elements.namedItem("default_duration_minutes").value),
      active: form.elements.namedItem("active").checked };
    state.selectedClient = id;
    await change(`/clients/${encodeURIComponent(id)}`, "PUT", body, "Client profile saved", false);
  });

  $("note-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.selectedClient) { notice("Select a client first", true); return; }
    const form = event.currentTarget;
    const body = { body: form.elements.namedItem("body").value.trim(),
      appointment_id: form.elements.namedItem("appointment_id").value.trim() || null };
    if (await change(`/clients/${encodeURIComponent(state.selectedClient)}/notes`, "POST", body,
      "Note saved")) form.reset();
  });

  $("exception-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.policy) { notice("Configure the policy before editing exceptions", true); return; }
    const form = event.currentTarget;
    const date = form.elements.namedItem("date").value;
    const mode = form.elements.namedItem("mode").value;
    const policy = structuredClone(state.policy.record.policy);
    if (mode === "normal") delete policy.date_exceptions[date];
    else if (mode === "closed") policy.date_exceptions[date] = [];
    else policy.date_exceptions[date] = [{ opens: form.elements.namedItem("opens").value,
      closes: form.elements.namedItem("closes").value }];
    await change("/policy", "PUT", { expected_revision: state.policy.calendar_revision,
      expected_version: state.policy.record.version, policy }, "Date exception saved");
  });
}

async function init() {
  try {
    const response = await fetch("/owner/config", { credentials: "omit" });
    if (!response.ok) throw new Error("Owner sign-in is not configured");
    state.config = await response.json();
    if (new URL(state.config.redirect_uri).origin !== location.origin) {
      throw new Error("Owner sign-in callback must use this site");
    }
    const beginSignIn = () => signIn().catch((error) => notice(error.message, true));
    $("auth-button").addEventListener("click", () => state.token ? signOut() : beginSignIn());
    $("welcome-sign-in").addEventListener("click", beginSignIn);
    $("refresh").addEventListener("click", () => refresh().catch((error) => notice(error.message, true)));
    $("calendar-date").addEventListener("change", renderCalendar);
    $("calendar-view").addEventListener("change", renderCalendar);
    for (const tab of document.querySelectorAll(".tabs button")) {
      tab.addEventListener("click", () => {
        for (const item of document.querySelectorAll(".tabs button")) item.classList.toggle("active", item === tab);
        for (const panel of document.querySelectorAll(".tab-panel")) panel.hidden = panel.id !== tab.dataset.tab;
      });
    }
    bindForms();
    await completeSignIn();
  } catch (error) { signOut(); notice(error.message, true); }
}

init();
