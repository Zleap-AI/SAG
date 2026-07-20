"use client";

import * as React from "react";
import { RotateCcw } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";

import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Slider } from "@/components/ui/slider";
import { useIsMobile } from "@/hooks/use-mobile";
import {
  UNIVERSE_VIEW_LIMITS,
  minimumUniverseCacheCapacity,
  normalizeUniverseViewPreferences,
  type UniverseViewPreferences,
} from "@/lib/universe-view-preferences";
import { cn } from "@/lib/utils";

export interface UniverseViewSettingsProps {
  preferences: UniverseViewPreferences;
  onChange: (preferences: UniverseViewPreferences) => void;
  onReset: () => void;
  entityCategories: string[];
  compact?: boolean;
  isMobile?: boolean;
}

function SettingSlider({
  ariaLabel,
  value,
  min,
  max,
  step,
  recommended,
  onChange,
}: {
  ariaLabel: string;
  value: number;
  min: number;
  max: number;
  step: number;
  recommended: number;
  onChange: (value: number) => void;
}) {
  const t = useTranslations("GraphSettings");
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between text-sm">
        <span className="text-muted-foreground">{t("value.current")}</span>
        <span className="font-mono font-semibold tabular-nums">{value}</span>
      </div>
      <Slider
        aria-label={ariaLabel}
        max={max}
        min={min}
        onValueChange={([next]) => {
          if (next !== undefined) onChange(next);
        }}
        step={step}
        value={[value]}
      />
      <div className="flex justify-between text-xs text-muted-foreground">
        <span>{min}</span>
        <span>{t("value.recommended", { count: recommended })}</span>
        <span>{max}</span>
      </div>
    </div>
  );
}

export function UniverseViewSettings({
  preferences,
  onChange,
  onReset,
  entityCategories,
  compact = false,
  isMobile,
}: UniverseViewSettingsProps) {
  const locale = useLocale();
  const t = useTranslations("GraphSettings");
  const detectedMobile = useIsMobile();
  const mobile = isMobile ?? detectedMobile;
  const normalized = React.useMemo(
    () => normalizeUniverseViewPreferences(preferences),
    [preferences],
  );
  const availableTypes = React.useMemo(() => {
    const selected = normalized.entityTypes ?? [];
    return [...new Set([...entityCategories, ...selected]
      .map((category) => category.trim())
      .filter(Boolean))]
      .sort((left, right) => left.localeCompare(right, locale));
  }, [entityCategories, locale, normalized.entityTypes]);

  const emit = React.useCallback((patch: Partial<UniverseViewPreferences>) => {
    onChange(normalizeUniverseViewPreferences({ ...normalized, ...patch }));
  }, [normalized, onChange]);

  const toggleType = React.useCallback((category: string, checked: boolean) => {
    const next = new Set(normalized.entityTypes ?? availableTypes);
    if (checked) next.add(category);
    else {
      if (next.size <= 1) return;
      next.delete(category);
    }
    const selected = availableTypes.filter((item) => next.has(item));
    emit({
      entityTypes: selected.length === availableTypes.length
        ? null
        : selected,
    });
  }, [availableTypes, emit, normalized.entityTypes]);

  const allTypesSelected = normalized.entityTypes === null;
  const selectedTypeCount = normalized.entityTypes?.length
    ?? availableTypes.length;
  const minimumCache = minimumUniverseCacheCapacity(
    normalized.eventWindowSize,
    normalized.temporalPageSize,
    normalized.temporalPrefetchPages,
  );

  return (
    <div
      className={cn("flex flex-col", compact ? "gap-4" : "gap-6")}
      data-settings-section="graph"
      data-settings-compact={compact}
      data-settings-device={mobile ? "mobile" : "desktop"}
      data-event-window-size={normalized.eventWindowSize}
      data-event-card-preview-count={normalized.eventCardPreviewCount}
      data-cache-capacity={normalized.cacheCapacity}
    >
      <SettingsSection
        title={t("cards.title")}
        description={t("cards.description")}
      >
        <SettingsRow
          title={t("cards.enabled.title")}
          description={t("cards.enabled.description")}
          layout="inline"
          className={cn(compact && "sm:flex-row sm:items-center")}
        >
          <Checkbox
            aria-label={t("cards.enabled.aria")}
            checked={normalized.cardsEnabled}
            onCheckedChange={(value) => emit({ cardsEnabled: value === true })}
          />
        </SettingsRow>
        <SettingsRow
          title={t("cards.preview.title")}
          description={t("cards.preview.description")}
        >
          <SettingSlider
            ariaLabel={t("cards.preview.aria")}
            value={normalized.eventCardPreviewCount}
            min={UNIVERSE_VIEW_LIMITS.eventCardPreviewCount.min}
            max={Math.min(
              normalized.eventWindowSize,
              UNIVERSE_VIEW_LIMITS.eventCardPreviewCount.max,
            )}
            step={UNIVERSE_VIEW_LIMITS.eventCardPreviewCount.step}
            recommended={UNIVERSE_VIEW_LIMITS.eventCardPreviewCount.default}
            onChange={(eventCardPreviewCount) => emit({
              eventCardPreviewCount,
            })}
          />
        </SettingsRow>
      </SettingsSection>

      <SettingsSection
        title={t("entityTypes.title")}
        description={t("entityTypes.description")}
      >
        <div className="p-4 sm:p-5">
          <div className="overflow-hidden rounded-lg border">
            <label className={cn(
              "flex items-center gap-3 border-b px-3 py-2.5 text-sm font-medium",
              allTypesSelected ? "cursor-default" : "cursor-pointer",
            )}>
              <Checkbox
                checked={allTypesSelected}
                disabled={allTypesSelected}
                onCheckedChange={(value) => {
                  if (value === true) emit({ entityTypes: null });
                }}
              />
              {t("entityTypes.all")}
              <span className="ml-auto text-xs font-normal text-muted-foreground">
                {availableTypes.length || t("entityTypes.none")}
              </span>
            </label>
            {availableTypes.length > 0 ? (
              <div className={cn(
                "grid max-h-48 overflow-y-auto py-1",
                !compact && "sm:grid-cols-2",
              )}>
                {availableTypes.map((category) => {
                  const checked = allTypesSelected
                    || Boolean(normalized.entityTypes?.includes(category));
                  const lastSelected = checked && selectedTypeCount <= 1;
                  return (
                    <label
                      className={cn(
                        "flex items-center gap-3 px-3 py-2 text-sm",
                        lastSelected
                          ? "cursor-default"
                          : "cursor-pointer hover:bg-muted/60",
                      )}
                      key={category}
                    >
                      <Checkbox
                        checked={checked}
                        disabled={lastSelected}
                        onCheckedChange={(value) =>
                          toggleType(category, value === true)}
                      />
                      <span className="min-w-0 truncate" title={category}>
                        {category}
                      </span>
                    </label>
                  );
                })}
              </div>
            ) : (
              <p className="px-3 py-3 text-sm text-muted-foreground">
                {t("entityTypes.emptyDescription")}
              </p>
            )}
          </div>
        </div>
      </SettingsSection>

      <SettingsSection
        title={t("window.title")}
        description={t("window.description")}
      >
        <SettingsRow
          title={t("eventWindow.title")}
          description={t("eventWindow.description")}
        >
          <SettingSlider
            ariaLabel={t("eventWindow.aria")}
            value={normalized.eventWindowSize}
            min={UNIVERSE_VIEW_LIMITS.eventWindowSize.min}
            max={UNIVERSE_VIEW_LIMITS.eventWindowSize.max}
            step={UNIVERSE_VIEW_LIMITS.eventWindowSize.step}
            recommended={UNIVERSE_VIEW_LIMITS.eventWindowSize.default}
            onChange={(eventWindowSize) => emit({ eventWindowSize })}
          />
        </SettingsRow>

        <SettingsRow
          title={t("cacheCapacity.title")}
          description={t("cacheCapacity.description")}
        >
          <SettingSlider
            ariaLabel={t("cacheCapacity.aria")}
            value={normalized.cacheCapacity}
            min={minimumCache}
            max={UNIVERSE_VIEW_LIMITS.cacheCapacity.max}
            step={UNIVERSE_VIEW_LIMITS.cacheCapacity.step}
            recommended={UNIVERSE_VIEW_LIMITS.cacheCapacity.default}
            onChange={(cacheCapacity) => emit({ cacheCapacity })}
          />
        </SettingsRow>
      </SettingsSection>

      <SettingsSection
        title={t("temporal.title")}
        description={t("temporal.description")}
      >
        <SettingsRow
          title={t("temporal.page.title")}
          description={t("temporal.page.description")}
        >
          <SettingSlider
            ariaLabel={t("temporal.page.aria")}
            value={normalized.temporalPageSize}
            min={UNIVERSE_VIEW_LIMITS.temporalPageSize.min}
            max={UNIVERSE_VIEW_LIMITS.temporalPageSize.max}
            step={UNIVERSE_VIEW_LIMITS.temporalPageSize.step}
            recommended={UNIVERSE_VIEW_LIMITS.temporalPageSize.default}
            onChange={(temporalPageSize) => emit({ temporalPageSize })}
          />
        </SettingsRow>
        <SettingsRow
          title={t("temporal.prefetch.title")}
          description={t("temporal.prefetch.description")}
        >
          <SettingSlider
            ariaLabel={t("temporal.prefetch.aria")}
            value={normalized.temporalPrefetchPages}
            min={UNIVERSE_VIEW_LIMITS.temporalPrefetchPages.min}
            max={UNIVERSE_VIEW_LIMITS.temporalPrefetchPages.max}
            step={UNIVERSE_VIEW_LIMITS.temporalPrefetchPages.step}
            recommended={UNIVERSE_VIEW_LIMITS.temporalPrefetchPages.default}
            onChange={(temporalPrefetchPages) => emit({
              temporalPrefetchPages,
            })}
          />
        </SettingsRow>
      </SettingsSection>

      <div className={cn(
        "flex flex-col gap-3 rounded-lg border bg-card px-4 py-3 shadow-soft",
        !compact && "sm:flex-row sm:items-center sm:justify-between sm:px-5",
      )}>
        <p className="text-xs leading-5 text-muted-foreground">
          {t("reset.savedLocally")}
        </p>
        <Button type="button" variant="outline" size="sm" onClick={onReset}>
          <RotateCcw />
          {t("reset.action")}
        </Button>
      </div>
    </div>
  );
}
