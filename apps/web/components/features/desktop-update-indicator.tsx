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

export type DesktopUpdateState =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "available"; version: string }
  | { status: "not-available" }
  | { status: "downloading"; percent: number }
  | { status: "downloaded"; version: string }
  | { status: "error"; message: string };

export interface DesktopUpdateBridge {
  getUpdateState(): Promise<DesktopUpdateState>;
  installUpdate(): Promise<{ started: boolean }>;
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
}: {
  state: DesktopUpdateState;
  onInstall: () => void;
}) {
  const t = useTranslations("DesktopUpdate");
  if (
    state.status !== "available"
    && state.status !== "downloading"
    && state.status !== "downloaded"
  ) {
    return null;
  }

  const downloaded = state.status === "downloaded";
  const label =
    state.status === "available"
      ? t("available", { version: state.version })
      : state.status === "downloading"
        ? t("downloading", { percent: Math.round(state.percent) })
        : t("restart", { version: state.version });

  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        type="button"
        tooltip={label}
        aria-label={label}
        aria-disabled={!downloaded}
        onClick={() => {
          if (downloaded) onInstall();
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
  const [state, setState] = React.useState<DesktopUpdateState>({ status: "idle" });
  const bridge =
    typeof window !== "undefined" && window.sagDesktop?.isDesktop
      ? window.sagDesktop
      : null;

  React.useEffect(() => {
    if (!bridge?.getUpdateState || !bridge.installUpdate) return;
    return connectDesktopUpdater(bridge, setState);
  }, [bridge]);

  if (!bridge) return null;

  return (
    <DesktopUpdateIndicatorView
      state={state}
      onInstall={() => {
        void bridge.installUpdate().then(({ started }) => {
          if (!started) toast.error(t("installUnavailable"));
        }).catch(() => toast.error(t("installFailed")));
      }}
    />
  );
}
