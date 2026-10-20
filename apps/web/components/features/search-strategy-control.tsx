"use client";

import { Check, ChevronDown, Sparkles, Zap } from "lucide-react";
import { useTranslations } from "next-intl";
import { useApp } from "@/components/features/app-shell";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import {
  getSearchStrategy,
  getSearchStrategiesDisabled,
  SEARCH_STRATEGIES,
  type SearchStrategy,
} from "@/lib/retrieval-config";

const STRATEGY_ICONS = {
  vector: Zap,
  multi: Sparkles,
  multi_jev: Sparkles,
} as const;

export function SearchStrategyControl({
  value,
  defaultValue,
  onValueChange,
}: {
  value: SearchStrategy;
  defaultValue: SearchStrategy;
  onValueChange: (value: SearchStrategy) => void;
}) {
  const t = useTranslations("SearchStrategies");
  const { capabilities } = useApp();
  const disabled = getSearchStrategiesDisabled(capabilities, t("unsupportedBackend"));
  const currentStrategy = getSearchStrategy(value);
  const CurrentIcon = STRATEGY_ICONS[currentStrategy.value];

  return (
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
        <DropdownMenuLabel className="px-2 pb-1 pt-1 text-[11px]">{t("mode")}</DropdownMenuLabel>
        {SEARCH_STRATEGIES.map((strategy) => {
          const Icon = STRATEGY_ICONS[strategy.value];
          const selected = strategy.value === value;
          const isDefault = strategy.value === defaultValue;

          return (
            <DropdownMenuItem
              key={strategy.value}
              disabled={Boolean(disabled[strategy.value])}
              onSelect={() => onValueChange(strategy.value)}
              className="items-start gap-2.5 px-2 py-2"
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
                </span>
                <span className="mt-0.5 block text-[11px] leading-4 text-muted-foreground">
                  {disabled[strategy.value]?.message ?? t(strategy.descriptionKey)}
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
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
