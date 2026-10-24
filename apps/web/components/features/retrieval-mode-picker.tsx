"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { JevQuickSetup, type JevQuickSetupState } from "@/components/features/jev-quick-setup";
import { Field, FieldLabel } from "@/components/ui/field";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  getSearchStrategy,
  isJevConfigDisabledReason,
  isJevScoredStrategy,
  needsJevQuickSetup,
  SEARCH_STRATEGIES,
  SEARCH_STRATEGY_ICONS,
  type SearchStrategy,
  type SearchStrategyDisabledMap,
} from "@/lib/retrieval-config";
import { cn } from "@/lib/utils";

/** Select value standing in for "no source default; follow the global setting". */
export const FOLLOW_GLOBAL_VALUE = "__global__";

const MODE_COPY = {
  vector: { summary: "vectorSummary", whenToUse: "vectorWhenToUse" },
  multi_es_fast: { summary: "multiEsFastSummary", whenToUse: "multiEsFastWhenToUse" },
  multi: { summary: "multiSummary", whenToUse: "multiWhenToUse" },
  multi_jev: { summary: "multiJevSummary", whenToUse: "multiJevWhenToUse" },
  sag2: { summary: "sag2Summary", whenToUse: "sag2WhenToUse" },
} as const satisfies Record<SearchStrategy, { summary: string; whenToUse: string }>;

const badgeClassName =
  "rounded bg-muted px-1 py-0.5 text-[10px] font-normal leading-none text-muted-foreground";

export function RetrievalModePicker({
  id,
  value,
  onValueChange,
  globalStrategy,
  disabledMap,
  disabled = false,
  jevSetup,
}: {
  id: string;
  /** `null` follows the global default. */
  value: SearchStrategy | null;
  onValueChange: (value: SearchStrategy | null) => void;
  globalStrategy: SearchStrategy;
  /** `capabilities.search_strategies_disabled`, normalised by `getSearchStrategiesDisabled`. */
  disabledMap?: SearchStrategyDisabledMap;
  disabled?: boolean;
  /**
   * Inline JEV service setup. Rendered under the description only while the
   * selected mode is JEV-scored and blocked solely by a missing configuration.
   */
  jevSetup?: JevQuickSetupState;
}) {
  const t = useTranslations("RetrievalModes");
  const strategies = useTranslations("SearchStrategies");
  const globalMode = getSearchStrategy(globalStrategy);
  const globalLabel = strategies(globalMode.labelKey);
  const selected = value ? getSearchStrategy(value) : null;
  const selectedInfo = selected ? disabledMap?.[selected.value] : undefined;
  const selectedUnavailable = Boolean(selectedInfo) && !isJevConfigDisabledReason(selectedInfo?.reason);
  const showJevSetup = jevSetup !== undefined && needsJevQuickSetup(value, disabledMap);
  const describedMode = selected ?? globalMode;

  return (
    <Field>
      <FieldLabel htmlFor={id}>{t("label")}</FieldLabel>
      <Select
        value={value ?? FOLLOW_GLOBAL_VALUE}
        disabled={disabled}
        onValueChange={(next) => onValueChange(
          next === FOLLOW_GLOBAL_VALUE ? null : (next as SearchStrategy),
        )}
      >
        <SelectTrigger id={id} data-testid="retrieval-mode-picker">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={FOLLOW_GLOBAL_VALUE} data-testid="retrieval-mode-global">
            {t("followGlobal", { label: globalLabel })}
          </SelectItem>
          <SelectSeparator />
          {SEARCH_STRATEGIES.map((strategy) => {
            const info = disabledMap?.[strategy.value];
            const needsKeyOnly = Boolean(info) && isJevConfigDisabledReason(info?.reason);
            const unavailable = Boolean(info) && !needsKeyOnly;
            const paid = isJevScoredStrategy(strategy.value);
            const Icon = SEARCH_STRATEGY_ICONS[strategy.value];
            return (
              <SelectItem
                key={strategy.value}
                value={strategy.value}
                disabled={unavailable}
                data-testid={`retrieval-mode-${strategy.value}`}
              >
                <span className="flex flex-col gap-0.5">
                  <span className="flex items-center gap-1.5">
                    <Icon className="size-3.5 shrink-0 text-muted-foreground" />
                    {strategies(strategy.labelKey)}
                    {paid && (
                      <span className={badgeClassName} data-testid={`retrieval-mode-${strategy.value}-paid`}>
                        {t("paid")}
                      </span>
                    )}
                    {needsKeyOnly && <span className={badgeClassName}>{t("needsKeyBadge")}</span>}
                    {unavailable && <span className={badgeClassName}>{t("unavailable")}</span>}
                  </span>
                  {unavailable && info && (
                    <span className="block max-w-[260px] whitespace-normal text-[11px] leading-4 text-muted-foreground">
                      {info.message}
                    </span>
                  )}
                </span>
              </SelectItem>
            );
          })}
        </SelectContent>
      </Select>

      <div
        className="rounded-md border bg-muted/40 px-3 py-2.5 text-xs leading-5 text-muted-foreground"
        data-testid="retrieval-mode-description"
      >
        {!selected && (
          <>
            <p>{t("followGlobalDescription")}</p>
            <p className="mt-1 font-medium text-foreground">{t("globalModeLead", { label: globalLabel })}</p>
          </>
        )}
        <p className={cn(selected && "text-foreground")}>{t(MODE_COPY[describedMode.value].summary)}</p>
        <p>{t(MODE_COPY[describedMode.value].whenToUse)}</p>
        {isJevScoredStrategy(describedMode.value) && (
          <p className="mt-1 font-medium text-foreground/80" data-testid="retrieval-mode-paid-note">
            {t("paidNeedsKey")}
          </p>
        )}
        {selectedUnavailable && selectedInfo && (
          <p role="alert" className="mt-1 text-destructive">{selectedInfo.message}</p>
        )}
      </div>

      {showJevSetup && jevSetup && (
        <JevQuickSetup setup={jevSetup} idPrefix={id} disabled={disabled} />
      )}
    </Field>
  );
}
