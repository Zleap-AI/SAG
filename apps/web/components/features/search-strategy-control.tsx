"use client";

import { Check, ChevronDown, ShieldQuestion, Sparkles, Zap } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ComponentType } from "react";

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
  SEARCH_STRATEGIES,
  type SearchStrategy,
  type SearchStrategyDisabledMap,
} from "@/lib/retrieval-config";

const STRATEGY_ICONS: Record<SearchStrategy, ComponentType<{ className?: string }>> = {
  vector: Zap,
  multi_es_fast: ShieldQuestion,
  multi: Sparkles,
};

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
            const disabledInfo = disabledMap?.[strategy.value];
            const item = (
              <DropdownMenuItem
                key={strategy.value}
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
                  <span className="flex items-center gap-1.5 text-xs font-medium text-foreground">
                    {t(strategy.labelKey)}
                    {isDefault && (
                      <span className="rounded bg-muted px-1 py-0.5 text-[10px] font-normal leading-none text-muted-foreground">
                        {t("default")}
                      </span>
                    )}
                    {disabledInfo && (
                      <span className="rounded bg-muted px-1 py-0.5 text-[10px] font-normal leading-none text-muted-foreground">
                        {t("disabledSuffix")}
                      </span>
                    )}
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
            if (!disabledInfo) return item;
            return (
              <Tooltip key={strategy.value}>
                <TooltipTrigger asChild>{item}</TooltipTrigger>
                <TooltipContent side="left" className="max-w-[220px] text-left">
                  {disabledInfo.message}
                </TooltipContent>
              </Tooltip>
            );
          })}
        </DropdownMenuContent>
      </DropdownMenu>
    </TooltipProvider>
  );
}
