import type { PreferenceClient, PreferenceRule } from "@/api/types";

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function clock(value: string): string {
  const [hour = "0", minute = "00"] = value.split(":");
  const h = Number(hour);
  return `${h % 12 === 0 ? 12 : h % 12}:${minute} ${h < 12 ? "AM" : "PM"}`;
}

/** One reviewed rule in words, with exact weekdays and start-time ranges. A client rule names
 * its profile by the current display name. */
export function describeRule(rule: PreferenceRule, clients: PreferenceClient[] = []): string {
  const days = rule.weekdays.length === 7 ? "Every day" : rule.weekdays.map((day) => DAYS[day] ?? "?").join(", ");
  const times = rule.windows.length
    ? `visits start ${rule.windows.map((window) => `${clock(window.start)}–${clock(window.end)}`).join(" or ")}`
    : "any start time";
  if (!rule.client_id) return `${days}: ${times}`;
  const name = clients.find((client) => client.client_id === rule.client_id)?.name ?? "Removed client";
  return `${name} — ${days}: ${times}`;
}

export function summarize(rules: PreferenceRule[], clients: PreferenceClient[] = []): string {
  return rules.map((rule) => describeRule(rule, clients)).join(" · ");
}
