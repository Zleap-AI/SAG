"use client";

import {
  Check,
  ChevronDown,
  ScanSearch,
  Settings2,
  ShieldQuestion,
  Sparkles,
  Zap,
} from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { Fragment, type ComponentType } from "react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import {
  getSearchStrategy,
  isJevConfigDisabledReason,
  isJevScoredStrategy,
  SEARCH_STRATEGIES,
  type SearchStrategy,
  type SearchStrategyDisabledMap,
} from "@/lib/retrieval-config";
import { settingsTabHref } from "@/lib/settings-config";

const STRATEGY_ICONS: Record<SearchStrategy, ComponentType<{ className?: string }>> = {
  vector: Zap,
  multi_es_fast: ShieldQuestion,
  multi: Sparkles,
  multi_jev: Sparkles,
  sag2: ScanSearch,
};

/** The shared JEV service section inside the knowledge settings tab. */
const JEV_SETTINGS_HREF = settingsTabHref("knowledge", "jev");

const badgeClassName =
  "rounded bg-muted px-1 py-0.5 text-[10px] font-normal leading-none text-muted-foreground";

export function SearchStrategyControl({
  value,
  defaultValue,
  onValueChange,
  disabledMap,
}: {
  value: SearchStrategy;
  defaultValue: SearchStrategy;
  onValueChange: (value: SearchStrategy) => void;
  /**
   * 后端 capabilities.search_strategies_disabled 直接透传;未传时全部可用。
   * key 缺失即启用,存在即灰置 + Tooltip 展示 message。
   */
  disabledMap?: SearchStrategyDisabledMap;
}) {
  const t = useTranslations("SearchStrategies");
  const currentStrategy = getSearchStrategy(value);
  const CurrentIcon = STRATEGY_ICONS[currentStrategy.value];

  return (
    <TooltipProvider delayDuration={150}>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={t("modeAria", { strategy: t(currentStrategy.labelKey) })}
            data-testid="search-strategy"
            className="inline-flex h-7 shrink-0 items-center gap-1 rounded-md px-2 text-[11px] text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring data-[state=open]:bg-muted data-[state=open]:text-foreground"
          >
            <CurrentIcon className="size-3.5" />
            <span>{t(currentStrategy.labelKey)}</span>
            <ChevronDown className="size-3 opacity-60" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72 p-1.5">
          <DropdownMenuLabel className="px-2 pb-1 pt-1 text-[11px]">
            {t("mode")}
          </DropdownMenuLabel>
          {SEARCH_STRATEGIES.map((strategy) => {
            const Icon = STRATEGY_ICONS[strategy.value];
            const selected = strategy.value === value;
            const isDefault = strategy.value === defaultValue;
            const paid = isJevScoredStrategy(strategy.value);
            const disabledInfo = disabledMap?.[strategy.value];
            // A disabled Radix item ignores pointer events, so the settings
            // shortcut lives in its own selectable row right below the item.
            const needsJevSettings = paid && isJevConfigDisabledReason(disabledInfo?.reason);
            const item = (
              <DropdownMenuItem
                disabled={Boolean(disabledInfo)}
                onSelect={(event) => {
                  if (disabledInfo) {
                    // Radix 默认会关闭菜单;这里阻断以便 Tooltip 保持可见,
                    // 且不触发无意义的 onValueChange。
                    event.preventDefault();
                    return;
                  }
                  onValueChange(strategy.value);
                }}
                className={cn(
                  "items-start gap-2.5 px-2 py-2",
                  disabledInfo && "cursor-not-allowed opacity-50",
                )}
              >
                <Icon className="mt-0.5 size-3.5 shrink-0" />
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-1.5 text-xs font-medium text-foreground">
                    {t(strategy.labelKey)}
                    {isDefault && <span className={badgeClassName}>{t("default")}</span>}
                    {paid && (
                      <span className={badgeClassName} data-testid={`search-strategy-${strategy.value}-paid`}>
                        {t("paid")}
                      </span>
                    )}
                    {disabledInfo && <span className={badgeClassName}>{t("disabledSuffix")}</span>}
                  </span>
                  <span className="mt-0.5 block text-[11px] leading-4 text-muted-foreground">
                    {disabledInfo?.message ?? t(strategy.descriptionKey)}
                  </span>
                </span>
                <Check
                  className={cn(
                    "mt-0.5 size-3.5 shrink-0 text-foreground transition-opacity",
                    selected ? "opacity-100" : "opacity-0",
                  )}
                />
              </DropdownMenuItem>
            );
            return (
              <Fragment key={strategy.value}>
                {disabledInfo ? (
                  <Tooltip>
                    <TooltipTrigger asChild>{item}</TooltipTrigger>
                    <TooltipContent side="left" className="max-w-[220px] text-left">
                      {disabledInfo.message}
                    </TooltipContent>
                  </Tooltip>
                ) : item}
                {needsJevSettings && (
                  <DropdownMenuItem asChild className="ml-8 mr-2 mb-1 gap-1.5 px-2 py-1 text-[11px] text-foreground">
                    <Link
                      href={JEV_SETTINGS_HREF}
                      data-testid={`search-strategy-${strategy.value}-settings`}
                    >
                      <Settings2 className="size-3.5" />
                      {t("openSettings")}
                    </Link>
                  </DropdownMenuItem>
                )}
              </Fragment>
            );
          })}
        </DropdownMenuContent>
      </DropdownMenu>
    </TooltipProvider>
  );
}
