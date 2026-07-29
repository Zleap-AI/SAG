import { settingsTabHref } from "./settings-config";

export function uploadConcurrencyGuidance(compact: boolean) {
  return {
    href: settingsTabHref("knowledge", "document-processing"),
    textClassName: compact
      ? "max-w-sm text-[11px] leading-relaxed text-muted-foreground"
      : "max-w-sm text-xs text-muted-foreground",
  };
}
