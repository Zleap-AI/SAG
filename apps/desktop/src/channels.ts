export const DESKTOP_CHANNELS = {
  appInfo: "desktop:app-info",
  checkForUpdates: "desktop:check-for-updates",
  updateState: "desktop:update-state",
} as const;

export type UpdateState =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "available"; version: string }
  | { status: "not-available" }
  | { status: "downloading"; percent: number }
  | { status: "downloaded"; version: string }
  | { status: "error"; message: string };
