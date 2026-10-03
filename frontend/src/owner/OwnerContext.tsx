import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode } from "react";

import type { Appointment, CalendarSnapshot, ClientProfile, PolicyState } from "@/api/types";
import { ApiError, type OwnerApi, type RequestOptions } from "@/lib/api";
import { stepDate, todayKey, type CalendarView } from "@/lib/time";

export type Tab = "schedule" | "requests" | "clients" | "settings";

export interface OwnerData {
  calendar: CalendarSnapshot | null;
  requests: Appointment[];
  clients: ClientProfile[];
  policy: PolicyState | null;
  /** Business timezone; UTC until the policy has loaded. */
  zone: string;
}

export interface RefreshStamp {
  /** Increments after every completed refresh, so detail views can reload. */
  version: number;
  /** True after a conflict: detail views keep their selection and reload current state. */
  preserveSelection: boolean;
}

export type Notify = (message: string, error?: boolean) => void;

interface OwnerContextValue {
  api: OwnerApi;
  data: OwnerData;
  loaded: boolean;
  stamp: RefreshStamp;
  refresh: (preserveSelection?: boolean) => Promise<void>;
  /**
   * Send one write, then refresh. On conflict nothing was saved: reload current state
   * and say so. Returns whether the write committed.
   */
  change: (path: string, method: NonNullable<RequestOptions["method"]>, body: unknown,
    success: string, idempotent?: boolean) => Promise<boolean>;
  /** Resolve a datetime-local value to one UTC instant in the business timezone. */
  resolveLocal: (value: string) => Promise<string>;
  notify: Notify;
  tab: Tab;
  setTab: (tab: Tab) => void;
  /** Selected calendar date (YYYY-MM-DD, business timezone); follows today until moved. */
  date: string;
  view: CalendarView;
  setView: (view: CalendarView) => void;
  goToday: () => void;
  stepRange: (direction: -1 | 1) => void;
  selectedClientId: string | null;
  selectClient: (clientId: string | null, noteAppointmentId?: string | null) => void;
  /** Visit to prefill on the next client note, set when opening notes from the schedule. */
  noteAppointmentId: string | null;
  /** Increments on every selectClient call, so forms can reset per selection event. */
  selectionVersion: number;
  /** Ask the Clients page to open this client's details with Notes expanded (from a calendar card). */
  openClientNotes: (clientId: string, appointmentId: string) => void;
  /** Client whose Notes the Clients page should open next; cleared once the page has taken it. */
  notesRequest: string | null;
  clearNotesRequest: () => void;
}

const OwnerContext = createContext<OwnerContextValue | null>(null);

export function useOwner(): OwnerContextValue {
  const value = useContext(OwnerContext);
  if (!value) throw new Error("useOwner must be used inside OwnerProvider");
  return value;
}

const EMPTY: OwnerData = { calendar: null, requests: [], clients: [], policy: null, zone: "UTC" };

export function OwnerProvider({ api, notify, children }: {
  api: OwnerApi; notify: Notify; children: ReactNode;
}) {
  const [data, setData] = useState<OwnerData>(EMPTY);
  const [loaded, setLoaded] = useState(false);
  const [stamp, setStamp] = useState<RefreshStamp>({ version: 0, preserveSelection: false });
  const [tab, setTab] = useState<Tab>("schedule");
  const [pickedDate, setPickedDate] = useState<string | null>(null);
  const [view, setView] = useState<CalendarView>("day");
  const [selectedClientId, setSelectedClientId] = useState<string | null>(null);
  const [noteAppointmentId, setNoteAppointmentId] = useState<string | null>(null);
  const [selectionVersion, setSelectionVersion] = useState(0);
  const [notesRequest, setNotesRequest] = useState<string | null>(null);
  // The provider is keyed by session; once unmounted, late responses must not notify.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const safeNotify = useCallback<Notify>((message, error) => {
    if (alive.current) notify(message, error);
  }, [notify]);

  // Overlapping refreshes may finish out of order; only the latest one may update state.
  const latestRefresh = useRef(0);

  const refresh = useCallback(async (preserveSelection = false) => {
    const request = ++latestRefresh.current;
    const [calendar, requests, clients, policy] = await Promise.all([
      api.get<CalendarSnapshot>("/calendar"),
      api.get<Appointment[]>("/requests"),
      api.get<ClientProfile[]>("/clients"),
      api.get<PolicyState>("/policy").catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }),
    ]);
    if (!alive.current || request !== latestRefresh.current) return;
    setData({ calendar, requests, clients, policy, zone: policy?.record.policy.timezone ?? "UTC" });
    setLoaded(true);
    setStamp((current) => ({ version: current.version + 1, preserveSelection }));
  }, [api]);

  const change = useCallback<OwnerContextValue["change"]>(
    async (path, method, body, success, idempotent = true) => {
      safeNotify(""); // a new action starts: the previous result no longer applies
      try {
        await api.request(path, { method, body, idempotent });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (error instanceof ApiError && error.status === 409) {
          const conflictMessage = path === "/policy/seed"
            ? "Pilot policy could not be loaded because the policy or calendar changed"
            : `Nothing was saved: ${message}`;
          try {
            await refresh(true);
            safeNotify(`${conflictMessage}. Current state has been refreshed.`, true);
          } catch (refreshError) {
            safeNotify(`${conflictMessage}. Current state could not load: ${
              refreshError instanceof Error ? refreshError.message : String(refreshError)}`, true);
          }
        } else {
          safeNotify(`Nothing was saved: ${message}`, true);
        }
        return false;
      }
      try {
        await refresh();
        safeNotify(success);
      } catch (error) {
        safeNotify(`${success}, but the latest view could not load: ${
          error instanceof Error ? error.message : String(error)}`, true);
      }
      return true;
    }, [api, refresh, safeNotify]);

  // Until the owner navigates, the date is today in the business timezone. Before the first
  // load the zone is unknown, so there is no date ("") and navigation does nothing.
  const date = pickedDate ?? (loaded ? todayKey(data.zone) : "");
  const goToday = useCallback(() => setPickedDate(null), []);
  const stepRange = useCallback((direction: -1 | 1) => {
    if (!loaded) return;
    setPickedDate(stepDate(pickedDate ?? todayKey(data.zone), view, direction));
  }, [loaded, pickedDate, data.zone, view]);

  const resolveLocal = useCallback(async (value: string) => {
    safeNotify(""); // first step of a save: clear the previous result
    if (!value) throw new Error("Choose a local date and time");
    const result = await api.get<{ instant: string }>(`/local-time?value=${encodeURIComponent(value)}`);
    return result.instant;
  }, [api, safeNotify]);

  const selectClient = useCallback((clientId: string | null, appointmentId: string | null = null) => {
    setSelectedClientId(clientId);
    setNoteAppointmentId(appointmentId);
    setSelectionVersion((current) => current + 1);
  }, []);

  const openClientNotes = useCallback((clientId: string, appointmentId: string) => {
    selectClient(clientId, appointmentId);
    setNotesRequest(clientId);
    setTab("clients");
  }, [selectClient]);
  const clearNotesRequest = useCallback(() => setNotesRequest(null), []);

  useEffect(() => {
    refresh().catch((error: unknown) =>
      safeNotify(error instanceof Error ? error.message : String(error), true));
  }, [refresh, safeNotify]);

  const value = useMemo<OwnerContextValue>(() => ({
    api, data, loaded, stamp, refresh, change, resolveLocal, notify: safeNotify, tab, setTab,
    date, view, setView, goToday, stepRange,
    selectedClientId, selectClient, noteAppointmentId, selectionVersion,
    openClientNotes, notesRequest, clearNotesRequest,
  }), [api, data, loaded, stamp, refresh, change, resolveLocal, safeNotify, tab,
    date, view, goToday, stepRange, selectedClientId, selectClient, noteAppointmentId, selectionVersion,
    openClientNotes, notesRequest, clearNotesRequest]);

  return <OwnerContext.Provider value={value}>{children}</OwnerContext.Provider>;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
