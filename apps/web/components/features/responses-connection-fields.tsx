"use client";

import { useTranslations } from "next-intl";
import { ChevronDown } from "lucide-react";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import type { ResponsesProviderId } from "@/lib/types";

type Connection = {
  provider: ResponsesProviderId;
  endpoint: string;
  api_version: string;
  send_temperature: boolean;
};

export function OpenAIAPIFormatField({ id, value, onChange, disabled }: {
  id: string;
  value: "openai" | "responses";
  onChange: (value: "openai" | "responses") => void;
  disabled: boolean;
}) {
  const t = useTranslations("ModelConfig");
  return <Field>
    <FieldLabel htmlFor={id}>{t("apiFormat")}</FieldLabel>
    <Select value={value} disabled={disabled} onValueChange={format => onChange(format as "openai" | "responses")}>
      <SelectTrigger id={id}><SelectValue /></SelectTrigger>
      <SelectContent>
        <SelectItem value="openai">Chat Completions</SelectItem>
        <SelectItem value="responses">Responses API</SelectItem>
      </SelectContent>
    </Select>
    <FieldDescription>{t("apiFormatDescription")}</FieldDescription>
  </Field>;
}

export function ResponsesBaseURLField({ id, value, onChange, disabled }: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  const t = useTranslations("ModelConfig");
  return <Field>
    <FieldLabel htmlFor={`${id}-endpoint`}>{t("responsesEndpoint")}</FieldLabel>
    <Input id={`${id}-endpoint`} value={value} disabled={disabled}
      placeholder="https://api.openai.com/v1/responses"
      onChange={event => onChange(event.target.value)} />
    <FieldDescription>{t("responsesEndpointDescription")}</FieldDescription>
  </Field>;
}

export function ResponsesConnectionFields({ id, value, onChange, disabled }: {
  id: string;
  value: Connection;
  onChange: (value: Connection) => void;
  disabled: boolean;
}) {
  const t = useTranslations("ModelConfig");
  return <div className="grid gap-4 sm:grid-cols-2">
    <Field>
      <FieldLabel htmlFor={`${id}-temperature`}>{t("responsesSendTemperature")}</FieldLabel>
      <div className="flex h-9 items-center">
        <Switch id={`${id}-temperature`} checked={value.send_temperature} disabled={disabled}
          onCheckedChange={checked => onChange({ ...value, send_temperature: checked })} />
      </div>
      <FieldDescription>{t("responsesTemperatureDescription")}</FieldDescription>
    </Field>
    <details className="group sm:col-span-2">
      <summary className="flex w-fit cursor-pointer list-none items-center gap-2 text-sm font-medium [&::-webkit-details-marker]:hidden">
        <ChevronDown className="size-4 opacity-50 transition-transform group-open:rotate-180" />
        {t("responsesAdvanced")}
      </summary>
      <div className="grid gap-4 pt-4 sm:grid-cols-2">
        <Field>
          <FieldLabel htmlFor={`${id}-auth`}>{t("responsesAuthentication")}</FieldLabel>
          <Select value={value.provider === "azure" ? "azure" : "openai"} disabled={disabled}
            onValueChange={provider => onChange({ ...value, provider: provider as ResponsesProviderId, api_version: "" })}>
            <SelectTrigger id={`${id}-auth`}><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="openai">{t("responsesBearerAuthentication")}</SelectItem>
              <SelectItem value="azure">{t("responsesAzureAuthentication")}</SelectItem>
            </SelectContent>
          </Select>
          <FieldDescription>{t("responsesAuthenticationDescription")}</FieldDescription>
        </Field>
        {value.provider === "azure" && <Field>
          <FieldLabel htmlFor={`${id}-version`}>{t("responsesApiVersion")}</FieldLabel>
          <Input id={`${id}-version`} value={value.api_version} disabled={disabled}
            placeholder="v1" onChange={event => onChange({ ...value, api_version: event.target.value })} />
        </Field>}
      </div>
    </details>
  </div>;
}
