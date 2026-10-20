import type { PreferenceRule } from "@/api/types";

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function clock(value: string): string {
  const [hour = "0", minute = "00"] = value.split(":");
  const h = Number(hour);
  return `${h % 12 === 0 ? 12 : h % 12}:${minute} ${h < 12 ? "AM" : "PM"}`;
}

/** One reviewed rule in words, with exact weekdays and start-time ranges. */
export function describeRule(rule: PreferenceRule): string {
  const days = rule.weekdays.length === 7 ? "Every day" : rule.weekdays.map((day) => DAYS[day] ?? "?").join(", ");
  const times = rule.windows.length
    ? `visits start ${rule.windows.map((window) => `${clock(window.start)}–${clock(window.end)}`).join(" or ")}`
    : "any start time";
  return `${days}: ${times}`;
}

export function summarize(rules: PreferenceRule[]): string {
  return rules.map(describeRule).join(" · ");
}
