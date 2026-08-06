import {
  Bot,
  Bug,
  Cpu,
  LibraryBig,
  Monitor,
  Moon,
  Orbit,
  Palette,
  Plug,
  Sun,
  UserRound,
} from "lucide-react";

export const SETTINGS_PAGE = {
  titleKey: "title",
  descriptionKey: "description",
} as const;

export const SETTINGS_TABS = [
  { value: "account", labelKey: "account", icon: UserRound },
  { value: "agent", labelKey: "agent", icon: Bot },
  { value: "model", labelKey: "model", icon: Cpu },
  { value: "knowledge", labelKey: "knowledge", icon: LibraryBig },
  { value: "integrations", labelKey: "integrations", icon: Plug },
  { value: "appearance", labelKey: "appearance", icon: Palette },
  { value: "graph", labelKey: "graph", icon: Orbit },
  { value: "diagnostics", labelKey: "diagnostics", icon: Bug },
] as const;

export type SettingsTab = (typeof SETTINGS_TABS)[number]["value"];

export function isSettingsTab(value: string | null): value is SettingsTab {
  return SETTINGS_TABS.some((tab) => tab.value === value);
}

export function resolveSettingsTab(value: string | null): SettingsTab {
  return isSettingsTab(value) ? value : "account";
}

/** Canonical URL used by full-size, mini and ambient settings entry points. */
export function settingsTabHref(tab: SettingsTab, section?: string) {
  const params = new URLSearchParams({ tab });
  if (section?.trim()) params.set("section", section.trim());
  return `/settings?${params.toString()}`;
}

export const THEME_OPTIONS = [
  { value: "light", labelKey: "light", icon: Sun },
  { value: "dark", labelKey: "dark", icon: Moon },
  { value: "system", labelKey: "system", icon: Monitor },
] as const;

export const TIMEZONE_OPTIONS = [
  { value: "Asia/Shanghai", labelKey: "shanghai" },
  { value: "UTC", labelKey: "utc" },
  { value: "Asia/Hong_Kong", labelKey: "hongKong" },
  { value: "Asia/Singapore", labelKey: "singapore" },
  { value: "Asia/Tokyo", labelKey: "tokyo" },
  { value: "Asia/Seoul", labelKey: "seoul" },
  { value: "Asia/Dubai", labelKey: "dubai" },
  { value: "Europe/London", labelKey: "london" },
  { value: "Europe/Berlin", labelKey: "berlin" },
  { value: "America/New_York", labelKey: "newYork" },
  { value: "America/Chicago", labelKey: "chicago" },
  { value: "America/Denver", labelKey: "denver" },
  { value: "America/Los_Angeles", labelKey: "losAngeles" },
  { value: "Australia/Sydney", labelKey: "sydney" },
] as const;

export const ARCHIVED_THREADS_PAGE_SIZE = 5;
export const SIDEBAR_THREADS_PAGE_SIZE = 6;
