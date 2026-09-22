"use client";

import * as React from "react";
import { ArrowUpCircle } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { Spinner } from "@/components/ui/spinner";
import {
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";

import type { SagDesktopUpdateState } from "@/lib/desktop-bridge";

export type DesktopUpdateState = SagDesktopUpdateState;

export interface DesktopUpdateBridge {
  getUpdateState(): Promise<DesktopUpdateState>;
  downloadUpdate(version: string): Promise<{ started: boolean }>;
  installUpdate(version: string): Promise<{ started: boolean }>;
  onUpdateState(listener: (state: DesktopUpdateState) => void): () => void;
}

export function connectDesktopUpdater(
  bridge: DesktopUpdateBridge,
  onState: (state: DesktopUpdateState) => void,
): () => void {
  let active = true;
  let receivedLiveState = false;
  const unsubscribe = bridge.onUpdateState((state) => {
    if (!active) return;
    receivedLiveState = true;
    onState(state);
  });
  void bridge.getUpdateState().then((state) => {
    if (active && !receivedLiveState) onState(state);
  }).catch(() => {
    // The live subscription remains active if the initial IPC snapshot fails.
  });
  return () => {
    active = false;
    unsubscribe();
  };
}

export function DesktopUpdateIndicatorView({
  state,
  onInstall,
  onDownload,
  busy = false,
}: {
  state: DesktopUpdateState;
  onInstall: (version: string) => void;
  onDownload: (version: string) => void;
  busy?: boolean;
}) {
  const t = useTranslations("DesktopUpdate");
  const retry = state.status === "error" && state.version
    && (state.operation === "download" || state.operation === "install");
  if (state.status !== "available" && state.status !== "downloading"
    && state.status !== "downloaded" && !retry) return null;

  const version = "version" in state ? state.version : undefined;
  const downloading = state.status === "downloading";
  const install = state.status === "downloaded"
    || (state.status === "error" && state.operation === "install");
  const label = state.status === "error"
    ? t(install ? "retryInstall" : "retryDownload", { version: version! })
    : state.status === "available"
      ? t("available", { version: state.version })
      : state.status === "downloading"
        ? t("downloading", { percent: Math.round(state.percent) })
        : t("restart", { version: version! });

  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        type="button"
        tooltip={label}
        aria-label={label}
        disabled={downloading || busy}
        aria-disabled={downloading || busy}
        onClick={() => {
          if (!version || downloading || busy) return;
          if (install) onInstall(version);
          else onDownload(version);
        }}
        className="bg-blue-500/10 text-blue-700 hover:bg-blue-500/15 hover:text-blue-800 dark:text-blue-300 dark:hover:text-blue-200"
      >
        {state.status === "downloading" ? (
          <Spinner className="size-4" />
        ) : (
          <ArrowUpCircle className="size-4" />
        )}
        <span>{label}</span>
        <span
          className="ml-auto size-2 rounded-full bg-blue-500 group-data-[collapsible=icon]:hidden"
          aria-hidden="true"
        />
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}

export function DesktopUpdateIndicator() {
  const t = useTranslations("DesktopUpdate");
  const [busy, setBusy] = React.useState(false);
  const actionPending = React.useRef(false);
  const [state, setState] = React.useState<DesktopUpdateState>({ status: "idle" });
  const bridge =
    typeof window !== "undefined" && window.sagDesktop?.isDesktop
      ? window.sagDesktop
      : null;

  React.useEffect(() => {
    if (!bridge?.getUpdateState || !bridge.installUpdate || !bridge.downloadUpdate) return;
    return connectDesktopUpdater(bridge, setState);
  }, [bridge]);

  if (!bridge) return null;

  const runAction = async (operation: "download" | "install", version: string) => {
    if (actionPending.current) return;
    actionPending.current = true;
    setBusy(true);
    try {
      const result = operation === "download"
        ? await bridge.downloadUpdate(version)
        : await bridge.installUpdate(version);
      if (!result.started) toast.error(t(operation === "download" ? "downloadUnavailable" : "installUnavailable"));
    } catch {
      toast.error(t(operation === "download" ? "downloadFailed" : "installFailed"));
    } finally {
      actionPending.current = false;
      setBusy(false);
    }
  };

  return (
    <DesktopUpdateIndicatorView
      state={state}
      busy={busy}
      onDownload={(version) => { void runAction("download", version); }}
      onInstall={(version) => { void runAction("install", version); }}
    />
  );
}
