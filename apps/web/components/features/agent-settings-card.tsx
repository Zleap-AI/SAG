"use client";

import * as React from "react";
import { Save } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import { MAX_AVATAR_CHARS, normalizeAvatar } from "@/lib/avatar";
import { DEFAULT_AGENT_AVATAR, DEFAULT_AGENT_NAME } from "@/lib/branding";
import type { PetPresence } from "@/lib/app-initialization";
import { usePetPresence } from "@/lib/pet-preferences";
import { cn } from "@/lib/utils";
import { useApp } from "@/components/features/app-shell";
import { PetHeadAvatar } from "@/components/features/pet-head-avatar";
import { PetAppearanceSettings } from "@/components/features/pet-appearance-settings";
import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

/** 助手设定 —— 默认 agent 的名字 / 头像 / 开场白 / 系统提示。 */
export function AgentSettingsCard({ compact = false }: { compact?: boolean }) {
  const t = useTranslations("AgentSettings");
  const { agent, replaceAgent } = useApp();
  const fieldId = React.useId();
  const avatarId = `${fieldId}-avatar`;
  const nameId = `${fieldId}-name`;
  const greetingId = `${fieldId}-greeting`;
  const promptId = `${fieldId}-prompt`;
  const [name, setName] = React.useState("");
  const [avatar, setAvatar] = React.useState("");
  const [greeting, setGreeting] = React.useState("");
  const [systemPrompt, setSystemPrompt] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [petPresence, setPetPresence] = usePetPresence();

  React.useEffect(() => {
    if (!agent) return;
    setName(agent.name);
    setAvatar(agent.avatar);
    setGreeting(agent.persona?.greeting ?? "");
    setSystemPrompt(agent.persona?.system_prompt ?? "");
  }, [agent]);

  if (!agent) return null;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!agent) return;
    setSaving(true);
    try {
      const updated = await api.updateAgent(agent.id, {
        name: name.trim() || DEFAULT_AGENT_NAME,
        avatar: normalizeAvatar(avatar) || DEFAULT_AGENT_AVATAR,
        persona: { ...agent.persona, greeting: greeting.trim(), system_prompt: systemPrompt },
      });
      replaceAgent(updated);
      toast.success(t("saved"));
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={cn("flex flex-col", compact ? "gap-4" : "gap-6")}>
      <form onSubmit={save}>
        <SettingsSection
          title={t("title")}
          description={t("description")}
          footer={
            <div className="flex justify-end">
              <Button type="submit" disabled={saving}>
                {saving ? <Spinner /> : <Save />}
                {saving ? t("saving") : t("save")}
              </Button>
            </div>
          }
        >
        <SettingsRow
          title={t("basicInfo")}
          description={t("basicDescription", { max: MAX_AVATAR_CHARS })}
        >
          <div
            className={cn(
              "grid",
              compact ? "gap-3" : "gap-4 sm:grid-cols-[12rem_minmax(0,1fr)]",
            )}
          >
            <Field className="min-w-0">
              <FieldLabel htmlFor={avatarId}>{t("avatar")}</FieldLabel>
              <div className="flex items-center gap-2">
                <PetHeadAvatar
                  face={avatar || DEFAULT_AGENT_AVATAR}
                  size="md"
                />
                <Input
                  id={avatarId}
                  value={avatar}
                  onChange={(e) => setAvatar(normalizeAvatar(e.target.value))}
                  className="min-w-0 text-center font-mono"
                  placeholder="AI"
                />
              </div>
              <FieldDescription>{t("avatarDescription")}</FieldDescription>
            </Field>
            <Field className="min-w-0">
              <FieldLabel htmlFor={nameId}>{t("name")}</FieldLabel>
              <Input id={nameId} value={name} onChange={(e) => setName(e.target.value)} required />
            </Field>
          </div>
        </SettingsRow>
        <SettingsRow
          title={t("petVisibility")}
          description={t("petVisibilityDescription")}
          layout={compact ? "stacked" : "inline"}
          contentClassName={compact ? "flex justify-end" : "self-end sm:self-auto"}
        >
          <Select
            value={petPresence}
            onValueChange={(value) => setPetPresence(value as PetPresence)}
          >
            <SelectTrigger className="w-44" aria-label={t("petVisibility")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="always">{t("petVisibilityAlways")}</SelectItem>
              <SelectItem value="explore-only">{t("petVisibilityExplore")}</SelectItem>
              <SelectItem value="hidden">{t("petVisibilityHidden")}</SelectItem>
            </SelectContent>
          </Select>
        </SettingsRow>
        <SettingsRow title={t("greeting")} description={t("greetingDescription")}>
          <Field>
            <FieldLabel htmlFor={greetingId} className="sr-only">
              {t("greeting")}
            </FieldLabel>
            <Input id={greetingId} value={greeting} onChange={(e) => setGreeting(e.target.value)} />
          </Field>
        </SettingsRow>
        <SettingsRow title={t("systemPrompt")} description={t("systemPromptDescription")}>
          <Field>
            <FieldLabel htmlFor={promptId} className="sr-only">
              {t("systemPrompt")}
            </FieldLabel>
            <Textarea
              id={promptId}
              rows={5}
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              placeholder={t("systemPromptPlaceholder")}
              className="min-h-32 resize-y"
            />
            <FieldDescription>{t("systemPromptHelp")}</FieldDescription>
          </Field>
        </SettingsRow>
        </SettingsSection>
      </form>
      <PetAppearanceSettings
        agentFace={avatar || DEFAULT_AGENT_AVATAR}
        compact={compact}
      />
    </div>
  );
}
