"use client";

import { useTranslations } from "next-intl";
import { useApp } from "@/components/features/app-shell";
import { ArchivedThreadsCard } from "@/components/features/archived-threads-card";
import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export function AccountSettings() {
  const t = useTranslations("AccountSettings");
  const { user, capabilities, logout } = useApp();
  const fnosManaged = capabilities?.auth_mode === "fnos";
  const initial =
    user?.name.trim().slice(0, 1).toUpperCase() ||
    user?.email.trim().slice(0, 1).toUpperCase() ||
    "?";

  return (
    <div className="flex flex-col gap-6">
      <SettingsSection title={t("identityTitle")} description={t("identityDescription")}>
        <div className="flex items-center justify-between gap-4 p-4 sm:p-5">
          <div className="flex min-w-0 items-center gap-3">
            <Avatar className="size-10">
              <AvatarFallback className="text-sm">{initial}</AvatarFallback>
            </Avatar>
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold text-foreground">
                {user?.name || t("nameMissing")}
              </div>
              <div className="mt-0.5 truncate text-sm text-muted-foreground">
                {user?.email || t("emailMissing")}
              </div>
            </div>
          </div>
          <Badge variant="success" className="shrink-0">
            {fnosManaged ? t("fnosManaged") : t("local")}
          </Badge>
        </div>
        {fnosManaged ? <SettingsRow title={t("fnosManaged")} description={t("fnosManagedDescription")}><span /></SettingsRow> : <SettingsRow
          title={t("signOutTitle")}
          description={t("signOutDescription")}
          layout="inline"
        >
          <Button variant="outline" size="sm" onClick={logout}>
            <LogOut />
            {t("signOut")}
          </Button>
        </SettingsRow>}
      </SettingsSection>

      <ArchivedThreadsCard />
    </div>
  );
}
import { LogOut } from "lucide-react";
